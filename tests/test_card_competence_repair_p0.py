"""Regression suite for the P0 Work Order
(`docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md`): the auditable, reversible
repair engine for card purchases that were persisted with the pre-PR-#81
`booked_at`-month competence instead of the canonical invoice competence.

Production already contains transactions in that bad state; nothing in this
codebase is allowed to write one anymore (PR #81 fixed both creation paths --
see `tests/test_card_open_invoice_competence.py`), so every "historical bad
row" fixture here is inserted directly through the ORM, bypassing the API,
exactly the way a pre-hotfix production row would have been persisted. This
mirrors the pattern `tests/test_monthly_close_api.py` already uses for a
second, isolated household.

Covers:
1. Detector scope (`find_card_competence_candidates` via `POST .../preview`):
   only in-scope card expenses with a divergent, non-null persisted
   competence are surfaced; everything the Work Order excludes is not.
2. Preview is read-only (no mutation, no snapshot, no audit row).
3. Auth/admin gating on all three endpoints, before any lookup.
4. Apply: correctness (only `competence` changes), audit trail, atomicity
   (stale revision, no-longer-candidate, cross-household id), fail-closed
   duplicate/monthly-close blocks.
5. The mandatory financial-effect proof: budget/card-spend/category moves
   from the old period to the new one, exactly once, across dashboard,
   credit-card summary and report.
6. Logical rollback: exact restoration, and fail-closed on a stale/
   conflicting target.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "card-competence-repair-p0-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-card-competence-repair-p0-data-{uuid.uuid4().hex}")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AuditEvent,
    DuplicateGroup,
    FinancialSnapshot,
    Household,
    MonthlyFinancialClose,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services.card_competence import card_invoice_competence  # noqa: E402
from app.services.classifier import normalize_description  # noqa: E402

REPAIR_EVENT_TYPE = "transaction.card_competence_repair"
REPAIR_BATCH_EVENT_TYPE = "card_competence_repair.batch"
ROLLBACK_EVENT_TYPE = "transaction.card_competence_repair_rollback"


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), session_factory


def _setup_household(client, *, username="admin-repair", household_name="Família Reparo"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": household_name,
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text


def _add_member(client, *, username, password="senha-membro-segura", is_admin=False):
    response = client.post(
        "/api/users",
        json={"name": "Membro", "username": username, "password": password, "is_admin": is_admin},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _login(client, *, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def _create_account(client, *, name, account_type="checking", card_closing_day=None, card_due_day=None):
    payload = {"name": name, "account_type": account_type}
    if card_closing_day is not None:
        payload["card_closing_day"] = card_closing_day
    if card_due_day is not None:
        payload["card_due_day"] = card_due_day
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _category_id(client, name="Mercado e itens domésticos"):
    categories = client.get("/api/categories").json()
    return next(item["id"] for item in categories if item["name"] == name)


def _household_id(session_factory) -> str:
    with session_factory() as db:
        return db.scalar(select(User.household_id))


def _create_household_admin(session_factory, *, household_name: str, username: str, password: str) -> str:
    with session_factory() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        user = User(
            household_id=household.id,
            name="Admin",
            username=username,
            password_hash=hash_password(password),
            is_admin=True,
        )
        db.add(user)
        db.commit()
        return household.id


def _insert_legacy_transaction(
    session_factory,
    *,
    household_id,
    account_id,
    category_id,
    booked_at,
    amount,
    description="Compra legada de cartão",
    classification_source="manual_confirmed",
    competence,
    transaction_type="expense",
    possible_duplicate=False,
    canonical_status="unassigned",
    duplicate_group_id=None,
    owner_label="Família",
) -> str:
    """Bypass the API entirely -- simulates a row persisted *before* PR #81's
    hotfix, or by any out-of-scope path. No canonical command in this
    codebase can produce this state anymore; that is exactly why the P0
    detector/repairer needs to exist."""

    # Mirrors how every real canonical command persists an expense
    # (`create_manual_transaction`: `amount = -abs(payload.amount)`) --
    # `app.services.financial_snapshots._collect()` only recognizes an
    # `expense`/`refund` row as spend when `amount < 0`, so a fixture with a
    # positive amount would silently never contribute to any snapshot total.
    stored_amount = Decimal(str(amount))
    if transaction_type == "expense":
        stored_amount = -abs(stored_amount)
    with session_factory() as db:
        tx = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=booked_at,
            description=description,
            normalized_description=normalize_description(description),
            amount=stored_amount,
            transaction_type=transaction_type,
            owner_label=owner_label,
            fingerprint=f"legacy-{uuid.uuid4().hex}",
            occurred_at=booked_at,
            competence=competence,
            classification_source=classification_source,
            classification_version="pre-pr81-legacy",
            canonical_status=canonical_status,
            source_priority=90,
            confidence=Decimal("1"),
            excluded=False,
            possible_duplicate=possible_duplicate,
            duplicate_group_id=duplicate_group_id,
            reviewed=True,
        )
        db.add(tx)
        db.commit()
        return tx.id


def _open_duplicate_group(session_factory, *, household_id, status="open", confidence="0.9000") -> str:
    with session_factory() as db:
        group = DuplicateGroup(
            household_id=household_id,
            group_key=f"legacy-group-{uuid.uuid4().hex}",
            status=status,
            confidence=Decimal(confidence),
            rule_version="2026.09.1",
        )
        db.add(group)
        db.commit()
        return group.id


def _trust_monthly_close(session_factory, *, household_id, period) -> None:
    with session_factory() as db:
        db.add(MonthlyFinancialClose(household_id=household_id, period=period, status="trusted"))
        db.commit()


def _get_transaction(session_factory, transaction_id):
    with session_factory() as db:
        return db.get(Transaction, transaction_id)


def _get_snapshot(session_factory, *, household_id, period):
    with session_factory() as db:
        return db.scalar(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == household_id,
                FinancialSnapshot.period == period,
                FinancialSnapshot.snapshot_kind == "actual",
                FinancialSnapshot.status == "current",
            )
        )


def _set_cash_cap(client, *, monthly_cash_cap):
    profile = client.get("/api/profile").json()
    payload = {
        "monthly_salary_net": profile["monthly_salary_net"],
        "monthly_cash_cap": monthly_cash_cap,
        "emergency_floor": profile["emergency_floor"],
        "food_allowance": profile["food_allowance"],
        "meal_allowance_daily": profile["meal_allowance_daily"],
        "workdays_month": profile["workdays_month"],
        "investment_name": profile["investment_name"],
        "investment_balance": profile["investment_balance"],
        "investment_gross_annual_rate": profile["investment_gross_annual_rate"],
        "investment_income_tax_rate": profile["investment_income_tax_rate"],
        "projection_end": profile["projection_end"],
    }
    response = client.put("/api/profile", json=payload)
    assert response.status_code == 200, response.text


def _find_candidate(preview_json, transaction_id):
    return next(item for item in preview_json["candidates"] if item["transaction_id"] == transaction_id)


# ---------------------------------------------------------------------------
# 1. Detector scope -- only exactly what the Work Order allows is surfaced.
# ---------------------------------------------------------------------------


def test_preview_detects_legacy_card_purchase_with_wrong_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        booked_at = date(2026, 9, 15)
        expected = card_invoice_competence(
            Account(account_type="credit_card", card_closing_day=2, card_due_day=9), booked_at
        )
        assert expected == "2026-10"
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=booked_at,
            amount=200,
            competence="2026-09",
        )

        response = client.post("/api/maintenance/card-competence/preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body["financial_revision"], int)
        candidate = _find_candidate(body, transaction_id)
        assert candidate["competence_current"] == "2026-09"
        assert candidate["competence_expected"] == "2026-10"
        assert candidate["blocked"] is False
        assert candidate["blocked_reasons"] == []
        assert "2026-09" in body["periods_affected"]
        assert "2026-10" in body["periods_affected"]
        assert body["eligible_count"] == 1
        assert body["blocked_count"] == 0


def test_preview_excludes_transaction_already_at_canonical_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 1),
            amount=100,
            competence="2026-09",  # already correct: before closing day 2
        )

        body = client.post("/api/maintenance/card-competence/preview").json()
        assert all(item["transaction_id"] != transaction_id for item in body["candidates"])


def test_preview_excludes_non_card_account():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        checking = _create_account(client, name="Conta Corrente", account_type="checking")
        category_id = _category_id(client)
        # Non-card: competence must equal booked_at's month by construction,
        # so there is no divergence to detect -- this row can never be a
        # candidate regardless of what its persisted competence says.
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=checking,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=50,
            competence="2026-09",
        )

        body = client.post("/api/maintenance/card-competence/preview").json()
        assert all(item["transaction_id"] != transaction_id for item in body["candidates"])


def test_preview_excludes_card_without_fully_configured_cycle():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(client, name="Cartão sem ciclo", account_type="credit_card")
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=90,
            competence="2026-09",
        )

        body = client.post("/api/maintenance/card-competence/preview").json()
        assert all(item["transaction_id"] != transaction_id for item in body["candidates"])


def test_preview_excludes_out_of_scope_classification_source():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        for source in ("local_rule", "builtin_rule", "financial_plan_workbook", "legacy"):
            transaction_id = _insert_legacy_transaction(
                session_factory,
                household_id=household_id,
                account_id=card,
                category_id=category_id,
                booked_at=date(2026, 9, 15),
                amount=90,
                competence="2026-09",
                classification_source=source,
            )
            body = client.post("/api/maintenance/card-competence/preview").json()
            assert all(item["transaction_id"] != transaction_id for item in body["candidates"]), source


def test_preview_excludes_null_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=90,
            competence=None,
        )
        body = client.post("/api/maintenance/card-competence/preview").json()
        assert all(item["transaction_id"] != transaction_id for item in body["candidates"])


def test_preview_excludes_non_expense_transaction_type():
    """A card *payment*/refund/reconciliation must never be treated as a
    purchase candidate, even if its competence looks divergent."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        for tx_type in ("reconciliation", "refund", "transfer", "income"):
            transaction_id = _insert_legacy_transaction(
                session_factory,
                household_id=household_id,
                account_id=card,
                category_id=category_id,
                booked_at=date(2026, 9, 15),
                amount=90,
                competence="2026-09",
                transaction_type=tx_type,
            )
            body = client.post("/api/maintenance/card-competence/preview").json()
            assert all(item["transaction_id"] != transaction_id for item in body["candidates"]), tx_type


