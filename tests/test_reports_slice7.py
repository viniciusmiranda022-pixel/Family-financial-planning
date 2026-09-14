"""Tests for October Go-Live Slice 7 -- `Gastos & Economia` + `Relatórios`.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_7.md`.
Normative source: `docs/OCTOBER_GO_LIVE_REBASELINE.md` §§14, 15, 16, 17.

Covers the new sections `_build_report_payload` (`GET /reports`) publishes
in this slice -- `patrimony`, `investments`, `card_invoices`, `obligations`,
`financial_states`, `ledger`, `monthly_categories`/`internal_transfers` --
and the new `GET /spending-economy` endpoint, against the exact "Testes
obrigatórios" the Work Order lists: no double counting, REALIZADO/
COMPROMETIDO/PREVISTO separation, patrimony = cash + `current_value` only,
investments/obligations/card-invoice reuse, household isolation, Dashboard
<-> Reports parity, commission never PREVISTO automatically, and the Codex
"Seus dados / Referências externas / Análise / Recomendação" contract
(including divergence signalling, never silent override).

Uses the same fully isolated in-memory-SQLite/`TestClient` pattern every
other HTTP-level test file in this suite uses (e.g.
`tests/test_report_export.py`, `tests/test_card_invoice_lifecycle.py`).
"""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "reports-slice7-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-reports-slice7-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, Household, Transaction, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.card_invoice_lifecycle import close_invoice, get_or_sync_invoice  # noqa: E402
from app.services.codex_client import CodexAdvisorClient, CodexResult  # noqa: E402
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


def _setup_household(client, session_factory, *, household_name: str, username: str) -> str:
    password = "senha-slice7-muito-segura-1"
    with session_factory() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Administrador",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
        household_id = household.id
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    complete_mfa_enrollment(client)
    return household_id


