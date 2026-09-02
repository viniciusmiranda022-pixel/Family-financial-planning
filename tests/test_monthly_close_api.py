"""HTTP-level tests for PR 7: finding lifecycle endpoints and monthly close.

Auth wiring, household isolation, `reason` validation and the interaction
between a real INV-014 finding and the monthly-close trust gate are all
exercised end to end here, over the actual FastAPI app. The precise trust
gate arithmetic (which combinations of status/snapshot flags allow
`trusted`) is unit-tested in isolation in `tests/test_monthly_close.py`.

This module gives itself a fully isolated database via a FastAPI
`dependency_overrides` on `get_db`, instead of relying on `app.db.engine`
(a process-wide singleton created once from whichever `DATABASE_URL` the
*first*-imported test module set -- `tests/test_api.py` also builds a real
`TestClient(app)`, and Python's module cache means both files would
otherwise silently share one engine/database).
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "monthly-close-api-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-monthly-close-api-data-{uuid.uuid4().hex}")

import app.api as api_module  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Category,
    Commission,
    FinancialSnapshot,
    Household,
    IntegrityRun,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402

PERIOD = "2026-08"

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _create_household_admin(*, household_name: str, username: str, password: str) -> None:
    with _TestSessionLocal() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Admin",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()


def test_monthly_close_lifecycle_and_finding_actions() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            setup = client.post(
                "/api/auth/setup",
                json={
                    "household_name": "Família Fechamento",
                    "name": "Admin",
                    "username": "admin-close",
                    "password": "senha-local-segura",
                },
            )
            assert setup.status_code == 201

            # No close yet: GET synthesizes a default without persisting anything.
            empty_state = client.get(f"/api/monthly-closes/{PERIOD}")
            assert empty_state.status_code == 200
            assert empty_state.json()["status"] == "open"
            assert empty_state.json()["id"] is None
            assert empty_state.json()["pending"]["eligible_for_trust"] is False

            assert client.get("/api/monthly-closes/2026-8").status_code == 422

            member = client.post(
                "/api/users",
                json={
                    "name": "Kelly",
                    "username": "kelly-close",
                    "password": "senha-kelly-segura",
                    "is_admin": False,
                },
            )
            assert member.status_code == 201
            with TestClient(app) as member_client:
                login = member_client.post(
                    "/api/auth/login",
                    json={"username": "kelly-close", "password": "senha-kelly-segura"},
                )
                assert login.status_code == 200
                assert member_client.post(f"/api/monthly-closes/{PERIOD}/run").status_code == 403
                assert member_client.post(f"/api/monthly-closes/{PERIOD}/trust").status_code == 403
                assert member_client.post(
                    f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "não autorizado"}
                ).status_code == 403

            account = client.post(
                "/api/accounts",
                json={
                    "name": "Nubank",
                    "institution": "Nubank",
                    "account_type": "credit_card",
                    "owner_label": "Família",
                    "last_four": "1234",
                },
            )
            assert account.status_code == 201
            with _TestSessionLocal() as db:
                household = db.scalar(select(Household).where(Household.name == "Família Fechamento"))
                duplicate = Transaction(
                    household_id=household.id,
                    account_id=account.json()["id"],
                    booked_at=date(2026, 8, 15),
                    description="Compra suspeita",
                    normalized_description="COMPRA SUSPEITA",
                    amount=Decimal("-10.00"),
                    transaction_type="expense",
                    owner_label="Família",
                    fingerprint="f" * 64,
                    possible_duplicate=True,
                    excluded=False,
                    reviewed=False,
                )
                db.add(duplicate)
                db.commit()
                duplicate_id = duplicate.id

            run_response = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert run_response.status_code == 200
            run_payload = run_response.json()
            assert run_payload["status"] == "review_required"
            assert run_payload["snapshot_id"]
            assert run_payload["integrity_run_id"]
            assert run_payload["pending"]["integrity_status"] == "critical"
            assert run_payload["pending"]["eligible_for_trust"] is False

            assert client.get(f"/api/monthly-closes/{PERIOD}").json()["status"] == "review_required"

            blocked_trust = client.post(f"/api/monthly-closes/{PERIOD}/trust")
            assert blocked_trust.status_code == 422
            detail = blocked_trust.json()["detail"]
            assert any("critical" in reason for reason in detail["reasons"])

            findings = client.get(
                "/api/integrity/findings",
                params={"status": "open", "invariant_id": "INV-014", "period": PERIOD},
            ).json()
            assert findings["total"] == 1
            finding_id = findings["items"][0]["id"]

            # `status=active` (open OR acknowledged) is what the UI's global
            # BLOCK/CRITICAL panel relies on to stay visible regardless of
            # the paginated list's own filters -- see the engineering review
            # on PR 7 ("UI pode esconder BLOCK/CRITICAL ativo pelo cap de
            # 100"). Rejects anything but the documented literal values.
            assert client.get(
                "/api/integrity/findings", params={"status": "not-a-real-status"}
            ).status_code == 422
            active_critical = client.get(
                "/api/integrity/findings",
                params={"status": "active", "severity": "critical", "period": PERIOD},
            ).json()
            assert finding_id in {item["id"] for item in active_critical["items"]}

            assert client.post(
                f"/api/integrity/findings/{finding_id}/acknowledge", json={}
            ).status_code == 422

            acknowledge = client.post(
                f"/api/integrity/findings/{finding_id}/acknowledge",
                json={"reason": "Estou revisando com a Kelly"},
            )
            assert acknowledge.status_code == 200
            assert acknowledge.json()["status"] == "acknowledged"
            assert acknowledge.json()["acknowledgement_reason"] == "Estou revisando com a Kelly"

            resolve = client.post(
                f"/api/integrity/findings/{finding_id}/resolve",
                json={"reason": "Confirmado como lançamento legítimo e único"},
            )
            assert resolve.status_code == 200
            assert resolve.json()["status"] == "resolved"

            assert client.post(
                f"/api/integrity/findings/{finding_id}/resolve", json={"reason": "segunda tentativa"}
            ).status_code == 409

            # A `resolved` finding is terminal, not "active" -- it must drop
            # out of the always-visible BLOCK/CRITICAL panel's query too.
            active_after_resolve = client.get(
                "/api/integrity/findings",
                params={"status": "active", "severity": "critical", "period": PERIOD},
            ).json()
            assert finding_id not in {item["id"] for item in active_after_resolve["items"]}

            with _TestSessionLocal() as db:
                transaction = db.get(Transaction, duplicate_id)
                # `resolve` never touched the underlying financial fact.
                assert transaction.possible_duplicate is True
                assert transaction.excluded is False
                assert transaction.amount == Decimal("-10.00")

            # Resolving the finding doesn't retroactively rewrite the already
            # completed run's stored summary -- trust must still fail.
            assert client.post(f"/api/monthly-closes/{PERIOD}/trust").status_code == 422

            # Cross-household isolation: a second, unrelated household cannot
            # see or act on this household's finding or monthly close.
            _create_household_admin(
                household_name="Outra Família", username="admin-outro", password="outra-senha-segura"
            )
            with TestClient(app) as other_client:
                other_login = other_client.post(
                    "/api/auth/login",
                    json={"username": "admin-outro", "password": "outra-senha-segura"},
                )
                assert other_login.status_code == 200
                assert other_client.get(f"/api/integrity/findings/{finding_id}").status_code == 404
                assert other_client.post(
                    f"/api/integrity/findings/{finding_id}/resolve", json={"reason": "tentativa indevida"}
                ).status_code == 404
                assert other_client.get(f"/api/monthly-closes/{PERIOD}").json()["status"] == "open"
                assert other_client.post(
                    f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "tentativa indevida"}
                ).status_code == 409

            # The duplicate transaction is still flagged (`resolve` never
            # cleared `possible_duplicate`), so a fresh run reopens the same
            # finding (reincidence) instead of silently staying "resolved"
            # forever.
            rerun = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert rerun.status_code == 200
            assert rerun.json()["pending"]["open_findings"] >= 1
            reopened_finding = client.get(f"/api/integrity/findings/{finding_id}").json()
            assert reopened_finding["status"] == "open"
            assert reopened_finding["occurrence_count"] >= 2

            assert client.post(
                f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "ainda não fechado"}
            ).status_code == 409
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_monthly_close_reaches_trusted_for_a_real_clean_period() -> None:
    """`POST run -> POST trust` must be able to reach `trusted` for real.

    Before this fix, `build_baseline_checks` only ever proved INV-014, whose
    applicable set is empty for a clean month, so the period-scoped run's own
    `trusted_for_projection`/`trusted_for_reports` could never become true
    through the real endpoint -- `_trust_gate()` also requires INV-005,
    INV-006, INV-018, INV-022 (projection) and INV-019, INV-020, INV-022
    (reports) to have actually been evaluated and to have passed, not merely
    to be absent from the result set. `tests/test_monthly_close.py` already
    covers the gate arithmetic by manufacturing `IntegrityRun.summary` and
    `FinancialSnapshot.trusted_*` directly, which proves the arithmetic but
    not that the public API can ever produce those values -- that was
    precisely the engineering review's objection blocking this PR. This
    drives one household through a genuinely clean month (a confirmed
    opening balance, one ordinary income transaction, no duplicates)
    exclusively through the real HTTP API and asserts the close reaches
    `trusted`.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        # `/api/auth/setup` bootstraps this single-tenant deployment exactly
        # once process-wide; `test_monthly_close_lifecycle_and_finding_actions`
        # above already consumed it on this module's shared `_test_engine`, so
        # this household is created directly (same helper the cross-household
        # isolation scenario above uses) and then logged in for real.
        _create_household_admin(
            household_name="Família Fechamento Limpo",
            username="admin-close-clean",
            password="senha-local-segura",
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin-close-clean", "password": "senha-local-segura"},
            )
            assert login.status_code == 200

            investment = client.post(
                "/api/accounts",
                json={
                    "name": "Reserva DI",
                    "institution": "Banco XP",
                    "account_type": "investment",
                    "owner_label": "Família",
                },
            )
            assert investment.status_code == 201

            checking = client.post(
                "/api/accounts",
                json={
                    "name": "Conta corrente",
                    "institution": "Banco XP",
                    "account_type": "checking",
                    "owner_label": "Família",
                },
            )
            assert checking.status_code == 201

            balance = client.post(
                "/api/account-balances",
                json={
                    "account_id": investment.json()["id"],
                    "amount": "1000.00",
                    "as_of_date": f"{PERIOD}-01",
                    "observation_type": "opening",
                },
            )
            assert balance.status_code == 201

            with _TestSessionLocal() as db:
                household = db.scalar(
                    select(Household).where(Household.name == "Família Fechamento Limpo")
                )
                income_category = Category(household_id=household.id, name="Receitas")
                db.add(income_category)
                db.flush()
                db.add(
                    Transaction(
                        household_id=household.id,
                        account_id=checking.json()["id"],
                        category_id=income_category.id,
                        booked_at=date(2026, 8, 10),
                        description="Salário",
                        normalized_description="SALARIO",
                        amount=Decimal("500.00"),
                        transaction_type="income",
                        owner_label="Família",
                        fingerprint="i" * 64,
                        possible_duplicate=False,
                        excluded=False,
                        reviewed=True,
                    )
                )
                db.commit()

            run_response = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert run_response.status_code == 200
            run_payload = run_response.json()
            assert run_payload["status"] == "review_required"
            assert run_payload["pending"]["integrity_status"] == "healthy"
            assert run_payload["pending"]["trusted_for_projection"] is True
            assert run_payload["pending"]["trusted_for_reports"] is True
            assert run_payload["pending"]["eligible_for_trust"] is True

            trust_response = client.post(f"/api/monthly-closes/{PERIOD}/trust")
            assert trust_response.status_code == 200
            trust_payload = trust_response.json()
            assert trust_payload["status"] == "trusted"
            assert trust_payload["closed_by"]
            assert trust_payload["closed_at"]

            # A materially incomplete month must still be rejected: deleting
            # the only lineage-bearing transaction and reopening/rerunning
            # must never leave a stale `trusted` state standing on facts that
            # no longer exist. With zero transactions left, INV-022 (CRITICAL)
            # genuinely FAILs -- lineage is provably incomplete, not merely
            # unattempted -- so the period must land on `critical`, never
            # silently stay `healthy` or fall back to an artificial pass.
            reopen = client.post(
                f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "Auditoria retroativa"}
            )
            assert reopen.status_code == 200
            with _TestSessionLocal() as db:
                db.query(Transaction).where(Transaction.household_id == household.id).delete()
                db.commit()
            rerun = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert rerun.status_code == 200
            assert rerun.json()["pending"]["integrity_status"] == "critical"
            assert rerun.json()["pending"]["eligible_for_trust"] is False
            assert client.post(f"/api/monthly-closes/{PERIOD}/trust").status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_future_commissions_gross_stays_noncanonical_after_trust() -> None:
    """PR 7, Round 7: a live figure must never get folded into a `trusted` close.

    `future_commissions_gross` is a live `Commission` query, not a Financial
    Engine/snapshot output (see the comment above its query in `dashboard()`).
    This drives a household through a genuinely clean `run -> trust`, then
    adds a `Commission` whose `expected_date` falls inside the now-trusted
    period and re-reads `/dashboard`: the close must stay `trusted` (the new
    commission cannot retroactively change what the snapshot certified), and
    the response must publish the live figure only inside the explicitly
    uncertified `noncanonical` object -- never as a bare top-level key beside
    `trusted_for_reports`/`snapshot_checksum` (see the engineering review on
    PR 7, Round 7: "cannot remain a first-level monetary field ... without
    marking").
    """

    app.dependency_overrides[get_db] = _override_get_db
    try:
        _create_household_admin(
            household_name="Família Comissão Futura",
            username="admin-close-commission",
            password="senha-local-segura",
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin-close-commission", "password": "senha-local-segura"},
            )
            assert login.status_code == 200

            investment = client.post(
                "/api/accounts",
                json={
                    "name": "Reserva DI",
                    "institution": "Banco XP",
                    "account_type": "investment",
                    "owner_label": "Família",
                },
            )
            assert investment.status_code == 201

            checking = client.post(
                "/api/accounts",
                json={
                    "name": "Conta corrente",
                    "institution": "Banco XP",
                    "account_type": "checking",
                    "owner_label": "Família",
                },
            )
            assert checking.status_code == 201

            balance = client.post(
                "/api/account-balances",
                json={
                    "account_id": investment.json()["id"],
                    "amount": "1000.00",
                    "as_of_date": f"{PERIOD}-01",
                    "observation_type": "opening",
                },
            )
            assert balance.status_code == 201

            with _TestSessionLocal() as db:
                household = db.scalar(
                    select(Household).where(Household.name == "Família Comissão Futura")
                )
                income_category = Category(household_id=household.id, name="Receitas")
                db.add(income_category)
                db.flush()
                db.add(
                    Transaction(
                        household_id=household.id,
                        account_id=checking.json()["id"],
                        category_id=income_category.id,
                        booked_at=date(2026, 8, 10),
                        description="Salário",
                        normalized_description="SALARIO",
                        amount=Decimal("500.00"),
                        transaction_type="income",
                        owner_label="Família",
                        fingerprint="j" * 64,
                        possible_duplicate=False,
                        excluded=False,
                        reviewed=True,
                    )
                )
                db.commit()

            assert client.post(f"/api/monthly-closes/{PERIOD}/run").status_code == 200
            trust_response = client.post(f"/api/monthly-closes/{PERIOD}/trust")
            assert trust_response.status_code == 200
            assert trust_response.json()["status"] == "trusted"

            with _TestSessionLocal() as db:
                household = db.scalar(
                    select(Household).where(Household.name == "Família Comissão Futura")
                )
                db.add(
                    Commission(
                        household_id=household.id,
                        description="Comissão pendente",
                        expected_date=date(2026, 8, 20),
                        gross_amount=Decimal("777.00"),
                        status="expected",
                    )
                )
                db.commit()

            dashboard_response = client.get(f"/api/dashboard?month={PERIOD}")
            assert dashboard_response.status_code == 200
            payload = dashboard_response.json()
            assert "future_commissions_gross" not in payload
            noncanonical = payload["noncanonical"]["future_commissions_gross"]
            assert Decimal(str(noncanonical["value"])) == Decimal("777.00")
            assert noncanonical["source"] == "live_query"
            assert noncanonical["certified_by"] is None
            assert payload["trusted_for_reports"] is True

            # The live commission must not retroactively disturb the close
            # already certified from the untouched snapshot.
            still_trusted = client.get(f"/api/monthly-closes/{PERIOD}")
            assert still_trusted.status_code == 200
            assert still_trusted.json()["status"] == "trusted"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_run_failure_leaves_no_snapshot_trace_but_still_records_the_failed_run(monkeypatch) -> None:
    """A failed `run` must not persist the snapshot rebuild it made along the way.

    `run_monthly_close` rebuilds the period's canonical `FinancialSnapshot`
    *before* calling `execute_integrity_run` (the projection/report gate
    checks need it). Before the engineering review caught this, if
    `execute_integrity_run` then raised, the `except` branch committed the
    whole transaction anyway "for the audit trail" -- silently persisting
    that snapshot rebuild (and any predecessor supersession) despite the
    close having failed. This forces `execute_integrity_run` to raise on a
    real household with a real, already-current snapshot, and proves: (a)
    the period's `current` snapshot is byte-for-byte the same row afterwards
    (no new version, no supersession), and (b) a `failed` `IntegrityRun` was
    still committed for the audit trail.
    """

    app.dependency_overrides[get_db] = _override_get_db
    try:
        _create_household_admin(
            household_name="Família Falha de Fechamento",
            username="admin-close-failure",
            password="senha-local-segura",
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin-close-failure", "password": "senha-local-segura"},
            )
            assert login.status_code == 200

            account = client.post(
                "/api/accounts",
                json={
                    "name": "Conta corrente",
                    "institution": "Banco XP",
                    "account_type": "checking",
                    "owner_label": "Família",
                },
            )
            assert account.status_code == 201

            with _TestSessionLocal() as db:
                household = db.scalar(
                    select(Household).where(Household.name == "Família Falha de Fechamento")
                )
                income_category = Category(household_id=household.id, name="Receitas")
                db.add(income_category)
                db.flush()
                db.add(
                    Transaction(
                        household_id=household.id,
                        account_id=account.json()["id"],
                        category_id=income_category.id,
                        booked_at=date(2026, 8, 10),
                        description="Salário",
                        normalized_description="SALARIO",
                        amount=Decimal("500.00"),
                        transaction_type="income",
                        owner_label="Família",
                        fingerprint="j" * 64,
                        possible_duplicate=False,
                        excluded=False,
                        reviewed=True,
                    )
                )
                db.commit()

            # A first, successful run establishes a real "before" snapshot.
            first_run = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert first_run.status_code == 200
            with _TestSessionLocal() as db:
                before_snapshot = db.scalar(
                    select(FinancialSnapshot).where(
                        FinancialSnapshot.household_id == household.id,
                        FinancialSnapshot.period == PERIOD,
                        FinancialSnapshot.status == "current",
                    )
                )
                assert before_snapshot is not None
                before_id, before_version, before_checksum = (
                    before_snapshot.id,
                    before_snapshot.version,
                    before_snapshot.checksum,
                )
                runs_before = db.scalars(
                    select(IntegrityRun).where(IntegrityRun.household_id == household.id)
                ).all()
                run_count_before = len(runs_before)
                # The first successful `run` above already legitimately left
                # one `superseded` row behind (its own forced rebuild bumps
                # the version even on a clean re-check) -- the assertion
                # below must be that this count does not *grow*, not that it
                # is zero.
                superseded_count_before = len(
                    db.scalars(
                        select(FinancialSnapshot).where(
                            FinancialSnapshot.household_id == household.id,
                            FinancialSnapshot.period == PERIOD,
                            FinancialSnapshot.status == "superseded",
                        )
                    ).all()
                )

            # A second transaction changes the period's data, so the next
            # `run` would legitimately rebuild a new snapshot version -- and
            # then `execute_integrity_run` itself fails.
            with _TestSessionLocal() as db:
                db.add(
                    Transaction(
                        household_id=household.id,
                        account_id=account.json()["id"],
                        category_id=income_category.id,
                        booked_at=date(2026, 8, 20),
                        description="Reembolso",
                        normalized_description="REEMBOLSO",
                        amount=Decimal("50.00"),
                        transaction_type="income",
                        owner_label="Família",
                        fingerprint="k" * 64,
                        possible_duplicate=False,
                        excluded=False,
                        reviewed=True,
                    )
                )
                db.commit()

            def _boom(*_args, **_kwargs):
                raise RuntimeError("simulated integrity run failure")

            monkeypatch.setattr(api_module, "execute_integrity_run", _boom)

            failed_run = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert failed_run.status_code == 500

            with _TestSessionLocal() as db:
                after_snapshot = db.scalar(
                    select(FinancialSnapshot).where(
                        FinancialSnapshot.household_id == household.id,
                        FinancialSnapshot.period == PERIOD,
                        FinancialSnapshot.status == "current",
                    )
                )
                # No new version, no supersession -- the failed run's
                # in-flight snapshot rebuild never survived.
                assert after_snapshot.id == before_id
                assert after_snapshot.version == before_version
                assert after_snapshot.checksum == before_checksum
                superseded_count_after = len(
                    db.scalars(
                        select(FinancialSnapshot).where(
                            FinancialSnapshot.household_id == household.id,
                            FinancialSnapshot.period == PERIOD,
                            FinancialSnapshot.status == "superseded",
                        )
                    ).all()
                )
                assert superseded_count_after == superseded_count_before

                runs_after = db.scalars(
                    select(IntegrityRun).where(IntegrityRun.household_id == household.id)
                ).all()
                assert len(runs_after) == run_count_before + 1
                failed_row = max(runs_after, key=lambda item: item.started_at)
                assert failed_row.status == "failed"
                assert failed_row.error_code == "RuntimeError"
    finally:
        app.dependency_overrides.pop(get_db, None)