def test_preview_covers_december_january_rollover():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 12, 20),
            amount=300,
            competence="2026-12",
        )
        body = client.post("/api/maintenance/card-competence/preview").json()
        candidate = _find_candidate(body, transaction_id)
        assert candidate["competence_expected"] == "2027-01"


# ---------------------------------------------------------------------------
# 2. Preview never mutates anything.
# ---------------------------------------------------------------------------


def test_preview_is_read_only_no_mutation_no_snapshot_no_audit():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )

        with session_factory() as db:
            snapshot_count_before = db.scalar(select(FinancialSnapshot.id)) is not None
            audit_count_before = len(db.scalars(select(AuditEvent)).all())
        assert not snapshot_count_before

        response = client.post("/api/maintenance/card-competence/preview")
        assert response.status_code == 200, response.text

        transaction = _get_transaction(session_factory, transaction_id)
        assert transaction.competence == "2026-09"
        with session_factory() as db:
            assert db.scalar(select(FinancialSnapshot.id)) is None
            assert len(db.scalars(select(AuditEvent)).all()) == audit_count_before


# ---------------------------------------------------------------------------
# 3. Auth/admin gating -- before any lookup.
# ---------------------------------------------------------------------------


def test_preview_requires_authentication():
    client, _session_factory = _client()
    with client:
        response = client.post("/api/maintenance/card-competence/preview")
        assert response.status_code == 401


