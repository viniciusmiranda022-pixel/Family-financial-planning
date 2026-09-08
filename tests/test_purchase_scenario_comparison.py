"""Tests for `POST /api/purchases/scenario-comparison`.

Work Order: `docs/WORK_ORDER_PURCHASE_SCENARIO_COMPARISON.md`.

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_manual_financial_flows.py` (a `StaticPool` in-memory SQLite engine
with `dependency_overrides[get_db]`, one session per request) -- deliberately
not the shared file-backed SQLite of `tests/test_api.py`. Two-household
isolation reuses the exact pattern from
`tests/test_card_payment_reconciliation.py::test_http_endpoints_authorize_isolate_and_audit`:
a second `TestClient(app)` sharing the same engine, logged in as an admin
seeded directly via the DB (since `/api/auth/setup` is a global one-time
bootstrap, not per-household).
"""

import os
import uuid
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-scenario-comparison-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "purchase-scenario-comparison-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.api import _advisor_payment  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Commission,
    Document,
    FinancialSnapshot,
    Household,
    IntegrityFinding,
    IntegrityRun,
    Obligation,
    PayrollRecord,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services.finance import (  # noqa: E402
    ForecastInput,
    add_months,
    amortized_installment_payment,
    build_forecast,
    money,
    month_key,
)
from app.services.projection_validator import validate_projection  # noqa: E402

ENDPOINT = "/api/purchases/scenario-comparison"


# ---------------------------------------------------------------------------
# Shared fixtures / helpers (mirrors tests/test_manual_financial_flows.py)
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


def _setup_household(client, *, household_name="Família Comparação", username="admin-comparacao", password="senha-local-segura"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": household_name,
            "name": "Admin",
            "username": username,
            "password": password,
        },
    )
    assert setup.status_code == 201, setup.text
    return setup.json()


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


def _create_account(client, *, name, account_type="checking", last_four=None):
    payload = {"name": name, "account_type": account_type}
    if last_four is not None:
        payload["last_four"] = last_four
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _non_system_category_id(client) -> str:
    categories = client.get("/api/categories").json()
    return next(
        item["id"]
        for item in categories
        if item["name"] not in {"Conciliação", "Transferência patrimonial", "Transferência interna", "Receitas"}
    )


def _set_profile(client, **overrides):
    payload = {
        "monthly_salary_net": 5000,
        "monthly_cash_cap": 0,
        "emergency_floor": 1000,
        "food_allowance": 0,
        "meal_allowance_daily": 0,
        "workdays_month": 21,
        "investment_name": "Reserva DI",
        "investment_balance": 20000,
        "investment_gross_annual_rate": 0,
        "investment_income_tax_rate": 0,
        "projection_end": "2027-06-01",
    }
    payload.update(overrides)
    response = client.put("/api/profile", json=payload)
    assert response.status_code == 200, response.text
    return payload


