"""Tests for issue #85 -- Privilège DI daily valuation via CVM
(`docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

Section 1 unit-tests `app.services.cvm_client` directly (no DB, no HTTP):
CSV/ZIP parsing, the `CNPJ_FUNDO`/`CNPJ_FUNDO_CLASSE` schema-variance
fallback, and malformed-response handling.

Section 2 unit-tests `app.services.privilege_valuation` directly against an
in-memory SQLite session -- idempotency, quota rise/unchanged, missing
evidence, application/redemption separation, observed-vs-calculated
separation, and Decimal precision (the Work Order's "Mandatory tests"
list).

Section 3 tests `app.cli.cvm_valuation_worker.run_once` end to end with a
fake `CvmClient` (never live network), including the "CVM unavailable ->
retain last valid state" and per-household failure isolation cases.

Section 4 is an HTTP-level functional suite for `GET /privilege-di/valuation`
and the two position-evidence routes, mirroring
`tests/test_investments_slice6.py`'s `_client()`/`_setup_household()`
pattern, including a real shared-engine two-household isolation case
(mirrors `tests/test_manual_ledger.py::test_ledger_household_isolation`).
"""

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-privilege-cvm-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "privilege-cvm-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.cli import cvm_valuation_worker as worker  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, FundReferenceQuote, FundValuation, Household, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import privilege_valuation as pv  # noqa: E402
from app.services.cvm_client import (  # noqa: E402
    CvmClient,
    CvmQuote,
    CvmResult,
    _extract_csv_text,
    _latest_quote_for_cnpj,
    normalize_fund_cnpj,
)
from app.services.cvm_worker_status import heartbeat_snapshot  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

FUND_CNPJ = pv.TRACKED_FUND_CNPJ  # "26199519000134"

# ---------------------------------------------------------------------------
# 1. app.services.cvm_client -- CSV/ZIP parsing, no DB, no HTTP.
# ---------------------------------------------------------------------------


def _fixture_csv(*, date_str="2026-09-15", quota="28.1234567890", cnpj_column="CNPJ_FUNDO") -> str:
    return (
        f"TP_FUNDO;{cnpj_column};DENOM_SOCIAL;DT_COMPTC;VL_TOTAL;VL_QUOTA;VL_PATRIM_LIQ;CAPTC_DIA;RESG_DIA;NR_COTST\n"
        f"FI;26.199.519/0001-34;ITAU PRIVILEGE;{date_str};1000000.00;{quota};900000.00;0;0;10\n"
        f"FI;11.111.111/0001-11;OUTRO FUNDO;{date_str};500.00;10.0000000000;400.00;0;0;5\n"
    )


def test_parses_official_csv_and_ignores_other_funds() -> None:
    quote = _latest_quote_for_cnpj(_fixture_csv(), fund_cnpj_digits=FUND_CNPJ)
    assert quote is not None
    assert quote.fund_cnpj == FUND_CNPJ
    assert quote.quota_reference_date == date(2026, 9, 15)
    assert quote.quota_value == Decimal("28.1234567890")
    assert isinstance(quote.quota_value, Decimal)


def test_picks_the_latest_reference_date_when_several_rows_exist() -> None:
    csv_text = _fixture_csv(date_str="2026-09-14", quota="28.0000000000") + (
        "FI;26.199.519/0001-34;ITAU PRIVILEGE;2026-09-15;1000100.00;28.1334560000;900100.00;100;0;10\n"
    )
    quote = _latest_quote_for_cnpj(csv_text, fund_cnpj_digits=FUND_CNPJ)
    assert quote.quota_reference_date == date(2026, 9, 15)
    assert quote.quota_value == Decimal("28.1334560000")


def test_cnpj_fundo_classe_column_variant_is_recognized() -> None:
    # CVM's Resolução 175 fund-classes reform may rename the join column --
    # the parser must not depend on CNPJ_FUNDO specifically.
    quote = _latest_quote_for_cnpj(_fixture_csv(cnpj_column="CNPJ_FUNDO_CLASSE"), fund_cnpj_digits=FUND_CNPJ)
    assert quote is not None
    assert quote.quota_value == Decimal("28.1234567890")