def test_preview_requires_admin():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        _add_member(client, username="membro-comum", is_admin=False)
        _login(client, username="membro-comum", password="senha-membro-segura")
        response = client.post("/api/maintenance/card-competence/preview")
        assert response.status_code == 403


def test_apply_requires_admin_before_any_mutation():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        _add_member(client, username="membro-comum", is_admin=False)
        _login(client, username="membro-comum", password="senha-membro-segura")

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={"transaction_ids": [transaction_id], "expected_financial_revision": 0, "reason": "teste"},
        )
        assert response.status_code == 403
        transaction = _get_transaction(session_factory, transaction_id)
        assert transaction.competence == "2026-09"


def test_rollback_requires_admin():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        _add_member(client, username="membro-comum", is_admin=False)
        _login(client, username="membro-comum", password="senha-membro-segura")
        response = client.post(
            "/api/maintenance/card-competence/rollback",
            json={"transaction_ids": ["whatever"], "expected_financial_revision": 0, "reason": "teste"},
        )
        assert response.status_code == 403


def test_apply_rejects_empty_selection():
    client, _session_factory = _client()
    with client:
        _setup_household(client)
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={"transaction_ids": [], "expected_financial_revision": 0, "reason": "teste"},
        )
        assert response.status_code == 422


def test_apply_rejects_reason_below_minimum_length():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={"transaction_ids": [transaction_id], "expected_financial_revision": 0, "reason": "ok"},
        )
        assert response.status_code == 422
        transaction = _get_transaction(session_factory, transaction_id)
        assert transaction.competence == "2026-09"


