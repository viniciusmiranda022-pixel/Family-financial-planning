"""Tests for October Go-Live Slice 3 (P0 #87) -- REALIZADO/COMPROMETIDO
separation of obligations, card invoices and the forward projection.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_3.md`.

Section 1 unit-tests the pure/DB-level helpers directly
(`app.api._obligation_rows`, `_forecast_obligations`,
`_forecast_card_invoices`, `card_invoice_lifecycle.invoice_financial_state`)
against an isolated in-memory SQLite session -- the same style
`tests/test_card_invoice_lifecycle.py` and `tests/test_plan_workbook.py`
already use for this module's private helpers.

Section 2 is an HTTP-level functional suite for `pay_obligation`/
`unpay_obligation` and the end-to-end "nothing silently disappears from the
projection" regression for an overdue `Obligation` and a closed, unpaid
`CardInvoice` -- Work Order "Testes obrigatórios" #1-#3, #8, #11.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-obligation-slice3-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "obligation-slice3-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.api import (  # noqa: E402
    _forecast_card_invoices,
    _forecast_obligations,
    _obligation_rows,
)
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, CardInvoice, Category, Household, Obligation, Transaction, User  # noqa: E402
from app.services.card_invoice_lifecycle import invoice_financial_state  # noqa: E402
from app.services.financial_state import COMPROMETIDO, REALIZADO  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Direct-session unit tests.
# ---------------------------------------------------------------------------


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def _household(session_factory, name="Família Obrigações Slice3") -> str:
    with session_factory() as db:
        household = Household(name=name)
        db.add(household)
        db.commit()
        return household.id


def _obligation(
    session_factory,
    *,
    household_id,
    due_date: date,
    amount: str = "500.00",
    status: str = "pending",
    recurrence_months: int = 0,
    occurrence_count: int = 1,
) -> str:
    with session_factory() as db:
        item = Obligation(
            household_id=household_id,
            name="Parcela chácara",
            due_date=due_date,
            amount=Decimal(amount),
            status=status,
            recurrence_months=recurrence_months,
            occurrence_count=occurrence_count,
        )
        db.add(item)
        db.commit()
        return item.id


def test_pending_obligation_is_comprometido_not_realizado() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 12, 1))

    with session_factory() as db:
        rows = _obligation_rows(db, household_id, reference=date(2026, 11, 1))
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["financial_state"] == COMPROMETIDO


def test_paid_obligation_is_realizado() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 12, 1), status="paid")

    with session_factory() as db:
        rows = _obligation_rows(db, household_id, include_paid=True)
    assert len(rows) == 1
    assert rows[0]["financial_state"] == REALIZADO


def test_overdue_obligation_remains_comprometido_and_visible() -> None:
    """Work Order "Testes obrigatórios" #3 -- an overdue, unpaid obligation
    stays visible/committed; being overdue never removes it from the list
    `_obligation_rows` returns (the alert level is `overdue`, not absence)."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 1, 1))

    with session_factory() as db:
        rows = _obligation_rows(db, household_id, reference=date(2026, 6, 1))
    assert len(rows) == 1
    assert rows[0]["alert_level"] == "overdue"
    assert rows[0]["financial_state"] == COMPROMETIDO


def test_forecast_obligations_without_start_month_keeps_prior_behaviour() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        obligations = list(
            db.scalars(select(Obligation).where(Obligation.household_id == household_id))
        )
    # An overdue obligation with no `start_month` argument is bucketed by its
    # own (past) due month, exactly like before this slice -- backward
    # compatible default, exercised for real by `tests/test_plan_workbook.py`.
    obligation_id = _obligation(session_factory, household_id=household_id, due_date=date(2026, 1, 1))
    with session_factory() as db:
        obligations = list(
            db.scalars(select(Obligation).where(Obligation.id == obligation_id))
        )
        result = _forecast_obligations(obligations)
    assert result == {"2026-01": Decimal("500.00")}