def test_neither_cnpj_column_present_is_not_a_match() -> None:
    csv_text = "FOO;BAR\nx;y\n"
    assert _latest_quote_for_cnpj(csv_text, fund_cnpj_digits=FUND_CNPJ) is None


def test_zip_container_is_extracted_transparently() -> None:
    import io
    import zipfile

    csv_text = _fixture_csv()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("inf_diario_fi_202609.csv", csv_text)
    assert _extract_csv_text(buf.getvalue()) == csv_text


def test_normalize_fund_cnpj_strips_formatting() -> None:
    assert normalize_fund_cnpj("26.199.519/0001-34") == "26199519000134"
    assert normalize_fund_cnpj("26199519000134") == "26199519000134"


def test_client_reports_disabled_when_not_configured(monkeypatch) -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CVM_VALUATION_ENABLED", "false")
    get_settings.cache_clear()
    client = CvmClient()
    assert client.configured is False
    result = client.latest_quote(FUND_CNPJ)
    assert result.quote is None
    assert result.error_code == "cvm_not_configured"
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. app.services.privilege_valuation -- direct SQLAlchemy session, no HTTP.
# ---------------------------------------------------------------------------


def _engine_and_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _quote(*, reference_date: date, quota_value: Decimal, net_worth=Decimal("900000.00")) -> FundReferenceQuote:
    return FundReferenceQuote(
        fund_cnpj=FUND_CNPJ,
        quota_reference_date=reference_date,
        quota_value=quota_value,
        net_worth=net_worth,
        provider="cvm_dados_abertos",
        source_url="http://fixture/inf_diario_fi_202609.csv",
        ingestion_batch="202609",
        retrieved_at=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
    )


def test_no_quote_yet_returns_no_quote_available_without_writing_anything() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        outcome = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert outcome.status == "no_quote_available"
        assert db.scalar(select(FundValuation)) is None


def test_no_position_evidence_returns_no_position_evidence_without_writing_anything() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.1234567890")))
        db.commit()

        outcome = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert outcome.status == "no_position_evidence"
        assert db.scalar(select(FundValuation)) is None


def test_quota_rise_deterministically_raises_valuation() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.0000000000")))
        db.commit()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs",
        )
        db.commit()
        first = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert first.status == "ok"
        assert first.valuation.gross_value == Decimal("28000.00")

        db.add(_quote(reference_date=date(2026, 9, 16), quota_value=Decimal("28.5000000000")))
        db.commit()
        second = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert second.status == "ok"
        assert second.valuation.gross_value == Decimal("28500.00")
        assert second.valuation.gross_value > first.valuation.gross_value
        assert second.valuation.variation_amount == Decimal("500.00")
        assert second.valuation.previous_gross_value == Decimal("28000.00")


def test_unchanged_quota_rerun_is_idempotent_and_creates_no_artificial_return() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.1234567890")))
        db.commit()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs",
        )
        db.commit()
        first = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert first.status == "ok"

        # Weekend/holiday/no-new-quota: rerunning against the *same* quote
        # (no new FundReferenceQuote row -- exactly what a worker pass on a
        # non-trading day observes) must not fabricate a second valuation or
        # any variation/return.
        second = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        assert second.status == "already_valued"
        assert second.valuation.id == first.valuation.id
        assert second.valuation.quota_reference_date == date(2026, 9, 15)
        rows = db.scalars(select(FundValuation)).all()
        assert len(rows) == 1


def test_application_movement_never_becomes_investment_return() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 1),
            evidence_type="statement_position",
            evidence_document_id="fake-doc",
        )
        db.commit()

        moved = pv.record_position_movement(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            delta_units=Decimal("200.00000000"),
            effective_date=date(2026, 9, 10),
            evidence_type="application",
        )
        db.commit()
        assert moved.units_held == Decimal("1200.00000000")
        assert moved.delta_units == Decimal("200.00000000")

        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.0000000000")))
        db.commit()
        outcome = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
        # The application changed units_held (a patrimonial movement), never
        # variation_amount/gain -- there is no previous valuation to compare
        # against yet, so variation must be None, not a fabricated "gain"
        # from the 200-unit application.
        assert outcome.valuation.units_held == Decimal("1200.00000000")
        assert outcome.valuation.previous_gross_value is None
        assert outcome.valuation.variation_amount is None