# ---------------------------------------------------------------------------
# 4. Apply -- correctness, audit trail, preserved fields.
# ---------------------------------------------------------------------------


def test_apply_changes_only_competence_and_preserves_every_other_field():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            description="Compra legada preservada",
            competence="2026-09",
        )
        before = _get_transaction(session_factory, transaction_id)
        before_snapshot = {
            "id": before.id,
            "household_id": before.household_id,
            "account_id": before.account_id,
            "category_id": before.category_id,
            "document_id": before.document_id,
            "booked_at": before.booked_at,
            "occurred_at": before.occurred_at,
            "description": before.description,
            "normalized_description": before.normalized_description,
            "amount": before.amount,
            "transaction_type": before.transaction_type,
            "owner_label": before.owner_label,
            "fingerprint": before.fingerprint,
            "classification_source": before.classification_source,
            "canonical_status": before.canonical_status,
            "duplicate_group_id": before.duplicate_group_id,
            "linked_transaction_id": before.linked_transaction_id,
            "transfer_group_id": before.transfer_group_id,
            "source_priority": before.source_priority,
            "confidence": before.confidence,
            "excluded": before.excluded,
            "possible_duplicate": before.possible_duplicate,
            "reviewed": before.reviewed,
        }

        preview = client.post("/api/maintenance/card-competence/preview").json()
        revision = preview["financial_revision"]
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": revision,
                "reason": "Reparo P0 de teste",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == [
            {
                "transaction_id": transaction_id,
                "account_id": card,
                "competence_before": "2026-09",
                "competence_after": "2026-10",
            }
        ]
        assert set(body["periods_recomputed"]) == {"2026-09", "2026-10"}

        after = _get_transaction(session_factory, transaction_id)
        assert after.competence == "2026-10"
        for field, value in before_snapshot.items():
            assert getattr(after, field) == value, field


def test_apply_writes_per_transaction_and_batch_audit_trail():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        preview = client.post("/api/maintenance/card-competence/preview").json()
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Motivo auditável do reparo",
            },
        )
        assert response.status_code == 200, response.text
        batch_id = response.json()["batch_id"]

        with session_factory() as db:
            per_transaction = db.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == REPAIR_EVENT_TYPE,
                    AuditEvent.entity_id == transaction_id,
                )
            )
            assert per_transaction is not None
            assert per_transaction.before_state == {"competence": "2026-09"}
            assert per_transaction.after_state == {"competence": "2026-10"}
            assert per_transaction.reason == "Motivo auditável do reparo"
            assert per_transaction.trace_id == batch_id
            assert per_transaction.household_id == household_id

            batch_event = db.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == REPAIR_BATCH_EVENT_TYPE,
                    AuditEvent.entity_id == batch_id,
                )
            )
            assert batch_event is not None
            assert batch_event.reason == "Motivo auditável do reparo"


def test_apply_never_deletes_or_recreates_the_transaction():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        with session_factory() as db:
            count_before = len(db.scalars(select(Transaction)).all())

        preview = client.post("/api/maintenance/card-competence/preview").json()
        client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo sem apagar nada",
            },
        )
        with session_factory() as db:
            count_after = len(db.scalars(select(Transaction)).all())
            still_there = db.get(Transaction, transaction_id)
        assert count_after == count_before
        assert still_there is not None
        assert still_there.id == transaction_id