def test_forecast_obligations_clamps_overdue_amount_into_start_month() -> None:
    """Work Order "Testes obrigatórios" #3 / rebaseline §7: an overdue,
    unpaid obligation's amount never silently disappears from the
    projection just because its own due month is now in the past relative
    to the projection's first visited month."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 1, 1), amount="500.00")
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 7, 1), amount="300.00")

    with session_factory() as db:
        obligations = list(
            db.scalars(select(Obligation).where(Obligation.household_id == household_id))
        )
        result = _forecast_obligations(obligations, date(2026, 7, 1))

    # The already-future obligation keeps its own month; the overdue one is
    # folded into `start_month` instead of vanishing into "2026-01", a month
    # the projection's forward-only cursor (starting at 2026-07) never visits.
    assert result == {"2026-07": Decimal("800.00")}


def test_forecast_obligations_ignores_paid_and_inactive() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _obligation(session_factory, household_id=household_id, due_date=date(2026, 1, 1), status="paid")
    with session_factory() as db:
        db.add(
            Obligation(
                household_id=household_id,
                name="Cancelada",
                due_date=date(2026, 1, 1),
                amount=Decimal("100.00"),
                active=False,
            )
        )
        db.commit()

    with session_factory() as db:
        obligations = list(
            db.scalars(select(Obligation).where(Obligation.household_id == household_id))
        )
        result = _forecast_obligations(obligations, date(2026, 7, 1))
    assert result == {}


def _card_account(session_factory, *, household_id, name="Cartão Slice3") -> str:
    with session_factory() as db:
        account = Account(
            household_id=household_id,
            name=name,
            account_type="credit_card",
            card_closing_day=10,
            card_due_day=17,
        )
        db.add(account)
        db.commit()
        return account.id


def _card_invoice(
    session_factory,
    *,
    household_id,
    account_id,
    competence: str,
    status: str,
    computed_total: str,
    paid_total: str = "0",
    principal_carried_in: str = "0",
    due_date: date | None = None,
) -> str:
    with session_factory() as db:
        invoice = CardInvoice(
            household_id=household_id,
            account_id=account_id,
            competence=competence,
            status=status,
            computed_total=Decimal(computed_total),
            paid_total=Decimal(paid_total),
            principal_carried_in=Decimal(principal_carried_in),
            due_date=due_date,
        )
        db.add(invoice)
        db.commit()
        return invoice.id


def test_forecast_card_invoices_only_counts_closed_and_partially_paid() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _card_account(session_factory, household_id=household_id)
    _card_invoice(
        session_factory, household_id=household_id, account_id=account_id,
        competence="2026-06", status="open", computed_total="900.00", due_date=date(2026, 6, 17),
    )
    _card_invoice(
        session_factory, household_id=household_id, account_id=account_id,
        competence="2026-07", status="closed", computed_total="1200.00", due_date=date(2026, 7, 17),
    )
    _card_invoice(
        session_factory, household_id=household_id, account_id=account_id,
        competence="2026-08", status="partially_paid", computed_total="800.00",
        paid_total="300.00", due_date=date(2026, 8, 17),
    )
    _card_invoice(
        session_factory, household_id=household_id, account_id=account_id,
        competence="2026-09", status="paid", computed_total="400.00",
        paid_total="400.00", due_date=date(2026, 9, 17),
    )

    with session_factory() as db:
        result = _forecast_card_invoices(db, household_id, date(2026, 6, 1))

    assert result == {"2026-07": Decimal("1200.00"), "2026-08": Decimal("500.00")}


def test_forecast_card_invoices_clamps_overdue_due_date_into_start_month() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _card_account(session_factory, household_id=household_id)
    _card_invoice(
        session_factory, household_id=household_id, account_id=account_id,
        competence="2026-01", status="closed", computed_total="600.00", due_date=date(2026, 1, 17),
    )

    with session_factory() as db:
        result = _forecast_card_invoices(db, household_id, date(2026, 7, 1))
    assert result == {"2026-07": Decimal("600.00")}


def test_invoice_financial_state_mapping() -> None:
    for status, expected in (
        ("open", REALIZADO),
        ("closed", COMPROMETIDO),
        ("partially_paid", COMPROMETIDO),
        ("paid", REALIZADO),
    ):
        invoice = CardInvoice(status=status, computed_total=Decimal("0"), paid_total=Decimal("0"))
        assert invoice_financial_state(invoice) == expected


# ---------------------------------------------------------------------------
# 2. HTTP-level functional suite.
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


def _setup_household(client, *, username="admin-obligation-slice3"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Obrigações HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def test_pay_and_unpay_obligation_full_functional_cycle() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        assert account.status_code == 201, account.text
        account_id = account.json()["id"]

        created = client.post(
            "/api/obligations",
            json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"},
        )
        assert created.status_code == 201, created.text
        obligation_id = created.json()["id"]

        before = next(item for item in client.get("/api/obligations").json() if item["id"] == obligation_id)
        assert before["financial_state"] == COMPROMETIDO
        assert before["status"] == "pending"

        pay = client.post(
            f"/api/obligations/{obligation_id}/pay",
            json={"account_id": account_id, "paid_at": "2026-08-09", "confirmed_large_amount": False},
        )
        assert pay.status_code == 200, pay.text
        transaction_id = pay.json()["transaction_id"]
        assert pay.json()["transaction_created"] is True

        after = next(item for item in client.get("/api/obligations").json() if item["id"] == obligation_id)
        assert after["financial_state"] == REALIZADO
        assert after["status"] == "paid"
        assert after["paid_transaction_id"] == transaction_id

        # Work Order "Testes obrigatórios" #8: no double counting between the
        # obligation and the transaction it produced -- exactly one expense
        # `Transaction` exists for this payment, and the obligation itself no
        # longer contributes to the projection's COMPROMETIDO obligations.
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id))
            all_expenses = list(
                db.scalars(
                    select(Transaction).where(
                        Transaction.household_id == household_id, Transaction.transaction_type == "expense"
                    )
                )
            )
            assert len(all_expenses) == 1

        forecast = client.get("/api/forecast")
        assert forecast.status_code == 200
        total_obligations_projected = sum(
            Decimal(str(row["obligations"])) for row in forecast.json()["rows"]
        )
        assert total_obligations_projected == Decimal("0.00")

        # Undo restores the compromisso without deleting the audit trail --
        # the reversal is itself a new, auditable state transition.
        unpay = client.post(
            f"/api/obligations/{obligation_id}/unpay",
            json={"reason": "Pagamento lançado por engano"},
        )
        assert unpay.status_code == 200, unpay.text
        assert unpay.json()["transaction_deleted"] is True

        reverted = next(item for item in client.get("/api/obligations").json() if item["id"] == obligation_id)
        assert reverted["financial_state"] == COMPROMETIDO
        assert reverted["status"] == "pending"


def test_overdue_unpaid_obligation_still_appears_in_forecast() -> None:
    """End-to-end regression for the disappearing-money gap: an overdue,
    unpaid obligation must still be summed into the nearest month
    `/api/forecast` actually shows, never silently dropped."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/obligations",
            json={"name": "Conta atrasada", "due_date": "2020-01-10", "amount": "321.00"},
        )
        assert created.status_code == 201, created.text

        forecast = client.get("/api/forecast")
        assert forecast.status_code == 200
        rows = forecast.json()["rows"]
        assert rows, "forecast must project at least one month"
        assert rows[0]["financial_state"] == "PREVISTO"
        total_obligations_projected = sum(Decimal(str(row["obligations"])) for row in rows)
        assert total_obligations_projected == Decimal("321.00")
        # Folded into the first row specifically -- the nearest month the
        # projection actually visits -- not merely present somewhere.
        assert Decimal(str(rows[0]["obligations"])) == Decimal("321.00")