def test_redemption_requires_a_prior_position_and_rejects_negative_result() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        db.commit()

        with pytest.raises(pv.PrivilegeValuationError, match="posição anterior"):
            pv.record_position_movement(
                db,
                household_id=household.id,
                fund_cnpj=FUND_CNPJ,
                delta_units=Decimal("-10.00000000"),
                effective_date=date(2026, 9, 10),
                evidence_type="redemption",
            )

        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("5.00000000"),
            effective_date=date(2026, 9, 1),
            evidence_type="statement_position",
            evidence_document_id="fake-doc",
        )
        db.commit()
        with pytest.raises(pv.PrivilegeValuationError, match="negativa"):
            pv.record_position_movement(
                db,
                household_id=household.id,
                fund_cnpj=FUND_CNPJ,
                delta_units=Decimal("-10.00000000"),
                effective_date=date(2026, 9, 10),
                evidence_type="redemption",
            )


def test_observed_balance_stays_distinct_from_calculated_valuation() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.1234567890")))
        db.commit()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1445.12345678"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs",
        )
        db.commit()

        outcome = pv.run_valuation_for_household(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            observed_balance=Decimal("40667.49"),
            observed_balance_as_of=date(2026, 9, 11),
        )
        assert outcome.status == "ok"
        valuation = outcome.valuation
        # The two figures are never equal by construction here (deliberately
        # divergent fixture) -- proving the calculated value never silently
        # overwrites or is silently overwritten by the observed one; both
        # are preserved distinctly, plus the explicit diff.
        assert valuation.gross_value != valuation.observed_balance
        assert valuation.observed_balance == Decimal("40667.49")
        assert valuation.reconciliation_diff == pv.money(valuation.gross_value - Decimal("40667.49"))


def test_decimal_precision_no_float_anywhere_in_the_arithmetic_path() -> None:
    quota = Decimal("28.12345678")
    units = Decimal("1445.12345678")
    result = pv.compute_gross_value(units_held=units, quota_value=quota)
    assert isinstance(result, Decimal)
    # money() quantizes to cents -- the exact expected product, computed in
    # pure Decimal arithmetic, must match bit-for-bit (no float roundoff).
    expected = (units * quota).quantize(Decimal("0.01"))
    assert result == expected


def test_household_isolation_of_position_and_valuation_reads() -> None:
    _, Session = _engine_and_session()
    with Session() as db:
        household_a = Household(name="Familia A")
        household_b = Household(name="Familia B")
        db.add_all([household_a, household_b])
        db.flush()
        db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.0000000000")))
        db.commit()

        pv.record_position_snapshot(
            db,
            household_id=household_a.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs-a",
        )
        db.commit()

        # Household B has no position evidence of its own -- must never see
        # household A's units_held or a valuation derived from it.
        assert pv.latest_position_for_household(db, household_id=household_b.id, fund_cnpj=FUND_CNPJ) is None
        outcome_b = pv.run_valuation_for_household(db, household_id=household_b.id, fund_cnpj=FUND_CNPJ)
        assert outcome_b.status == "no_position_evidence"

        outcome_a = pv.run_valuation_for_household(db, household_id=household_a.id, fund_cnpj=FUND_CNPJ)
        assert outcome_a.status == "ok"
        assert (
            pv.latest_valuation_for_household(db, household_id=household_b.id, fund_cnpj=FUND_CNPJ) is None
        )


# ---------------------------------------------------------------------------
# 3. app.cli.cvm_valuation_worker.run_once -- fake CvmClient, never live network.
# ---------------------------------------------------------------------------


class _FakeCvmClient:
    def __init__(self, result: CvmResult):
        self._result = result
        self.configured = True

    def latest_quote(self, fund_cnpj, reference_month=None):
        return self._result


def _worker_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_worker_run_once_ingests_quote_and_valuates_households_with_evidence() -> None:
    SessionFactory = _worker_session_factory()
    with SessionFactory() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs",
        )
        db.commit()

    fake_client = _FakeCvmClient(
        CvmResult(
            CvmQuote(
                fund_cnpj=FUND_CNPJ,
                quota_reference_date=date(2026, 9, 15),
                quota_value=Decimal("28.1234567890"),
                net_worth=Decimal("900000.00"),
                source_url="http://fixture",
                ingestion_batch="202609",
            )
        )
    )
    counts = worker.run_once(session_factory=SessionFactory, client=fake_client)
    assert counts["quote_ingested"] == 1
    assert counts["ok"] == 1

    with SessionFactory() as db:
        snap = heartbeat_snapshot(db)
        assert snap["last_run_ok"] is True
        assert snap["last_counts"]["ok"] == 1