# ---------------------------------------------------------------------------
# 5. Apply -- atomicity and concurrency guards.
# ---------------------------------------------------------------------------


def test_apply_with_stale_revision_mutates_nothing():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        preview = client.post("/api/maintenance/card-competence/preview").json()
        stale_revision = preview["financial_revision"] + 999

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": stale_revision,
                "reason": "Deveria falhar",
            },
        )
        assert response.status_code == 409
        transaction = _get_transaction(session_factory, transaction_id)
        assert transaction.competence == "2026-09"
        with session_factory() as db:
            assert db.scalar(select(AuditEvent.id).where(AuditEvent.event_type == REPAIR_EVENT_TYPE)) is None


def test_apply_with_one_no_longer_candidate_id_fails_atomically_for_the_whole_batch():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        good_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            description="Candidata válida",
            competence="2026-09",
        )
        # Already-correct competence: this id will never be a candidate.
        never_candidate_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 1),
            amount=50,
            description="Já correta",
            competence="2026-09",
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [good_id, never_candidate_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Deveria falhar inteiro",
            },
        )
        assert response.status_code == 409
        assert _get_transaction(session_factory, good_id).competence == "2026-09"
        assert _get_transaction(session_factory, never_candidate_id).competence == "2026-09"


def test_apply_with_cross_household_transaction_id_fails_without_leaking_existence():
    client, session_factory = _client()
    with client:
        _setup_household(client, username="admin-a", household_name="Família A")
        household_a = _household_id(session_factory)
        card_a = _create_account(
            client, name="Cartão A", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_a = _category_id(client)

        other_household_id = _create_household_admin(
            session_factory, household_name="Família B", username="admin-b", password="senha-outra-familia"
        )
        with session_factory() as db:
            other_card = Account(
                household_id=other_household_id,
                name="Cartão B",
                account_type="credit_card",
                card_closing_day=2,
                card_due_day=9,
            )
            db.add(other_card)
            db.commit()
            other_card_id = other_card.id
        other_transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=other_household_id,
            account_id=other_card_id,
            category_id=None,
            booked_at=date(2026, 9, 15),
            amount=300,
            competence="2026-09",
        )

        own_transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_a,
            account_id=card_a,
            category_id=category_a,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        # Household A's admin session tries to repair a transaction id that
        # actually belongs to household B.
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [own_transaction_id, other_transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Tentativa de vazamento entre households",
            },
        )
        assert response.status_code == 409
        # Same generic "no longer a candidate" rejection this suite already
        # exercises for other reasons (stale data, already-correct
        # competence) -- never a distinguishable 404, and never a message
        # naming or counting only the foreign id, which would itself
        # confirm the other household's transaction exists.
        other_household_rejection = response.json()["detail"]
        nonexistent_id_rejection = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [own_transaction_id, str(uuid.uuid4())],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Comparison against a plain nonexistent id",
            },
        )
        assert nonexistent_id_rejection.status_code == 409
        assert other_household_rejection == nonexistent_id_rejection.json()["detail"]
        assert _get_transaction(session_factory, own_transaction_id).competence == "2026-09"
        assert _get_transaction(session_factory, other_transaction_id).competence == "2026-09"


# ---------------------------------------------------------------------------
# 6. Apply -- fail-closed duplicate and monthly-close blocks.
# ---------------------------------------------------------------------------


def test_apply_blocked_by_possible_duplicate_flag():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
            possible_duplicate=True,
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        candidate = _find_candidate(preview, transaction_id)
        assert candidate["blocked"] is True
        assert "possible_duplicate" in candidate["blocked_reasons"]

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Não deveria aplicar",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["blocked"][0]["transaction_id"] == transaction_id
        assert _get_transaction(session_factory, transaction_id).competence == "2026-09"


def test_apply_blocked_by_open_duplicate_group():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        group_id = _open_duplicate_group(session_factory, household_id=household_id, status="open")
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
            duplicate_group_id=group_id,
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        candidate = _find_candidate(preview, transaction_id)
        assert "duplicate_group_open" in candidate["blocked_reasons"]

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Não deveria aplicar",
            },
        )
        assert response.status_code == 422
        assert _get_transaction(session_factory, transaction_id).competence == "2026-09"