def test_closed_unpaid_card_invoice_contributes_to_forecast_as_committed() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        with session_factory() as db:
            household_id = db.scalar(select(Household)).id
        account_id = _card_account(session_factory, household_id=household_id, name="Cartão Forecast")
        _card_invoice(
            session_factory,
            household_id=household_id,
            account_id=account_id,
            competence="2020-01",
            status="closed",
            computed_total="777.00",
            due_date=date(2020, 1, 17),
        )

        forecast = client.get("/api/forecast")
        assert forecast.status_code == 200
        rows = forecast.json()["rows"]
        assert rows
        total_card_invoices_projected = sum(
            Decimal(str(row.get("card_invoices", "0"))) for row in rows
        )
        assert total_card_invoices_projected == Decimal("777.00")
        assert Decimal(str(rows[0]["card_invoices"])) == Decimal("777.00")


def test_pay_obligation_rejects_amount_mismatched_transaction_link() -> None:
    """Regression: `pay_obligation` never accepts a linked transaction whose
    amount differs from the obligation's own amount -- this is what actually
    prevents an obligation payment from silently duplicating or
    under/over-counting an existing economic fact."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        account_id = account.json()["id"]
        created = client.post(
            "/api/obligations",
            json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"},
        )
        obligation_id = created.json()["id"]

        with session_factory() as db:
            household_id = db.scalar(select(Household)).id
            category = Category(household_id=household_id, name="Compromissos")
            db.add(category)
            db.flush()
            txn = Transaction(
                household_id=household_id,
                account_id=account_id,
                category_id=category.id,
                booked_at=date(2026, 8, 9),
                occurred_at=date(2026, 8, 9),
                competence="2026-08",
                description="Pagamento diferente",
                normalized_description="PAGAMENTO DIFERENTE",
                amount=Decimal("-499.00"),
                transaction_type="expense",
                fingerprint=f"mismatch{uuid.uuid4().hex}".ljust(64, "0")[:64],
                source_priority=50,
                confidence=Decimal("1"),
                reviewed=True,
            )
            db.add(txn)
            db.commit()
            transaction_id = txn.id

        pay = client.post(
            f"/api/obligations/{obligation_id}/pay",
            json={"transaction_id": transaction_id},
        )
        assert pay.status_code == 422, pay.text