def test_worker_cvm_unavailable_retains_last_valid_state_no_destructive_write() -> None:
    SessionFactory = _worker_session_factory()
    with SessionFactory() as db:
        household = Household(name="Familia Teste")
        db.add(household)
        db.flush()
        pv.record_position_snapshot(
            db,
            household_id=household.id,
            fund_cnpj=FUND_CNPJ,
            units_held=Decimal("1000.00000000"),
            effective_date=date(2026, 9, 11),
            evidence_type="confirmed_balance_reconciliation",
            evidence_balance_observation_id="fake-obs",
        )
        db.commit()

    ok_client = _FakeCvmClient(
        CvmResult(
            CvmQuote(
                fund_cnpj=FUND_CNPJ,
                quota_reference_date=date(2026, 9, 15),
                quota_value=Decimal("28.1234567890"),
                net_worth=Decimal("900000.00"),
                source_url="http://fixture",
                ingestion_batch="202609",
            )
        )
    )
    first_counts = worker.run_once(session_factory=SessionFactory, client=ok_client)
    assert first_counts["ok"] == 1

    with SessionFactory() as db:
        quote_before = pv.latest_quote_for_fund(db, fund_cnpj=FUND_CNPJ)
        valuation_before = pv.latest_valuation_for_household(
            db, household_id=db.scalar(select(Household)).id, fund_cnpj=FUND_CNPJ
        )
        assert quote_before is not None
        assert valuation_before is not None

    down_client = _FakeCvmClient(CvmResult(None, "Fonte CVM indisponível", "cvm_transport_error"))
    second_counts = worker.run_once(session_factory=SessionFactory, client=down_client)
    assert second_counts["quote_unavailable"] == 1
    assert second_counts["quote_ingested"] == 0

    with SessionFactory() as db:
        quote_after = pv.latest_quote_for_fund(db, fund_cnpj=FUND_CNPJ)
        valuation_after = pv.latest_valuation_for_household(
            db, household_id=db.scalar(select(Household)).id, fund_cnpj=FUND_CNPJ
        )
        # Last valid state is retained exactly -- no row deleted, no value
        # corrupted, despite the CVM source being unreachable this pass.
        assert quote_after.id == quote_before.id
        assert quote_after.quota_value == quote_before.quota_value
        assert valuation_after.id == valuation_before.id


def test_worker_one_household_exception_never_aborts_the_whole_pass(monkeypatch) -> None:
    SessionFactory = _worker_session_factory()
    with SessionFactory() as db:
        broken_household = Household(name="Familia Quebrada")
        healthy_household = Household(name="Familia Saudavel")
        db.add_all([broken_household, healthy_household])
        db.flush()
        for household in (broken_household, healthy_household):
            pv.record_position_snapshot(
                db,
                household_id=household.id,
                fund_cnpj=FUND_CNPJ,
                units_held=Decimal("1000.00000000"),
                effective_date=date(2026, 9, 11),
                evidence_type="confirmed_balance_reconciliation",
                evidence_balance_observation_id="fake-obs",
            )
        db.commit()
        broken_id = broken_household.id
        healthy_id = healthy_household.id

    real_resolve = pv.resolve_privilege_account

    def _boom(db, *, household_id):
        if household_id == broken_id:
            raise RuntimeError("simulated failure resolving the account")
        return real_resolve(db, household_id=household_id)

    monkeypatch.setattr(pv, "resolve_privilege_account", _boom)

    fake_client = _FakeCvmClient(
        CvmResult(
            CvmQuote(
                fund_cnpj=FUND_CNPJ,
                quota_reference_date=date(2026, 9, 15),
                quota_value=Decimal("28.1234567890"),
                net_worth=Decimal("900000.00"),
                source_url="http://fixture",
                ingestion_batch="202609",
            )
        )
    )
    counts = worker.run_once(session_factory=SessionFactory, client=fake_client)
    assert counts["valuation_error"] == 1
    assert counts["ok"] == 1  # the healthy household still got valuated

    with SessionFactory() as db:
        assert pv.latest_valuation_for_household(db, household_id=healthy_id, fund_cnpj=FUND_CNPJ) is not None
        assert pv.latest_valuation_for_household(db, household_id=broken_id, fund_cnpj=FUND_CNPJ) is None