def test_apply_blocked_by_settled_duplicate_role():
    """A transaction can carry a *settled* canonical/supporting role from an
    already-resolved duplicate group and still be blocked: its identity as
    the canonical/supporting side of that comparison could be entangled with
    the competence signature being changed (see
    `app.services.duplicates.assess_duplicate`'s `competence_equal` signal),
    so this fails closed independent of `possible_duplicate`/group status."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        group_id = _open_duplicate_group(session_factory, household_id=household_id, status="resolved")
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
            possible_duplicate=False,
            canonical_status="supporting",
            duplicate_group_id=group_id,
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        candidate = _find_candidate(preview, transaction_id)
        assert "duplicate_role:supporting" in candidate["blocked_reasons"]

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Não deveria aplicar",
            },
        )
        assert response.status_code == 422
        assert _get_transaction(session_factory, transaction_id).competence == "2026-09"


def test_apply_blocked_by_trusted_monthly_close_on_old_or_new_period():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        _trust_monthly_close(session_factory, household_id=household_id, period="2026-10")

        preview = client.post("/api/maintenance/card-competence/preview").json()
        candidate = _find_candidate(preview, transaction_id)
        assert "monthly_close_trusted:2026-10" in candidate["blocked_reasons"]

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Não deveria aplicar",
            },
        )
        assert response.status_code == 422
        assert _get_transaction(session_factory, transaction_id).competence == "2026-09"


# ---------------------------------------------------------------------------
# 7. Mandatory financial-effect proof.
# ---------------------------------------------------------------------------


def test_apply_moves_full_financial_effect_between_periods_exactly_once():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        _set_cash_cap(client, monthly_cash_cap=1000)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            description="Compra com competência histórica errada",
            competence="2026-09",
        )

        before_old = client.get("/api/dashboard", params={"month": "2026-09"}).json()
        before_new = client.get("/api/dashboard", params={"month": "2026-10"}).json()
        assert before_old["spending"] == 200.0
        assert before_old["card_spending"] == 200.0
        assert before_old["remaining_cap"] == 800.0
        assert before_new["spending"] == 0.0
        assert before_new["card_spending"] == 0.0
        assert before_new["remaining_cap"] == 1000.0

        cards_before_old = client.get("/api/credit-cards/summary", params={"month": "2026-09"}).json()
        cards_before_new = client.get("/api/credit-cards/summary", params={"month": "2026-10"}).json()
        row_before_old = next(row for row in cards_before_old["rows"] if row["account_id"] == card)
        row_before_new = next(row for row in cards_before_new["rows"] if row["account_id"] == card)
        assert row_before_old["actual_spending"] == 200.0
        assert row_before_new["actual_spending"] == 0.0

        old_snapshot_before = _get_snapshot(session_factory, household_id=household_id, period="2026-09")
        assert old_snapshot_before.budget_usage == Decimal("200.00")
        assert old_snapshot_before.card_spend == Decimal("200.00")

        preview = client.post("/api/maintenance/card-competence/preview").json()
        apply_response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo do incidente P0 (cenário do Work Order)",
            },
        )
        assert apply_response.status_code == 200, apply_response.text

        after_old = client.get("/api/dashboard", params={"month": "2026-09"}).json()
        after_new = client.get("/api/dashboard", params={"month": "2026-10"}).json()
        assert after_old["spending"] == 0.0
        assert after_old["card_spending"] == 0.0
        assert after_old["remaining_cap"] == 1000.0
        assert after_new["spending"] == 200.0
        assert after_new["card_spending"] == 200.0
        assert after_new["remaining_cap"] == 800.0

        cards_after_old = client.get("/api/credit-cards/summary", params={"month": "2026-09"}).json()
        cards_after_new = client.get("/api/credit-cards/summary", params={"month": "2026-10"}).json()
        row_after_old = next(row for row in cards_after_old["rows"] if row["account_id"] == card)
        row_after_new = next(row for row in cards_after_new["rows"] if row["account_id"] == card)
        assert row_after_old["actual_spending"] == 0.0
        assert row_after_new["actual_spending"] == 200.0

        old_snapshot_after = _get_snapshot(session_factory, household_id=household_id, period="2026-09")
        new_snapshot_after = _get_snapshot(session_factory, household_id=household_id, period="2026-10")
        assert old_snapshot_after.budget_usage == Decimal("0.00")
        assert old_snapshot_after.card_spend == Decimal("0.00")
        assert old_snapshot_after.budget_remaining == Decimal("1000.00")
        assert new_snapshot_after.budget_usage == Decimal("200.00")
        assert new_snapshot_after.card_spend == Decimal("200.00")
        assert new_snapshot_after.budget_remaining == Decimal("800.00")
        # No stale `current` row left behind for either period.
        assert old_snapshot_after.status == "current"
        assert new_snapshot_after.status == "current"

        category_old = next(
            (row for row in old_snapshot_after.payload["category_spending"]), None
        )
        assert category_old is None or category_old["amount"] != 200.0
        category_new = next(
            row for row in new_snapshot_after.payload["category_spending"] if row["amount"] == 200.0
        )
        assert category_new is not None

        report = client.get("/api/reports", params={"end_month": "2026-10", "months": 2}).json()
        report_by_month = {item["month"]: item for item in report["monthly"]}
        assert report_by_month["2026-09"]["spending"] == 0.0
        assert report_by_month["2026-10"]["spending"] == 200.0

        # Consolidated total across both months: appears exactly once.
        assert report_by_month["2026-09"]["spending"] + report_by_month["2026-10"]["spending"] == 200.0


def test_apply_two_cards_different_banks_repaired_independently():
    """docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md: 'Adicionar cenário de
    duas compras em dois cartões diferentes para provar que o reparo não
    depende de nome de banco e usa somente o ciclo configurado.'"""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card_a = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        card_b = _create_account(
            client, name="Nubank Cartão", account_type="credit_card", card_closing_day=24, card_due_day=2
        )
        category_id = _category_id(client)

        tx_a = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card_a,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=150,
            description="Compra cartão A",
            competence="2026-09",
        )
        tx_b = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card_b,
            category_id=category_id,
            booked_at=date(2026, 9, 25),
            amount=90,
            description="Compra cartão B",
            competence="2026-09",
        )

        preview = client.post("/api/maintenance/card-competence/preview").json()
        candidate_a = _find_candidate(preview, tx_a)
        candidate_b = _find_candidate(preview, tx_b)
        assert candidate_a["competence_expected"] == "2026-10"
        assert candidate_b["competence_expected"] == "2026-11"

        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [tx_a, tx_b],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo simultâneo de dois cartões",
            },
        )
        assert response.status_code == 200, response.text
        assert _get_transaction(session_factory, tx_a).competence == "2026-10"
        assert _get_transaction(session_factory, tx_b).competence == "2026-11"