def _confirm_opening_balance(client, *, account_id, amount, as_of_date="2026-09-01"):
    response = client.post(
        "/api/account-balances",
        json={
            "account_id": account_id,
            "amount": amount,
            "as_of_date": as_of_date,
            "observation_type": "opening",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _row_counts(session_factory):
    with session_factory() as db:
        return {
            "transactions": db.scalar(select(func.count()).select_from(Transaction)),
            "obligations": db.scalar(select(func.count()).select_from(Obligation)),
            "commissions": db.scalar(select(func.count()).select_from(Commission)),
            "payroll_records": db.scalar(select(func.count()).select_from(PayrollRecord)),
            "documents": db.scalar(select(func.count()).select_from(Document)),
            "integrity_runs": db.scalar(select(func.count()).select_from(IntegrityRun)),
            "integrity_findings": db.scalar(select(func.count()).select_from(IntegrityFinding)),
            "financial_snapshots": db.scalar(select(func.count()).select_from(FinancialSnapshot)),
        }


# ---------------------------------------------------------------------------
# 1 + 2: two alternatives with different impacts, each one reproducible by
# an independently assembled ForecastInput/build_forecast/validate_projection
# call (Projection Engine/Validator parity -- no parallel formula).
# ---------------------------------------------------------------------------


def test_two_alternatives_differ_and_match_manual_projection_engine():
    import datetime

    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(
            client,
            monthly_salary_net=5000,
            monthly_cash_cap=0,
            emergency_floor=1000,
            investment_balance=20000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2027-06-01",
        )

        forecast_before = client.get("/api/forecast")
        assert forecast_before.status_code == 200
        forecast_summary = forecast_before.json()["summary"]

        payload = {
            "alternatives": [
                {
                    "label": "A vista",
                    "price": 6000,
                    "down_payment": 0,
                    "installment_count": 1,
                },
                {
                    "label": "Parcelado 6x",
                    "price": 6000,
                    "down_payment": 0,
                    "installment_count": 6,
                    "monthly_interest_rate": 0.02,
                },
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["projection_start_month"] == "2026-10"
        assert data["projection_end_month"] == "2027-06"

        alt_by_label = {item["label"]: item for item in data["alternatives"]}
        cash = alt_by_label["A vista"]
        financed = alt_by_label["Parcelado 6x"]

        # Cash: installment_count == 1 lumps the whole price into a single
        # payment due at purchase_month (documented convention).
        assert cash["monthly_payment"] == 6000.0
        assert cash["total_purchase_cost"] == 6000.0
        assert cash["candidate_installment_schedule"] == [{"month": "2026-10", "amount": 6000.0}]

        expected_monthly, expected_total = amortized_installment_payment(
            Decimal("6000"), 6, Decimal("0.02")
        )
        assert financed["monthly_payment"] == float(expected_monthly)
        assert financed["total_purchase_cost"] == float(expected_total)
        expected_months = [month_key(add_months(datetime.date(2026, 10, 1), offset)) for offset in range(6)]
        assert [row["month"] for row in financed["candidate_installment_schedule"]] == expected_months
        assert all(row["amount"] == float(expected_monthly) for row in financed["candidate_installment_schedule"])

        # Different impacts: financing costs more overall (interest), so its
        # ending balance across the full horizon must end up lower than
        # paying cash, and the two summaries must not be identical.
        assert cash["scenarios"]["delayed"] != financed["scenarios"]["delayed"]
        assert financed["scenarios"]["delayed"]["final_balance"] < cash["scenarios"]["delayed"]["final_balance"]
        assert float(expected_total) > 6000.0  # interest actually applied

        # --- Parity with the canonical Projection Engine/Validator ---
        # Independently assemble the exact ForecastInput the endpoint's
        # canonical path would build for this household (no obligations,
        # commissions, payroll extras or persisted installments exist here)
        # plus the financed alternative's own schedule, and prove
        # build_forecast/validate_projection reproduce the API's numbers.
        schedule = {
            month_key(add_months(datetime.date(2026, 10, 1), offset)): expected_monthly
            for offset in range(6)
        }
        forecast_input = ForecastInput(
            start_month=datetime.date(2026, 10, 1),
            end_month=datetime.date(2027, 6, 1),
            starting_balance=Decimal(str(forecast_summary["starting_balance"])),
            monthly_salary=Decimal("5000"),
            monthly_cash_cap=Decimal("0"),
            monthly_investment_rate=Decimal("0"),
            obligations={},
            installments=schedule,
            payroll_extras={},
            commissions=(),
            starting_uncovered_deficit=Decimal(str(forecast_summary["current_uncovered_deficit"])),
            safety_floor=Decimal("1000"),
        )
        rows = build_forecast(forecast_input)
        validation = validate_projection(forecast_input, rows)
        assert validation.valid, validation.mismatches

        for scenario in ("no_commission", "delayed", "expected"):
            balances = [row[f"balance_{scenario}"] for row in rows]
            deficits = [row[f"uncovered_deficit_{scenario}"] for row in rows]
            distances = [row[f"distance_to_floor_{scenario}"] for row in rows]
            manual_summary = {
                "final_balance": float(money(balances[-1])),
                "minimum_balance": float(money(min(balances))),
                "final_uncovered_deficit": float(money(deficits[-1])),
                "maximum_uncovered_deficit": float(money(max(deficits))),
                "minimum_distance_to_floor": float(money(min(distances))),
                "crosses_safety_floor": min(distances) < 0,
                "has_uncovered_deficit": max(deficits) > 0,
            }
            assert financed["scenarios"][scenario] == manual_summary


# ---------------------------------------------------------------------------
# 3: deficit larger than liquidity -- balance is floored at zero, never
# negative, and the shortfall shows up as uncovered deficit instead.
# ---------------------------------------------------------------------------


def test_deficit_larger_than_liquidity_never_yields_negative_balance():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(
            client,
            monthly_salary_net=0,
            monthly_cash_cap=0,
            emergency_floor=100,
            investment_balance=1000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2026-12-01",
        )

        payload = {
            "alternatives": [
                {"label": "Estouro", "price": 10000, "down_payment": 0, "installment_count": 1},
                {"label": "Pequena", "price": 50, "down_payment": 0, "installment_count": 1},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        blown = next(item for item in data["alternatives"] if item["label"] == "Estouro")

        for scenario in ("no_commission", "delayed", "expected"):
            summary = blown["scenarios"][scenario]
            assert summary["minimum_balance"] >= 0
            assert summary["final_balance"] >= 0
            assert summary["final_balance"] == 0
            # 10000 purchase against 1000 of liquidity and no income at all
            # to ever pay it back: the shortfall is fully uncovered debt --
            # a genuine fact, not the removed `viable` verdict (engineering
            # review on this PR: a single cross-scenario boolean is never
            # published again; each scenario reports its own facts).
            assert summary["final_uncovered_deficit"] == 9000.0
            assert summary["maximum_uncovered_deficit"] == 9000.0
            assert summary["has_uncovered_deficit"] is True
            assert summary["crosses_safety_floor"] is True
        assert "viable" not in blown


# ---------------------------------------------------------------------------
# 4: the safety floor is a reference, never an artificial block -- a
# below-floor alternative still returns 200 with viable=False.
# ---------------------------------------------------------------------------


def test_alternative_below_safety_floor_returns_200_not_blocked():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(
            client,
            monthly_salary_net=0,
            monthly_cash_cap=0,
            emergency_floor=20000,
            investment_balance=5000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2026-12-01",
        )

        payload = {
            "alternatives": [
                {"label": "Modesta", "price": 100, "down_payment": 0, "installment_count": 1},
                {"label": "Outra modesta", "price": 200, "down_payment": 0, "installment_count": 1},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        for item in data["alternatives"]:
            # This is exactly the case the engineering review on this PR
            # flagged: crossing the floor with a genuinely positive balance
            # and zero real deficit must be reported as a plain fact, never
            # coerced into an invented "not viable"/veto verdict -- there is
            # no `viable` field at all any more, only independent facts.
            assert "viable" not in item
            assert item["scenarios"]["delayed"]["crosses_safety_floor"] is True
            assert item["scenarios"]["delayed"]["minimum_distance_to_floor"] < 0
            # The purchase itself never drained liquidity to zero here --
            # only the floor (a reference target) is missed, not an actual
            # deficit -- proving the floor is advisory, not a hard block.
            assert item["scenarios"]["delayed"]["final_uncovered_deficit"] == 0.0
            assert item["scenarios"]["delayed"]["has_uncovered_deficit"] is False


# ---------------------------------------------------------------------------
# 5: persisted future installments + a new hypothetical alternative must sum
# without double counting.
# ---------------------------------------------------------------------------


def test_persisted_installments_and_new_alternative_do_not_double_count():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(
            client,
            monthly_salary_net=5000,
            monthly_cash_cap=0,
            emergency_floor=0,
            investment_balance=20000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2027-06-01",
        )
        card_id = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        real_installment = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-05",
                "description": "Notebook parcelado",
                "amount": 90,
                "movement_type": "expense",
                "account_id": card_id,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        )
        assert real_installment.status_code == 201, real_installment.text

        # Baseline (no hypothetical alternative) must already reflect the
        # persisted installment's own remaining schedule (2026-10, 2026-11 --
        # installment_current=1 of 3, so 2 months remain).
        baseline_only = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {"label": "A", "price": 10, "down_payment": 0, "installment_count": 1},
                    {"label": "B", "price": 20, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert baseline_only.status_code == 200, baseline_only.text
        baseline_schedule = baseline_only.json()["baseline"]["commitment_schedule"]
        assert [row["card_installments"] for row in baseline_schedule] == [90.0, 90.0, 0.0, 0.0, 0.0, 0.0]

        # New alternative: 1200 over 4 interest-free installments, starting
        # AT purchase_month (2026-10 by default, the first month it affects
        # the projection) -> 2026-10..2027-01 at 300 each.
        payload = {
            "alternatives": [
                {
                    "label": "Notebook novo",
                    "price": 1200,
                    "down_payment": 0,
                    "installment_count": 4,
                    "monthly_interest_rate": 0,
                },
                {"label": "Irrelevante", "price": 10, "down_payment": 0, "installment_count": 1},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        alt = next(item for item in data["alternatives"] if item["label"] == "Notebook novo")

        # Combined month -> amount, with no double counting:
        # 2026-10: 90 (persisted) + 300 (candidate, its 1st installment) = 390
        # 2026-11: 90 (persisted) + 300 (candidate, its 2nd installment) = 390
        # 2026-12, 2027-01: 300 (candidate only, its 3rd/4th installments)
        # 2027-02, 2027-03: 0
        combined = [row["card_installments"] for row in alt["commitment_schedule"]]
        assert combined == [390.0, 390.0, 300.0, 300.0, 0.0, 0.0]

        # The other alternative in the same request must not see the first
        # alternative's candidate schedule either (each is evaluated in
        # isolation against the household's real persisted facts only): its
        # own single-installment purchase (10, due at 2026-10) adds to the
        # persisted 90 in that month, but never picks up "Notebook novo"'s
        # 300/month schedule.
        irrelevant = next(item for item in data["alternatives"] if item["label"] == "Irrelevante")
        irrelevant_schedule = [row["card_installments"] for row in irrelevant["commitment_schedule"]]
        assert irrelevant_schedule == [100.0, 90.0, 0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# 6: an untrusted projection (INV-018 gate) must be reported explicitly,
# fail-closed -- never silently promoted to a positive recommendation.
# ---------------------------------------------------------------------------


def test_untrusted_projection_is_reported_fail_closed_not_hidden():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(
            client,
            monthly_salary_net=5000,
            monthly_cash_cap=0,
            emergency_floor=1000,
            investment_balance=20000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2027-06-01",
        )
        # Deliberately never confirm an AccountBalanceObservation: a freshly
        # created household's opening balance is the legacy profile fallback,
        # which is the natural, real-world case where
        # `balance_evidence_trusted` is False.

        payload = {
            "alternatives": [
                {"label": "A", "price": 500, "down_payment": 0, "installment_count": 1},
                {"label": "B", "price": 1000, "down_payment": 0, "installment_count": 5},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["baseline"]["trusted_for_projection"] is False
        # The engine/validator themselves still genuinely agree (no
        # mismatch) -- untrusted here is strictly about balance evidence,
        # never fabricated as a mismatch that isn't real.
        assert data["baseline"]["projection_formula_trusted"] is True
        assert data["baseline"]["validator_mismatches"] == 0
        assert data["baseline"]["integrity_status"] == "pass"

        for alt in data["alternatives"]:
            assert alt["trusted_for_projection"] is False
            assert alt["projection_formula_trusted"] is True
            # No `viable` field exists at all (engineering review on this
            # PR): each scenario's `crosses_safety_floor`/
            # `has_uncovered_deficit` are distinct, independently computed
            # facts and must never be coerced by the trust flag in either
            # direction.
            assert "viable" not in alt
            for scenario in ("no_commission", "delayed", "expected"):
                assert isinstance(alt["scenarios"][scenario]["crosses_safety_floor"], bool)
                assert isinstance(alt["scenarios"][scenario]["has_uncovered_deficit"], bool)
            # The untrusted state must never vanish behind a 4xx/5xx or an
            # incomplete payload -- every field is still present and honest.
            assert set(alt["scenarios"]) == {"no_commission", "delayed", "expected"}


# ---------------------------------------------------------------------------
# 7: the comparison must never mutate any historical fact or leave an audit
# trail, because nothing here was actually confirmed.
# ---------------------------------------------------------------------------


def test_comparison_never_mutates_persisted_facts_or_integrity_history():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        _set_profile(client)
        checking_id = _create_account(client, name="Conta Corrente")
        card_id = _create_account(client, name="Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        assert client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-05",
                "description": "Compra parcelada",
                "amount": 90,
                "movement_type": "expense",
                "account_id": card_id,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        ).status_code == 201
        assert client.post(
            "/api/obligations",
            json={"name": "Aluguel", "due_date": "2026-10-10", "amount": 2000},
        ).status_code == 201
        assert client.post(
            "/api/commissions",
            json={"description": "Comissão", "expected_date": "2027-01-15", "gross_amount": 1000},
        ).status_code == 201
        assert client.post(
            "/api/payroll",
            json={
                "person_name": "Admin",
                "competence": "2026-12-01",
                "payment_date": "2026-12-20",
                "payroll_kind": "13_first",
                "net_amount": 2500,
            },
        ).status_code == 201

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id))
            db.add(
                Document(
                    household_id=household_id,
                    account_id=checking_id,
                    original_name="extrato.csv",
                    document_type="bank_statement",
                    sha256="d" * 64,
                    encrypted_path="documents/extrato.bin",
                    status="imported",
                    record_count=1,
                )
            )
            db.commit()

        # A prior GET /forecast call to exercise the same snapshot/integrity
        # path once for real, so the "no mutation" assertion below is about
        # the comparison endpoint specifically, not an artifact of nothing
        # ever having run.
        assert client.get("/api/forecast").status_code == 200

        before = _row_counts(session_factory)

        payload = {
            "alternatives": [
                {"label": "A", "price": 500, "down_payment": 0, "installment_count": 1},
                {"label": "B", "price": 2000, "down_payment": 500, "installment_count": 6, "monthly_interest_rate": 0.015},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text

        after = _row_counts(session_factory)
        assert after == before


# ---------------------------------------------------------------------------
# 8: authentication and household isolation.
# ---------------------------------------------------------------------------


def test_requires_authentication():
    client, _ = _client()
    with client:
        payload = {
            "alternatives": [
                {"label": "A", "price": 500, "down_payment": 0, "installment_count": 1},
                {"label": "B", "price": 1000, "down_payment": 0, "installment_count": 3},
            ]
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 401


def test_household_isolation_obligations_do_not_leak_or_affect_other_household():
    client_a, session_factory = _client()
    with client_a:
        _setup_household(client_a, household_name="Família A", username="admin-a", password="senha-local-segura")
        _set_profile(
            client_a,
            monthly_salary_net=5000,
            monthly_cash_cap=0,
            emergency_floor=0,
            investment_balance=20000,
            investment_gross_annual_rate=0,
            investment_income_tax_rate=0,
            projection_end="2027-06-01",
        )
        assert client_a.post(
            "/api/obligations",
            json={"name": "Aluguel", "due_date": "2026-10-10", "amount": 2000},
        ).status_code == 201

        _create_household_admin(
            session_factory, household_name="Família B", username="admin-b", password="senha-local-segura"
        )
        with TestClient(app) as client_b:
            login_b = client_b.post(
                "/api/auth/login", json={"username": "admin-b", "password": "senha-local-segura"}
            )
            assert login_b.status_code == 200
            _set_profile(
                client_b,
                monthly_salary_net=5000,
                monthly_cash_cap=0,
                emergency_floor=0,
                investment_balance=20000,
                investment_gross_annual_rate=0,
                investment_income_tax_rate=0,
                projection_end="2027-06-01",
            )
            assert client_b.get("/api/obligations").json() == []

            payload = {
                "alternatives": [
                    {"label": "A", "price": 100, "down_payment": 0, "installment_count": 1},
                    {"label": "B", "price": 500, "down_payment": 0, "installment_count": 2},
                ]
            }
            response_a = client_a.post(ENDPOINT, json=payload)
            response_b = client_b.post(ENDPOINT, json=payload)
            assert response_a.status_code == 200, response_a.text
            assert response_b.status_code == 200, response_b.text
            data_a = response_a.json()
            data_b = response_b.json()

            # Same profile/starting balance for both households, but only A
            # carries the 2000 obligation -- it must show up exclusively in
            # A's numbers, never leaking into (or being missing without
            # explanation from) B's.
            for scenario in ("no_commission", "delayed", "expected"):
                delta = (
                    data_b["baseline"]["scenarios"][scenario]["final_balance"]
                    - data_a["baseline"]["scenarios"][scenario]["final_balance"]
                )
                assert round(delta, 2) == 2000.0
            for label in ("A", "B"):
                alt_a = next(item for item in data_a["alternatives"] if item["label"] == label)
                alt_b = next(item for item in data_b["alternatives"] if item["label"] == label)
                delta = (
                    alt_b["scenarios"]["delayed"]["final_balance"]
                    - alt_a["scenarios"]["delayed"]["final_balance"]
                )
                assert round(delta, 2) == 2000.0


# ---------------------------------------------------------------------------
# 9: payload validation.
# ---------------------------------------------------------------------------


def test_rejects_fewer_than_two_alternatives():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client)
        response = client.post(
            ENDPOINT,
            json={"alternatives": [{"label": "Única", "price": 100, "down_payment": 0, "installment_count": 1}]},
        )
        assert response.status_code == 422


def test_rejects_duplicate_labels():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client)
        response = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {"label": "Repetido", "price": 100, "down_payment": 0, "installment_count": 1},
                    {"label": "Repetido", "price": 200, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert response.status_code == 422


def test_rejects_down_payment_greater_than_price():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client)
        response = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {"label": "Inválida", "price": 100, "down_payment": 200, "installment_count": 1},
                    {"label": "Válida", "price": 100, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert response.status_code == 422


def test_rejects_purchase_month_outside_projection_window():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client, projection_end="2027-06-01")

        # Before the projection window (the current month is already closed
        # by the snapshot the projection starts from).
        too_early = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {
                        "label": "Cedo demais",
                        "price": 100,
                        "down_payment": 0,
                        "installment_count": 1,
                        "purchase_month": "2026-09",
                    },
                    {"label": "Válida", "price": 100, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert too_early.status_code == 422

        # After the projection window.
        too_late = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {
                        "label": "Tarde demais",
                        "price": 100,
                        "down_payment": 0,
                        "installment_count": 1,
                        "purchase_month": "2028-01",
                    },
                    {"label": "Válida", "price": 100, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert too_late.status_code == 422


def test_rejects_installment_count_and_interest_rate_out_of_bounds():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client)

        base = {"label": "Válida", "price": 100, "down_payment": 0, "installment_count": 1}

        for bad_count in (0, 121):
            response = client.post(
                ENDPOINT,
                json={
                    "alternatives": [
                        {**base, "label": "A", "installment_count": bad_count},
                        {**base, "label": "B"},
                    ]
                },
            )
            assert response.status_code == 422, bad_count

        for bad_rate in (-0.01, 0.31):
            response = client.post(
                ENDPOINT,
                json={
                    "alternatives": [
                        {**base, "label": "A", "installment_count": 2, "monthly_interest_rate": bad_rate},
                        {**base, "label": "B"},
                    ]
                },
            )
            assert response.status_code == 422, bad_rate


# ---------------------------------------------------------------------------
# Edge cases: installment_count=1 with/without down payment, count>1 with/
# without interest, down_payment == price (financed_amount == 0).
# ---------------------------------------------------------------------------


def test_candidate_schedule_edge_cases():
    client, _ = _client()
    with client:
        _setup_household(client)
        _set_profile(client, projection_end="2027-01-01")

        # (a) count == 1, no down payment: pure cash, whole price due at
        # purchase_month.
        # (b) count == 1, WITH a down payment: purchase_month is,
        # unconditionally, the first month the purchase affects the
        # projection (see `_purchase_scenario_candidate_schedule`'s
        # docstring) -- the down payment and the financed remainder's one
        # installment both fall in that same month, summing to the same
        # total as (a). There is no inferred gap to a "next month" for the
        # financed part; that was the exact invented calendar semantics an
        # engineering review on this PR rejected.
        response_1 = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {"label": "CountOneNoDown", "price": 1000, "down_payment": 0, "installment_count": 1},
                    {"label": "CountOneWithDown", "price": 1000, "down_payment": 400, "installment_count": 1},
                ]
            },
        )
        assert response_1.status_code == 200, response_1.text
        data_1 = {item["label"]: item for item in response_1.json()["alternatives"]}
        no_down = data_1["CountOneNoDown"]
        with_down = data_1["CountOneWithDown"]
        assert no_down["monthly_payment"] == 1000.0
        assert with_down["monthly_payment"] == 600.0
        assert no_down["total_purchase_cost"] == with_down["total_purchase_cost"] == 1000.0
        assert no_down["candidate_installment_schedule"] == [{"month": "2026-10", "amount": 1000.0}]
        assert with_down["candidate_installment_schedule"] == [{"month": "2026-10", "amount": 1000.0}]

        # (c) count > 1, no interest. (d) count > 1, with interest.
        response_2 = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {
                        "label": "NoInterest",
                        "price": 1200,
                        "down_payment": 0,
                        "installment_count": 4,
                        "monthly_interest_rate": 0,
                    },
                    {
                        "label": "WithInterest",
                        "price": 1200,
                        "down_payment": 0,
                        "installment_count": 4,
                        "monthly_interest_rate": 0.03,
                    },
                ]
            },
        )
        assert response_2.status_code == 200, response_2.text
        data_2 = {item["label"]: item for item in response_2.json()["alternatives"]}
        no_interest = data_2["NoInterest"]
        with_interest = data_2["WithInterest"]
        assert no_interest["monthly_payment"] == 300.0
        assert with_interest["monthly_payment"] > 300.0
        assert with_interest["total_purchase_cost"] > no_interest["total_purchase_cost"]
        expected_monthly, expected_total = amortized_installment_payment(Decimal("1200"), 4, Decimal("0.03"))
        assert with_interest["monthly_payment"] == float(expected_monthly)
        assert with_interest["total_purchase_cost"] == float(expected_total)
        # No down payment, count > 1: `purchase_month` (2026-10) is the
        # month of the FIRST installment, not a month before it -- explicit
        # regression for the engineering review's request #4 (no month
        # displaced by an undocumented inference either with or without a
        # down payment).
        assert [row["month"] for row in no_interest["candidate_installment_schedule"]] == [
            "2026-10",
            "2026-11",
            "2026-12",
            "2027-01",
        ]

        # (e.5) count > 1 WITH a down payment: the down payment and the
        # first financed installment both fall at purchase_month (summed in
        # the same entry), and the remaining installments follow monthly --
        # no gap between the down payment's month and the first financed
        # installment.
        response_2b = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {
                        "label": "WithDownMulti",
                        "price": 1000,
                        "down_payment": 200,
                        "installment_count": 4,
                        "monthly_interest_rate": 0,
                    },
                    {"label": "Filler", "price": 10, "down_payment": 0, "installment_count": 1},
                ]
            },
        )
        assert response_2b.status_code == 200, response_2b.text
        with_down_multi = next(
            item for item in response_2b.json()["alternatives"] if item["label"] == "WithDownMulti"
        )
        # financed = 1000 - 200 = 800 over 4 installments of 200 each.
        assert with_down_multi["monthly_payment"] == 200.0
        assert with_down_multi["candidate_installment_schedule"] == [
            {"month": "2026-10", "amount": 400.0},  # 200 down payment + 200 (1st installment)
            {"month": "2026-11", "amount": 200.0},
            {"month": "2026-12", "amount": 200.0},
            {"month": "2027-01", "amount": 200.0},
        ]

        # (e) down_payment == price -> financed_amount == 0, regardless of
        # installment_count (single lump sum equal to the down payment,
        # zero monthly payment either way).
        response_3 = client.post(
            ENDPOINT,
            json={
                "alternatives": [
                    {"label": "DownEqualsPriceOne", "price": 800, "down_payment": 800, "installment_count": 1},
                    {"label": "DownEqualsPriceMulti", "price": 800, "down_payment": 800, "installment_count": 5},
                ]
            },
        )
        assert response_3.status_code == 200, response_3.text
        data_3 = {item["label"]: item for item in response_3.json()["alternatives"]}
        one = data_3["DownEqualsPriceOne"]
        multi = data_3["DownEqualsPriceMulti"]
        assert one["monthly_payment"] == 0.0
        assert multi["monthly_payment"] == 0.0
        assert one["total_purchase_cost"] == multi["total_purchase_cost"] == 800.0
        assert one["candidate_installment_schedule"] == [{"month": "2026-10", "amount": 800.0}]
        assert multi["candidate_installment_schedule"] == [{"month": "2026-10", "amount": 800.0}]


# ---------------------------------------------------------------------------
# 10: Advisor regression -- the extracted `amortized_installment_payment`
# must still be exactly what `_advisor_payment` (the free-text purchase
# parser used by the chat) computes. The full end-to-end advisor regression
# already lives in tests/test_api.py::test_complete_local_financial_flow --
# see the final report for confirmation it still passes; this is a fast,
# focused unit check on the extraction itself.
# ---------------------------------------------------------------------------


def test_advisor_payment_still_uses_the_shared_amortization_formula():
    result = _advisor_payment("Comprei em 6x com juros de 2% ao mês", Decimal("1000"))
    expected_monthly, expected_total = amortized_installment_payment(Decimal("1000"), 6, Decimal("0.02"))
    assert result["complete"] is True
    assert result["monthly_payment"] == float(expected_monthly)
    assert result["total_cost"] == float(expected_total)

    cash_result = _advisor_payment("Comprei à vista", Decimal("250"))
    assert cash_result["complete"] is True
    assert cash_result["mode"] == "cash"
    assert cash_result["monthly_payment"] == 250.0
    assert cash_result["total_cost"] == 250.0

    no_interest_result = _advisor_payment("Comprei em 10x sem juros", Decimal("500"))
    expected_ni_monthly, expected_ni_total = amortized_installment_payment(Decimal("500"), 10, Decimal("0"))
    assert no_interest_result["complete"] is True
    assert no_interest_result["monthly_payment"] == float(expected_ni_monthly)
    assert no_interest_result["total_cost"] == float(expected_ni_total)