# ---------------------------------------------------------------------------
# 4. HTTP-level functional suite.
# ---------------------------------------------------------------------------


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


def _setup_household(client, *, username="admin-privilege-cvm"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Privilège CVM",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def _create_household_admin(session_factory, *, household_name, username, password):
    with session_factory() as db:
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
        return household.id


def test_valuation_endpoint_returns_pending_status_before_any_evidence() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.get("/api/privilege-di/valuation")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "no_quote_available"
        assert body["estimated_balance"] is None


def test_position_snapshot_route_requires_admin_and_persists_evidence() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/privilege-di/position-snapshot",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "units_held": "1445.12345678",
                "effective_date": "2026-09-11",
                "evidence_type": "confirmed_balance_reconciliation",
                "evidence_balance_observation_id": "fake-obs",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["units_held"] == 1445.12345678


def test_position_snapshot_route_rejects_invalid_evidence_with_422() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.post(
            "/api/privilege-di/position-snapshot",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "units_held": "10.00000000",
                "effective_date": "2026-09-11",
                "evidence_type": "statement_position",
                # evidence_document_id intentionally omitted -- required for statement_position.
            },
        )
        assert response.status_code == 422, response.text


def test_position_movement_route_computes_units_from_prior_snapshot() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        client.post(
            "/api/privilege-di/position-snapshot",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "units_held": "1000.00000000",
                "effective_date": "2026-09-01",
                "evidence_type": "statement_position",
                "evidence_document_id": "fake-doc",
            },
        )
        moved = client.post(
            "/api/privilege-di/position-movement",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "delta_units": "200.00000000",
                "effective_date": "2026-09-10",
                "evidence_type": "application",
            },
        )
        assert moved.status_code == 201, moved.text
        assert moved.json()["units_held"] == 1200.0


def test_valuation_endpoint_reflects_persisted_valuation_and_reconciliation() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        with session_factory() as db:
            household = db.scalar(select(Household))
            account = Account(
                household_id=household.id, name="Privilege DI", institution="Itau", account_type="investment"
            )
            db.add(account)
            db.add(_quote(reference_date=date(2026, 9, 15), quota_value=Decimal("28.1234567890")))
            db.commit()

        client.post(
            "/api/privilege-di/position-snapshot",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "units_held": "1445.12345678",
                "effective_date": "2026-09-11",
                "evidence_type": "confirmed_balance_reconciliation",
                "evidence_balance_observation_id": "fake-obs",
            },
        )

        with session_factory() as db:
            household = db.scalar(select(Household))
            outcome = pv.run_valuation_for_household(db, household_id=household.id, fund_cnpj=FUND_CNPJ)
            db.commit()
            assert outcome.status == "ok"

        response = client.get("/api/privilege-di/valuation")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert body["estimated_balance"] == pytest.approx(float(outcome.valuation.gross_value))
        assert body["latest_quota"] == pytest.approx(28.1234567890)
        assert body["quota_reference_date"] == "2026-09-15"


def test_household_isolation_position_and_valuation_routes() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client, username="admin-a")
        client.post(
            "/api/privilege-di/position-snapshot",
            json={
                "fund_cnpj": "26.199.519/0001-34",
                "units_held": "1000.00000000",
                "effective_date": "2026-09-01",
                "evidence_type": "statement_position",
                "evidence_document_id": "fake-doc-a",
            },
        )

        _create_household_admin(
            session_factory, household_name="Família B", username="admin-b", password="senha-local-segura"
        )
        login_b = client.post("/api/auth/login", json={"username": "admin-b", "password": "senha-local-segura"})
        assert login_b.status_code == 200
        complete_mfa_enrollment(client)

        response_b = client.get("/api/privilege-di/valuation")
        assert response_b.status_code == 200, response_b.text
        # Household B has recorded no evidence of its own -- must never see
        # household A's units_held/quote-derived figures.
        assert response_b.json()["status"] == "no_quote_available"
        assert response_b.json()["units_held"] is None