# ---------------------------------------------------------------------------
# 8. Logical rollback.
# ---------------------------------------------------------------------------


def test_rollback_restores_exact_prior_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        preview = client.post("/api/maintenance/card-competence/preview").json()
        apply_response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo a ser revertido",
            },
        )
        assert apply_response.status_code == 200, apply_response.text
        revision_after_apply = apply_response.json()["financial_revision"]
        assert _get_transaction(session_factory, transaction_id).competence == "2026-10"

        rollback_response = client.post(
            "/api/maintenance/card-competence/rollback",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": revision_after_apply,
                "reason": "Reparo aplicado por engano",
            },
        )
        assert rollback_response.status_code == 200, rollback_response.text
        body = rollback_response.json()
        assert body["reverted"] == [
            {
                "transaction_id": transaction_id,
                "competence_before_rollback": "2026-10",
                "competence_after_rollback": "2026-09",
            }
        ]

        restored = _get_transaction(session_factory, transaction_id)
        assert restored.competence == "2026-09"

        with session_factory() as db:
            rollback_event = db.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == ROLLBACK_EVENT_TYPE,
                    AuditEvent.entity_id == transaction_id,
                )
            )
            assert rollback_event is not None
            assert rollback_event.before_state == {"competence": "2026-10"}
            assert rollback_event.after_state == {"competence": "2026-09"}

        # Financial effect actually moved back too, not just the raw field.
        old_snapshot = _get_snapshot(session_factory, household_id=household_id, period="2026-09")
        new_snapshot = _get_snapshot(session_factory, household_id=household_id, period="2026-10")
        assert old_snapshot.card_spend == Decimal("200.00")
        assert new_snapshot.card_spend == Decimal("0.00")