def _create_account(client, **overrides) -> dict:
    payload = {"name": "Conta Corrente", "institution": "Itaú", "account_type": "checking", "owner_label": "Família"}
    payload.update(overrides)
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _add_expense(client, *, account_id: str, booked_at: str, amount: str, description: str, category_name: str = "Mercado") -> dict:
    response = client.post(
        "/api/transactions",
        json={
            "booked_at": booked_at,
            "description": description,
            "amount": amount,
            "movement_type": "expense",
            "account_id": account_id,
            "category_name": category_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_income(client, *, account_id: str, booked_at: str, amount: str, description: str) -> dict:
    response = client.post(
        "/api/transactions",
        json={
            "booked_at": booked_at,
            "description": description,
            "amount": amount,
            "movement_type": "income",
            "account_id": account_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_investment(client, **overrides) -> dict:
    payload = {
        "name": "Studio",
        "historical_cost": "20000.00",
        "current_value": "35000.00",
        "expected_receivable_value": "45000.00",
        "valuation_date": "2026-08-01",
    }
    payload.update(overrides)
    response = client.post("/api/investments", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


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


def _purchase(session_factory, *, household_id, account_id, amount: str, booked_at: date, competence: str, category_id: str, description: str = "Compra") -> str:
    with session_factory() as db:
        txn = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=booked_at,
            occurred_at=booked_at,
            competence=competence,
            description=description,
            normalized_description=description.upper(),
            amount=-abs(Decimal(amount)),
            transaction_type="expense",
            fingerprint=f"slice7{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
        )
        db.add(txn)
        db.commit()
        return txn.id


def _category_id(session_factory, *, household_id, name="Compras, casa e vestuário") -> str:
    with session_factory() as db:
        existing = db.scalar(select(Category).where(Category.household_id == household_id, Category.name == name))
        if existing:
            return existing.id
        category = Category(household_id=household_id, name=name)
        db.add(category)
        db.commit()
        return category.id


# ---------------------------------------------------------------------------
# Patrimônio/investimentos: reuse, not a second calculation; parity with
# `GET /dashboard`; never sums historical_cost/expected_receivable_value.
# ---------------------------------------------------------------------------


def test_report_patrimony_matches_dashboard_and_uses_current_value_only() -> None:
    client, session_factory = _client()
    try:
        with client:
            _setup_household(client, session_factory, household_name="Família Patrimônio", username="admin-patrimonio")
            account = _create_account(client)
            _add_income(client, account_id=account["id"], booked_at="2026-08-05", amount="4000.00", description="Salário")
            _add_expense(client, account_id=account["id"], booked_at="2026-08-10", amount="1000.00", description="Mercado")
            _create_investment(
                client,
                name="Studio",
                historical_cost="20000.00",
                current_value="35000.00",
                expected_receivable_value="45000.00",
                valuation_date="2026-08-01",
            )

            dashboard = client.get("/api/dashboard?month=2026-08").json()
            report = client.get("/api/reports?end_month=2026-08&months=1").json()

            # Same cash+investments composition `/dashboard` already
            # publishes under `noncanonical.patrimony` -- never a third,
            # independently-derived figure (rebaseline §13.1 item 4).
            assert report["patrimony"]["value"] == dashboard["noncanonical"]["patrimony"]["value"]
            assert report["patrimony"]["cash_component"] == dashboard["noncanonical"]["patrimony"]["cash_component"]
            assert (
                report["patrimony"]["investments_component"]
                == dashboard["noncanonical"]["patrimony"]["investments_component"]
            )
            # rebaseline §16.2: only `current_value` (35000), never
            # `historical_cost` (20000) nor `expected_receivable_value`
            # (45000) folded in.
            assert report["patrimony"]["investments_component"] == 35000.0
            assert report["investments"][0]["current_value"] == 35000.0
            assert report["investments"][0]["historical_cost"] == 20000.0
            assert report["investments"][0]["gain_current"] == 15000.0
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Cartões/obrigações: COMPROMETIDO enquanto não liquidado, sem duplicar
# gasto; REALIZADO após pagamento.
# ---------------------------------------------------------------------------


def test_report_card_invoice_and_obligation_are_comprometido_until_paid() -> None:
    client, session_factory = _client()
    try:
        with client:
            household_id = _setup_household(
                client, session_factory, household_name="Família Cartão", username="admin-cartao"
            )
            checking = _create_account(client, name="Conta Corrente")
            card = _create_account(
                client, name="Cartão", account_type="credit_card", card_closing_day=10, card_due_day=17
            )
            category_id = _category_id(session_factory, household_id=household_id)
            _purchase(
                session_factory,
                household_id=household_id,
                account_id=card["id"],
                amount="900.00",
                booked_at=date(2026, 8, 3),
                competence="2026-08",
                category_id=category_id,
            )
            with session_factory() as db:
                account = db.get(Account, card["id"])
                invoice = get_or_sync_invoice(
                    db, household_id=household_id, account=account, competence="2026-08", as_of=date(2026, 8, 3)
                )
                invoice = close_invoice(
                    db, household_id=household_id, invoice_id=invoice.id, as_of=date(2026, 8, 11)
                )
                db.commit()
                invoice_id = invoice.id

            obligation = client.post(
                "/api/obligations",
                json={
                    "name": "Chácara",
                    "due_date": "2026-08-20",
                    "amount": 1500,
                    "recurrence_months": 0,
                    "occurrence_count": 1,
                    "category": "property",
                },
            )
            assert obligation.status_code == 201, obligation.text

            before = client.get("/api/reports?end_month=2026-08&months=1").json()
            invoice_row = next(item for item in before["card_invoices"] if item["id"] == invoice_id)
            assert invoice_row["status"] in {"closed", "partially_paid"}
            assert invoice_row["financial_state"] == "COMPROMETIDO"
            obligation_row = next(item for item in before["obligations"] if item["name"] == "Chácara")
            assert obligation_row["financial_state"] == "COMPROMETIDO"
            assert before["financial_states"]["comprometido"]["card_invoices_outstanding"] == 900.0
            assert before["financial_states"]["comprometido"]["obligations_pending"] == 1500.0
            # The purchase itself is the only economic expense counted --
            # closing/COMPROMETIDO status never adds a second one.
            assert before["summary"]["total_spending"] == 900.0

            pay = client.post(
                f"/api/card-invoices/{invoice_id}/pay",
                json={
                    "paying_account_id": checking["id"],
                    "amount": "900.00",
                    "booked_at": "2026-08-15",
                    "description": "Pagamento fatura",
                    "confirmed": True,
                },
            )
            assert pay.status_code == 201, pay.text

            after = client.get("/api/reports?end_month=2026-08&months=1").json()
            invoice_row_after = next(item for item in after["card_invoices"] if item["id"] == invoice_id)
            assert invoice_row_after["status"] == "paid"
            assert Decimal(invoice_row_after["outstanding_balance"]) == Decimal("0")
            assert after["financial_states"]["comprometido"]["card_invoices_outstanding"] == 0.0
            # rebaseline §6.3: paying the invoice is never counted as a new
            # expense -- spending stays exactly the original purchase.
            assert after["summary"]["total_spending"] == 900.0
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# PREVISTO: only the configured recurring salary, never a commission.
# ---------------------------------------------------------------------------


def test_report_previsto_never_includes_commission_but_may_include_recurring_salary() -> None:
    client, session_factory = _client()
    try:
        with client:
            _setup_household(client, session_factory, household_name="Família Previsto", username="admin-previsto")
            _create_account(client)
            profile = client.put("/api/profile", json=_profile_payload(monthly_salary_net="5000.00"))
            assert profile.status_code == 200, profile.text

            commission = client.post(
                "/api/commissions",
                json={
                    "description": "Comissão de venda",
                    "expected_date": "2026-09-15",
                    "gross_amount": "8500.00",
                },
            )
            assert commission.status_code == 201, commission.text

            report = client.get("/api/reports?end_month=2026-08&months=1").json()
            previsto = report["financial_states"]["previsto"]
            assert previsto["recurring_salary"] is not None
            assert previsto["recurring_salary"]["financial_state"] in {"PREVISTO", "REALIZADO"}
            assert previsto["recurring_salary"]["expected_amount"] == 5000.0
            # rebaseline §8.3 / INV-031: an unreceived commission never
            # appears anywhere in the PREVISTO section.
            assert "comiss" not in str(previsto).lower()
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Household isolation for every new Slice 7 section.
# ---------------------------------------------------------------------------


def test_report_new_sections_are_household_isolated() -> None:
    client, session_factory = _client()
    try:
        with client:
            household_a = _setup_household(
                client, session_factory, household_name="Família A Slice7", username="admin-a-slice7"
            )
            account_a = _create_account(client)
            _add_expense(client, account_id=account_a["id"], booked_at="2026-08-05", amount="200.00", description="Mercado A")
            _create_investment(client, name="Ativo A", current_value="1000.00")
            client.post(
                "/api/obligations",
                json={
                    "name": "Obrigação A",
                    "due_date": "2026-08-25",
                    "amount": 300,
                    "recurrence_months": 0,
                    "occurrence_count": 1,
                    "category": "other",
                },
            )

            household_b = _setup_household(
                client, session_factory, household_name="Família B Slice7", username="admin-b-slice7"
            )
            assert household_a != household_b
            account_b = _create_account(client)
            _add_expense(client, account_id=account_b["id"], booked_at="2026-08-06", amount="777.00", description="Mercado B")

            report_b = client.get("/api/reports?end_month=2026-08&months=1").json()
            assert report_b["summary"]["total_spending"] == 777.0
            assert report_b["investments"] == []
            assert report_b["obligations"] == []
            assert all(row["description"] != "Mercado A" for row in report_b["ledger"])
            assert report_b["patrimony"]["investments_component"] == 0.0
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Ledger preserves internal transfers as facts without double-counting them
# as income/expense.
# ---------------------------------------------------------------------------


def test_report_ledger_includes_internal_transfer_without_inflating_spending() -> None:
    client, session_factory = _client()
    try:
        with client:
            _setup_household(client, session_factory, household_name="Família Transferência", username="admin-transf")
            checking = _create_account(client, name="Conta Corrente")
            savings = _create_account(client, name="Privilège DI", account_type="investment")
            transfer = client.post(
                "/api/transfers",
                json={
                    "booked_at": "2026-08-12",
                    "description": "Resgate para pagamento",
                    "amount": "500.00",
                    "from_account_id": savings["id"],
                    "to_account_id": checking["id"],
                },
            )
            assert transfer.status_code == 201, transfer.text

            report = client.get("/api/reports?end_month=2026-08&months=1").json()
            transfer_descriptions = [row["description"] for row in report["ledger"]]
            assert "Resgate para pagamento" in transfer_descriptions
            # rebaseline §4.2: never renda/despesa.
            assert report["summary"]["total_spending"] == 0.0
            assert report["summary"]["total_cash_in"] == 0.0
            # Both transfer legs (500 out of Privilège, 500 into Conta Corrente)
            # count toward this gross activity total -- rebaseline §4.2 still
            # holds: neither leg is renda/despesa above.
            assert report["summary"]["internal_transfers_total"] == 1000.0
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# `GET /spending-economy`: seus dados / tendências / oportunidades /
# referências externas / análise / recomendação.
# ---------------------------------------------------------------------------


def test_spending_economy_flags_category_trending_above_average() -> None:
    client, session_factory = _client()
    try:
        with client:
            _setup_household(client, session_factory, household_name="Família Economia", username="admin-economia")
            account = _create_account(client)
            for month, amount in (("2026-06", "200.00"), ("2026-07", "220.00"), ("2026-08", "600.00")):
                _add_expense(
                    client,
                    account_id=account["id"],
                    booked_at=f"{month}-05",
                    amount=amount,
                    description="Mercado",
                    category_name="Mercado",
                )

            response = client.get("/api/spending-economy?end_month=2026-08&months=3")
            assert response.status_code == 200, response.text
            body = response.json()

            assert body["seus_dados"]["summary"]["total_spending"] == 1020.0
            trend = next(item for item in body["tendencias"] if item["category"] == "Mercado")
            assert trend["direction"] == "up"
            assert trend["current_amount"] == 600.0
            opportunity = next(item for item in body["oportunidades_economia"] if item["category"] == "Mercado")
            assert opportunity["vs_average_percentage"] > 0

            # rebaseline §14: referências externas nunca substituem fato
            # interno -- sem provedor de busca real configurado, a lista
            # fica estruturalmente presente e vazia, nunca inventada.
            assert body["referencias_externas"] == []
            assert body["analise"]["disponivel"] is False
            assert body["analise"]["provider"] == "local"
            assert "Mercado" in body["analise"]["resposta"]
            assert body["recomendacao"]["veredito"] == "informative"
            assert body["divergencia_codex"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_spending_economy_accepts_codex_answer_only_when_verdict_matches() -> None:
    client, session_factory = _client()
    try:
        with client:
            _setup_household(client, session_factory, household_name="Família Codex Economia", username="admin-codex-eco")
            account = _create_account(client)
            for month, amount in (("2026-06", "200.00"), ("2026-07", "200.00"), ("2026-08", "600.00")):
                _add_expense(
                    client, account_id=account["id"], booked_at=f"{month}-05", amount=amount, description="Mercado"
                )

            with pytest.MonkeyPatch.context() as codex_patch:
                codex_patch.setattr(CodexAdvisorClient, "configured", property(lambda self: True))
                codex_patch.setattr(
                    CodexAdvisorClient,
                    "analyze",
                    lambda self, payload: CodexResult(
                        {
                            "verdict": "informative",
                            "answer": "A categoria Mercado subiu bem acima do habitual neste mês.",
                            "evidence": ["Mercado 600.00 vs média 200.00"],
                            "assumptions": ["Limite de 15% acima da média"],
                            "confidence": 0.8,
                        }
                    ),
                )
                matched = client.get("/api/spending-economy?end_month=2026-08&months=3")
            assert matched.status_code == 200
            matched_body = matched.json()
            assert matched_body["analise"]["disponivel"] is True
            assert matched_body["analise"]["provider"] == "codex"
            assert "acima do habitual" in matched_body["analise"]["resposta"]
            assert matched_body["divergencia_codex"] is None

            with pytest.MonkeyPatch.context() as codex_patch:
                codex_patch.setattr(CodexAdvisorClient, "configured", property(lambda self: True))
                codex_patch.setattr(
                    CodexAdvisorClient,
                    "analyze",
                    lambda self, payload: CodexResult(
                        {
                            "verdict": "not_recommended",
                            "answer": "Corte todos os gastos imediatamente.",
                            "evidence": [],
                            "assumptions": [],
                            "confidence": 0.4,
                        }
                    ),
                )
                diverged = client.get("/api/spending-economy?end_month=2026-08&months=3")
            assert diverged.status_code == 200
            diverged_body = diverged.json()
            # PR review parity with `POST /advisor/question`/INV-021: a
            # mismatched Codex verdict is never silently applied -- the
            # deterministic answer stays, and the divergence is signalled
            # instead (Work Order "divergência ... deve ser sinalizada,
            # nunca sobrescrita silenciosamente").
            assert diverged_body["analise"]["disponivel"] is False
            assert diverged_body["analise"]["provider"] == "local"
            assert "Corte todos os gastos" not in diverged_body["analise"]["resposta"]
            assert diverged_body["divergencia_codex"] is not None
            assert diverged_body["divergencia_codex"]["codex_verdict"] == "not_recommended"
            assert diverged_body["divergencia_codex"]["deterministic_verdict"] == "informative"
    finally:
        app.dependency_overrides.pop(get_db, None)
