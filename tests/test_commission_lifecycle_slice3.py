"""Tests for October Go-Live Slice 3 (P0 #87) -- Commission receipt and the
recurring-income (Kelly's salary) forecast reconciliation.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_3.md`.

Rebaseline §8.3: "Comissões nunca entram como receita PREVISTA
automaticamente" (already true before this slice -- `POST /commissions` is
the only creation path, admin-only) and "a comissão só passa a existir na
visão financeira quando efetivamente registrada/recebida". This slice closes
the other half of that loop: once a commission is marked received, it must
stop being projected as PREVISTO a second time (the real credit already
exists as a fact elsewhere).

Rebaseline §8.4: Kelly's configured recurring salary
(`FinancialProfile.monthly_salary_net`) reconciles against a real income
`Transaction` for the same competence instead of being added twice.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-commission-slice3-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "commission-slice3-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Household, Transaction  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402


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


def _setup_household(client, *, username="admin-commission-slice3"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Comissão HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def _profile_payload(**overrides) -> dict:
    payload = {
        "monthly_salary_net": "0.00",
        "monthly_cash_cap": "0.00",
        "emergency_floor": "0.00",
        "food_allowance": "0.00",
        "meal_allowance_daily": "0.00",
        "workdays_month": 22,
        "investment_name": "Reserva DI",
        "investment_balance": "0.00",
        "investment_gross_annual_rate": "0.10",
        "investment_income_tax_rate": "0.15",
        "projection_end": "2027-12-01",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Commission receipt.
# ---------------------------------------------------------------------------


def test_commission_receive_endpoint_marks_received_and_sets_date() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/commissions",
            json={"description": "Comissão setembro", "expected_date": "2026-11-01", "gross_amount": "8000.00"},
        )
        assert created.status_code == 201, created.text
        commission_id = created.json()["id"]

        listed_before = next(item for item in client.get("/api/commissions").json() if item["id"] == commission_id)
        assert listed_before["status"] == "expected"
        assert listed_before["received_date"] is None
        assert listed_before["financial_state"] == "PREVISTO"

        receive = client.post(f"/api/commissions/{commission_id}/receive", json={"received_date": "2026-11-05"})
        assert receive.status_code == 200, receive.text
        assert receive.json()["status"] == "received"
        assert receive.json()["received_date"] == "2026-11-05"

        listed_after = next(item for item in client.get("/api/commissions").json() if item["id"] == commission_id)
        assert listed_after["status"] == "received"
        assert listed_after["received_date"] == "2026-11-05"
        assert listed_after["financial_state"] == "REALIZADO"


def test_commission_receive_defaults_date_to_today() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/commissions",
            json={"description": "Comissão outubro", "expected_date": "2026-11-01", "gross_amount": "5000.00"},
        )
        commission_id = created.json()["id"]

        receive = client.post(f"/api/commissions/{commission_id}/receive", json={})
        assert receive.status_code == 200, receive.text
        assert receive.json()["received_date"] == date.today().isoformat()


def test_commission_receive_rejects_double_receive() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/commissions",
            json={"description": "Comissão única", "expected_date": "2026-11-01", "gross_amount": "5000.00"},
        )
        commission_id = created.json()["id"]

        first = client.post(f"/api/commissions/{commission_id}/receive", json={})
        assert first.status_code == 200
        second = client.post(f"/api/commissions/{commission_id}/receive", json={})
        assert second.status_code == 409


def test_commission_receive_rejects_missing_commission() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.post("/api/commissions/missing/receive", json={})
        assert response.status_code == 404


def test_unreceived_commission_never_enters_forecast_projection() -> None:
    """INV-031 (October Go-Live Slice 3, Round 2 of PR #91's review):
    rebaseline §8.3 -- "Comissões nunca entram como receita PREVISTA
    automaticamente ... não deve inflar projeções futuras por expectativa."
    A pending, not-yet-received commission contributes exactly zero to every
    scenario's projected income, before *and* after it exists -- there is no
    explicit opt-in mechanism, so the only correct automatic contribution is
    none."""

    client, _ = _client()
    with client:
        _setup_household(client)

        before = client.get("/api/forecast")
        assert before.status_code == 200
        commission_before = sum(
            Decimal(str(row["commission_expected"])) for row in before.json()["rows"]
        )
        assert commission_before == Decimal("0.00")

        created = client.post(
            "/api/commissions",
            json={"description": "Comissão a receber", "expected_date": "2027-01-01", "gross_amount": "10000.00"},
        )
        assert created.status_code == 201, created.text

        after = client.get("/api/forecast")
        assert after.status_code == 200
        commission_after = sum(
            Decimal(str(row["commission_expected"])) for row in after.json()["rows"]
        )
        assert commission_after == Decimal("0.00")


def test_received_commission_still_excluded_from_forecast_projection() -> None:
    """INV-029: a commission already marked received never contributes to
    the projected income either -- it is REALIZADO (the real credit already
    exists elsewhere), and (per INV-031 above) it never contributed while
    pending in the first place, so nothing changes about the forecast when
    it is received -- only `GET /commissions`' own `financial_state` label
    flips from PREVISTO to REALIZADO."""

    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/commissions",
            json={"description": "Comissão a receber", "expected_date": "2027-01-01", "gross_amount": "10000.00"},
        )
        commission_id = created.json()["id"]

        receive = client.post(f"/api/commissions/{commission_id}/receive", json={"received_date": "2027-01-05"})
        assert receive.status_code == 200, receive.text

        after = client.get("/api/forecast")
        assert after.status_code == 200
        commission_after = sum(
            Decimal(str(row["commission_expected"])) for row in after.json()["rows"]
        )
        assert commission_after == Decimal("0.00")

        listed = client.get("/api/commissions")
        assert listed.status_code == 200
        (row,) = [item for item in listed.json() if item["id"] == commission_id]
        assert row["financial_state"] == "REALIZADO"


def test_cancelled_commission_cannot_be_marked_received() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/commissions",
            json={"description": "Comissão a cancelar", "expected_date": "2026-11-01", "gross_amount": "5000.00"},
        )
        commission_id = created.json()["id"]
        deleted = client.delete(f"/api/commissions/{commission_id}")
        assert deleted.status_code == 200

        receive = client.post(f"/api/commissions/{commission_id}/receive", json={})
        assert receive.status_code == 404


# ---------------------------------------------------------------------------
# Recurring income (Kelly's salary) reconciliation inside `/forecast`.
# ---------------------------------------------------------------------------


def test_forecast_uses_flat_estimate_when_no_real_credit_exists() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        profile = client.put("/api/profile", json=_profile_payload(monthly_salary_net="5000.00"))
        assert profile.status_code == 200, profile.text

        forecast = client.get("/api/forecast")
        assert forecast.status_code == 200
        first_row = forecast.json()["rows"][0]
        assert Decimal(str(first_row["salary"])) == Decimal("5000.00")
        assert first_row["salary_financial_state"] == "PREVISTO"
        assert first_row["salary_reconciled_transaction_id"] is None


def test_forecast_uses_realized_salary_amount_when_reconciled() -> None:
    """INV-030: once a real income `Transaction` matching the configured
    recurring salary exists for a projected competence, that period's
    `salary` figure comes from the real transaction -- never the flat
    estimate summed on top of it."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        profile = client.put("/api/profile", json=_profile_payload(monthly_salary_net="5000.00"))
        assert profile.status_code == 200, profile.text

        forecast_before = client.get("/api/forecast")
        first_period = forecast_before.json()["rows"][0]["month"]

        account = client.post("/api/accounts", json={"name": "Conta Kelly", "account_type": "checking"})
        account_id = account.json()["id"]

        year, month = (int(part) for part in first_period.split("-"))
        with session_factory() as db:
            household_id = db.scalar(select(Household)).id
            txn = Transaction(
                household_id=household_id,
                account_id=account_id,
                booked_at=date(year, month, 5),
                occurred_at=date(year, month, 5),
                competence=first_period,
                description="Salário",
                normalized_description="SALARIO",
                amount=Decimal("5000.01"),
                transaction_type="income",
                fingerprint=f"kellysalary{uuid.uuid4().hex}".ljust(64, "0")[:64],
                source_priority=50,
                confidence=Decimal("1"),
                reviewed=True,
            )
            db.add(txn)
            db.commit()
            transaction_id = txn.id

        forecast_after = client.get("/api/forecast")
        assert forecast_after.status_code == 200
        first_row = forecast_after.json()["rows"][0]
        assert first_row["month"] == first_period
        assert Decimal(str(first_row["salary"])) == Decimal("5000.01")
        assert first_row["salary_financial_state"] == "REALIZADO"
        assert first_row["salary_reconciled_transaction_id"] == transaction_id