def test_rollback_with_stale_revision_mutates_nothing():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        preview = client.post("/api/maintenance/card-competence/preview").json()
        apply_response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo original",
            },
        )
        assert apply_response.status_code == 200, apply_response.text

        response = client.post(
            "/api/maintenance/card-competence/rollback",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": apply_response.json()["financial_revision"] + 999,
                "reason": "Deveria falhar",
            },
        )
        assert response.status_code == 409
        assert _get_transaction(session_factory, transaction_id).competence == "2026-10"


def test_rollback_fails_when_transaction_changed_since_the_repair_it_targets():
    """Criterion 17: 'rollback stale/conflitante falha sem sobrescrever
    mudança posterior.' Simulates a second, independent mutation of the
    transaction's competence after the repair this rollback request names,
    made directly (as a later, unrelated admin action would) -- the rollback
    must refuse rather than blindly restore the original pre-repair value
    over that newer change."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 15),
            amount=200,
            competence="2026-09",
        )
        preview = client.post("/api/maintenance/card-competence/preview").json()
        apply_response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo original",
            },
        )
        assert apply_response.status_code == 200, apply_response.text
        revision_after_apply = apply_response.json()["financial_revision"]

        # A later, independent change to the same field -- not through this
        # engine -- moves the transaction away from the state the repair's
        # own audit trail describes.
        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            transaction.competence = "2026-11"
            db.commit()

        response = client.post(
            "/api/maintenance/card-competence/rollback",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": revision_after_apply,
                "reason": "Deveria falhar sem sobrescrever a mudança posterior",
            },
        )
        assert response.status_code in (409, 422)
        # The later change must survive untouched.
        assert _get_transaction(session_factory, transaction_id).competence == "2026-11"


def test_rollback_rejects_transaction_with_no_repair_history():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=date(2026, 9, 1),
            amount=90,
            competence="2026-09",  # already correct, never repaired
        )
        response = client.post(
            "/api/maintenance/card-competence/rollback",
            json={"transaction_ids": [transaction_id], "expected_financial_revision": 0, "reason": "Sem reparo"},
        )
        assert response.status_code == 409
        assert _get_transaction(session_factory, transaction_id).competence == "2026-09"


# ---------------------------------------------------------------------------
# 9. INV-017 itself, before and after the repair.
# ---------------------------------------------------------------------------


def test_apply_makes_inv017_pass_for_the_repaired_transaction():
    """docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md, "Critérios de aceite
    financeiros obrigatórios": 'INV-017 passa'. Feeds the real persisted
    `competence` (before and after the repair) and the real canonical engine
    output into the exact same INV-017 evaluator
    `tests/test_financial_invariants.py` trusts -- never a second,
    P0-private notion of what INV-017 means."""

    from app.services.financial_invariants import InvariantContext, InvariantScope, InvariantStatus
    from app.services.invariant_registry import evaluate_invariant

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        card = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        booked_at = date(2026, 9, 15)
        transaction_id = _insert_legacy_transaction(
            session_factory,
            household_id=household_id,
            account_id=card,
            category_id=category_id,
            booked_at=booked_at,
            amount=200,
            competence="2026-09",
        )

        def _inv017_status() -> "InvariantStatus":
            transaction = _get_transaction(session_factory, transaction_id)
            with session_factory() as db:
                account = db.get(Account, card)
                canonical = card_invoice_competence(account, booked_at)
            return evaluate_invariant(
                "INV-017",
                InvariantContext(
                    facts={
                        "canonical_competence": canonical,
                        "counted_competence": transaction.competence,
                    },
                    scope=InvariantScope.TRANSACTION,
                    entity_type="transaction",
                    entity_id=transaction_id,
                    period="2026-09",
                    trace_id="trace-p0-inv017-test",
                ),
            ).status

        assert _inv017_status().name == "FAIL"

        preview = client.post("/api/maintenance/card-competence/preview").json()
        response = client.post(
            "/api/maintenance/card-competence/apply",
            json={
                "transaction_ids": [transaction_id],
                "expected_financial_revision": preview["financial_revision"],
                "reason": "Reparo para satisfazer INV-017",
            },
        )
        assert response.status_code == 200, response.text
        assert _inv017_status().name == "PASS"
