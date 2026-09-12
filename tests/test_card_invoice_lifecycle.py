"""Tests for the `CardInvoice` lifecycle -- P0 #87, October Go-Live Slice 2.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_2.md`.

Section 1 unit-tests the pure competence-window inverse
(`card_invoice_window`) against the already-canonical forward function
(`card_invoice_competence`) with no database at all.

Section 2 exercises `app.services.card_invoice_lifecycle` directly against
an isolated in-memory SQLite session -- the same isolated-engine pattern
`tests/test_manual_payables_card_payment.py` uses, but calling the service
functions directly (not through `TestClient`) so each test can pin the
`as_of` date the lifecycle-transition functions use, instead of depending
on the real wall-clock date the test happens to run on.

Section 3 is a thin HTTP-level smoke test proving the new endpoints are
wired correctly (auth, household isolation, request/response shape) --
correctness of the lifecycle rules themselves is Section 2's job.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-card-invoice-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "card-invoice-lifecycle-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, Household, Transaction, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.card_competence import card_invoice_competence, card_invoice_window  # noqa: E402
from app.services.card_invoice_lifecycle import (  # noqa: E402
    CardInvoiceError,
    close_invoice,
    get_or_sync_invoice,
    invoice_divergence,
    link_refund,
    outstanding_balance,
    pay_invoice,
)
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Pure round-trip property test: `card_invoice_window` is the inverse of
#    the already-canonical `card_invoice_competence`.
# ---------------------------------------------------------------------------

_CYCLE_CASES = [
    # (closing_day, due_day, competence)
    (25, 5, "2026-09"),  # due_day < closing_day -> closing month is the one before due month
    (10, 20, "2026-09"),  # due_day >= closing_day -> same month
    (31, 10, "2026-02"),  # closing_day beyond February's length -> clipped
    (28, 28, "2026-03"),  # closing_day == due_day
    (5, 15, "2027-01"),  # year boundary
]


@pytest.mark.parametrize(("closing_day", "due_day", "competence"), _CYCLE_CASES)
def test_card_invoice_window_round_trips_with_forward_competence(closing_day, due_day, competence) -> None:
    account = Account(
        account_type="credit_card", name="Cartão", card_closing_day=closing_day, card_due_day=due_day
    )
    opens_at, closes_at, due_date = card_invoice_window(account, competence)

    assert opens_at <= closes_at <= due_date
    assert card_invoice_competence(account, opens_at) == competence
    assert card_invoice_competence(account, closes_at) == competence
    # The day right after closing always rolls to the *next* invoice.
    from datetime import timedelta

    next_day = closes_at + timedelta(days=1)
    assert card_invoice_competence(account, next_day) != competence


def test_card_invoice_window_fails_closed_without_configured_cycle() -> None:
    from fastapi import HTTPException

    account = Account(account_type="credit_card", name="Cartão sem ciclo")
    with pytest.raises(HTTPException):
        card_invoice_window(account, "2026-09")


def test_card_invoice_window_fails_closed_for_non_card_account() -> None:
    from fastapi import HTTPException

    account = Account(account_type="checking", name="Conta corrente")
    with pytest.raises(HTTPException):
        card_invoice_window(account, "2026-09")


# ---------------------------------------------------------------------------
# 2. Service-level lifecycle tests (direct session, explicit `as_of`).
# ---------------------------------------------------------------------------


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def _household(session_factory, name="Família Cartão Slice2") -> str:
    with session_factory() as db:
        household = Household(name=name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Admin",
                username=f"admin-{uuid.uuid4().hex[:8]}",
                password_hash=hash_password("senha-local-segura"),
                is_admin=True,
            )
        )
        db.commit()
        return household.id


def _account(session_factory, *, household_id, name, account_type="checking", closing_day=None, due_day=None) -> str:
    with session_factory() as db:
        account = Account(
            household_id=household_id,
            name=name,
            account_type=account_type,
            card_closing_day=closing_day,
            card_due_day=due_day,
        )
        db.add(account)
        db.commit()
        return account.id


def _category(session_factory, *, household_id, name="Compras, casa e vestuário") -> str:
    with session_factory() as db:
        existing = db.scalar(select(Category).where(Category.household_id == household_id, Category.name == name))
        if existing:
            return existing.id
        category = Category(household_id=household_id, name=name)
        db.add(category)
        db.commit()
        return category.id


def _purchase(
    session_factory,
    *,
    household_id,
    account_id,
    amount: str,
    booked_at: date,
    competence: str,
    category_id: str,
    transaction_type: str = "expense",
    description: str = "Compra",
) -> str:
    with session_factory() as db:
        signed_amount = Decimal(amount) if transaction_type == "refund" else -abs(Decimal(amount))
        txn = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=booked_at,
            occurred_at=booked_at,
            competence=competence,
            description=description,
            normalized_description=description.upper(),
            amount=signed_amount,
            transaction_type=transaction_type,
            fingerprint=f"cardinvoice{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
        )
        db.add(txn)
        db.commit()
        return txn.id


def _declared_invoice_line(session_factory, *, household_id, account_id, amount: str, competence: str) -> str:
    """The invoice's own "payment received" line, exactly like a real
    PDF/CSV import would persist it -- read-only evidence for
    `declared_total`, never a human-typed correction."""

    with session_factory() as db:
        category = db.scalar(
            select(Category).where(Category.household_id == household_id, Category.name == "Conciliação")
        )
        if category is None:
            category = Category(household_id=household_id, name="Conciliação")
            db.add(category)
            db.flush()
        booked_at = date(int(competence[:4]), int(competence[5:7]), 1)
        line = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category.id,
            booked_at=booked_at,
            occurred_at=booked_at,
            competence=competence,
            description="Pagamento em ...",
            normalized_description="PAGAMENTO",
            amount=Decimal(amount),
            transaction_type="reconciliation",
            fingerprint=f"declared{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=70,
            confidence=Decimal("1"),
            excluded=True,
            reviewed=True,
        )
        db.add(line)
        db.commit()
        return line.id


def test_get_or_sync_invoice_computes_total_from_purchases_and_declared_from_reconciliation() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="300.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="120.50", booked_at=date(2026, 9, 8), competence="2026-09", category_id=category_id)
    _declared_invoice_line(session_factory, household_id=household_id, account_id=card_id, amount="420.50", competence="2026-09")

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(
            db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5)
        )
        db.commit()
        assert invoice.status == "open"
        assert invoice.computed_total == Decimal("420.50")
        assert invoice.declared_total == Decimal("420.50")
        assert invoice.principal_carried_in == Decimal("0")

    # A purchase never reduces the bank/checking side (INV-002 family):
    # nothing here ever touched a checking account.
    with session_factory() as db:
        checking_rows = db.scalars(select(Transaction).where(Transaction.transaction_type == "expense")).all()
        assert len(checking_rows) == 2


def test_get_or_sync_invoice_closes_on_closing_day_and_never_reopens() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 9))
        db.commit()
        assert invoice.status == "open"

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 10))
        db.commit()
        assert invoice.status == "closed"
        assert invoice.closed_at is not None
        invoice_id = invoice.id

    # Resyncing with an *earlier* as_of never reopens an already-closed cycle.
    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 1))
        db.commit()
        assert invoice.id == invoice_id
        assert invoice.status == "closed"


def test_close_invoice_fails_closed_before_closing_date() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 1))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        with pytest.raises(CardInvoiceError):
            close_invoice(db, household_id=household_id, invoice_id=invoice_id, as_of=date(2026, 9, 1))


def test_pay_invoice_rejects_while_still_open() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    checking_id = _account(session_factory, household_id=household_id, name="Conta Corrente")
    category_id = _category(session_factory, household_id=household_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 1))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        with pytest.raises(CardInvoiceError):
            pay_invoice(
                db,
                household_id=household_id,
                invoice_id=invoice_id,
                paying_account=paying_account,
                category=category,
                amount=Decimal("100"),
                booked_at=date(2026, 9, 15),
                description="Pagamento",
            )


def test_pay_invoice_partial_then_remainder_reaches_paid_without_double_counting() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    checking_id = _account(session_factory, household_id=household_id, name="Conta Corrente")
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="1000.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 10))
        db.commit()
        invoice_id = invoice.id
        assert invoice.status == "closed"
        assert invoice.computed_total == Decimal("1000.00")

    # Partial payment.
    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        checking_leg, invoice, duplicate_assessment = pay_invoice(
            db,
            household_id=household_id,
            invoice_id=invoice_id,
            paying_account=paying_account,
            category=category,
            amount=Decimal("600.00"),
            booked_at=date(2026, 9, 15),
            description="Pagamento parcial",
        )
        db.commit()
        assert invoice.status == "partially_paid"
        assert invoice.paid_total == Decimal("600.00")
        assert outstanding_balance(invoice) == Decimal("400.00")
        assert checking_leg.transaction_type == "reconciliation"
        assert checking_leg.excluded is True
        assert checking_leg.amount == Decimal("-600.00")
        assert checking_leg.account_id == checking_id
        first_leg_id = checking_leg.id

    # Never doubles as a new economic expense: still exactly one `expense`
    # transaction (the original purchase).
    with session_factory() as db:
        expenses = db.scalars(select(Transaction).where(Transaction.transaction_type == "expense")).all()
        assert len(expenses) == 1
        assert expenses[0].amount == Decimal("-1000.00")

    # Remainder.
    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        checking_leg_2, invoice, _ = pay_invoice(
            db,
            household_id=household_id,
            invoice_id=invoice_id,
            paying_account=paying_account,
            category=category,
            amount=Decimal("400.00"),
            booked_at=date(2026, 9, 20),
            description="Pagamento restante",
        )
        db.commit()
        assert invoice.status == "paid"
        assert invoice.paid_total == Decimal("1000.00")
        assert outstanding_balance(invoice) == Decimal("0")
        assert checking_leg_2.id != first_leg_id

    # A third payment attempt is rejected -- already fully paid.
    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        with pytest.raises(CardInvoiceError):
            pay_invoice(
                db,
                household_id=household_id,
                invoice_id=invoice_id,
                paying_account=paying_account,
                category=category,
                amount=Decimal("1.00"),
                booked_at=date(2026, 9, 21),
                description="Pagamento indevido",
            )

    with session_factory() as db:
        legs = db.scalars(select(Transaction).where(Transaction.transaction_type == "reconciliation")).all()
        assert len(legs) == 2


def test_pay_invoice_rejects_amount_beyond_outstanding_balance() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    checking_id = _account(session_factory, household_id=household_id, name="Conta Corrente")
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="500.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 10))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        with pytest.raises(CardInvoiceError):
            pay_invoice(
                db,
                household_id=household_id,
                invoice_id=invoice_id,
                paying_account=paying_account,
                category=category,
                amount=Decimal("600.00"),
                booked_at=date(2026, 9, 15),
                description="Pagamento maior que a fatura",
            )


def test_principal_carried_forward_without_creating_new_expense() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    checking_id = _account(session_factory, household_id=household_id, name="Conta Corrente")
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="1000.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 10))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        _, invoice, _ = pay_invoice(
            db, household_id=household_id, invoice_id=invoice_id, paying_account=paying_account,
            category=category, amount=Decimal("400.00"), booked_at=date(2026, 9, 15), description="Pagamento parcial",
        )
        db.commit()
        assert outstanding_balance(invoice) == Decimal("600.00")

    # A new finance charge lands in the *next* cycle.
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="80.00", booked_at=date(2026, 10, 5), competence="2026-10", category_id=category_id, description="Juros do mês")

    with session_factory() as db:
        account = db.get(Account, card_id)
        next_invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-10", as_of=date(2026, 10, 5))
        db.commit()
        # The unpaid principal is carried forward as its own field -- never
        # a new `expense` Transaction, never re-added to `computed_total`.
        assert next_invoice.principal_carried_in == Decimal("600.00")
        assert next_invoice.computed_total == Decimal("80.00")
        assert outstanding_balance(next_invoice) == Decimal("680.00")

    with session_factory() as db:
        account = db.get(Account, card_id)
        september = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 10, 5))
        db.commit()
        assert september.principal_carried_out == Decimal("600.00")

    with session_factory() as db:
        expenses = db.scalars(select(Transaction).where(Transaction.transaction_type == "expense")).all()
        assert len(expenses) == 2  # the original 1000 purchase + the 80 charge -- never a third for the 600 carry.


def test_refund_integral_neutralizes_purchase_without_deleting_it() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    purchase_id = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="300.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    refund_id = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="300.00", booked_at=date(2026, 9, 5), competence="2026-09", category_id=category_id, transaction_type="refund", description="Estorno")

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        # Unlinked refund does not yet neutralize anything -- never inferred.
        assert invoice.computed_total == Decimal("300.00")

    with session_factory() as db:
        refund, original = link_refund(db, household_id=household_id, refund_transaction_id=refund_id, original_transaction_id=purchase_id)
        db.commit()
        assert refund.refund_of_transaction_id == purchase_id

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        assert invoice.computed_total == Decimal("0.00")

    # The original purchase still exists -- never deleted.
    with session_factory() as db:
        original = db.get(Transaction, purchase_id)
        assert original is not None
        assert original.amount == Decimal("-300.00")


def test_refund_partial_neutralizes_only_the_linked_amount_and_rejects_overshoot() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    purchase_id = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="300.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    refund_1 = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="100.00", booked_at=date(2026, 9, 5), competence="2026-09", category_id=category_id, transaction_type="refund")
    refund_2 = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="100.00", booked_at=date(2026, 9, 6), competence="2026-09", category_id=category_id, transaction_type="refund")
    refund_3 = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="150.00", booked_at=date(2026, 9, 7), competence="2026-09", category_id=category_id, transaction_type="refund")

    with session_factory() as db:
        link_refund(db, household_id=household_id, refund_transaction_id=refund_1, original_transaction_id=purchase_id)
        link_refund(db, household_id=household_id, refund_transaction_id=refund_2, original_transaction_id=purchase_id)
        db.commit()

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        assert invoice.computed_total == Decimal("100.00")

    with session_factory() as db:
        with pytest.raises(CardInvoiceError):
            link_refund(db, household_id=household_id, refund_transaction_id=refund_3, original_transaction_id=purchase_id)


def test_refund_after_invoice_already_paid_credits_later_cycle_without_rewriting_it() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    checking_id = _account(session_factory, household_id=household_id, name="Conta Corrente")
    category_id = _category(session_factory, household_id=household_id)
    purchase_id = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="300.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 10))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        paying_account = db.get(Account, checking_id)
        category = db.get(Category, category_id)
        _, invoice, _ = pay_invoice(
            db, household_id=household_id, invoice_id=invoice_id, paying_account=paying_account,
            category=category, amount=Decimal("300.00"), booked_at=date(2026, 9, 15), description="Pagamento integral",
        )
        db.commit()
        assert invoice.status == "paid"

    # A refund for that same purchase only arrives (and is booked) the
    # following cycle.
    refund_id = _purchase(session_factory, household_id=household_id, account_id=card_id, amount="100.00", booked_at=date(2026, 10, 4), competence="2026-10", category_id=category_id, transaction_type="refund")
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="150.00", booked_at=date(2026, 10, 5), competence="2026-10", category_id=category_id, description="Nova compra")

    with session_factory() as db:
        link_refund(db, household_id=household_id, refund_transaction_id=refund_id, original_transaction_id=purchase_id)
        db.commit()

    # September stays exactly as it was settled -- never rewritten.
    with session_factory() as db:
        account = db.get(Account, card_id)
        september = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 10, 5))
        db.commit()
        assert september.status == "paid"
        assert september.computed_total == Decimal("300.00")
        assert september.paid_total == Decimal("300.00")

    # October gets the credit instead.
    with session_factory() as db:
        account = db.get(Account, card_id)
        october = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-10", as_of=date(2026, 10, 5))
        db.commit()
        assert october.computed_total == Decimal("50.00")  # 150 new purchase - 100 linked refund


def test_divergence_not_applicable_without_declared_total() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="200.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        divergence = invoice_divergence(db, household_id=household_id, invoice_id=invoice_id)
        db.commit()
        assert divergence.status == "not_applicable"


def test_divergence_reconciled_when_declared_matches_computed() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="500.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    _declared_invoice_line(session_factory, household_id=household_id, account_id=card_id, amount="500.00", competence="2026-09")

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        divergence = invoice_divergence(db, household_id=household_id, invoice_id=invoice_id)
        db.commit()
        assert divergence.status == "reconciled"
        assert divergence.difference == Decimal("0.00")


def test_divergence_unreconciled_unexplained_without_any_candidate_cause() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="500.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    _declared_invoice_line(session_factory, household_id=household_id, account_id=card_id, amount="560.00", competence="2026-09")

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        divergence = invoice_divergence(db, household_id=household_id, invoice_id=invoice_id)
        db.commit()
        assert divergence.status == "unreconciled_unexplained"
        assert divergence.difference == Decimal("60.00")
        assert divergence.explanations == ()


def test_divergence_explained_by_unlinked_refund_in_cycle() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    card_id = _account(session_factory, household_id=household_id, name="Cartão", account_type="credit_card", closing_day=10, due_day=17)
    category_id = _category(session_factory, household_id=household_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="500.00", booked_at=date(2026, 9, 3), competence="2026-09", category_id=category_id)
    _purchase(session_factory, household_id=household_id, account_id=card_id, amount="30.00", booked_at=date(2026, 9, 6), competence="2026-09", category_id=category_id, transaction_type="refund")
    _declared_invoice_line(session_factory, household_id=household_id, account_id=card_id, amount="470.00", competence="2026-09")

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        divergence = invoice_divergence(db, household_id=household_id, invoice_id=invoice_id)
        db.commit()
        assert divergence.status == "unreconciled_explained"
        assert divergence.explanations[0]["cause"] == "estorno_nao_vinculado_no_ciclo"
        assert divergence.explanations[0]["amount"] == "30.00"


def test_household_isolation_of_lifecycle_functions() -> None:
    session_factory = _session_factory()
    household_a = _household(session_factory, name="Família A")
    household_b = _household(session_factory, name="Família B")
    card_id = _account(session_factory, household_id=household_a, name="Cartão A", account_type="credit_card", closing_day=10, due_day=17)

    with session_factory() as db:
        account = db.get(Account, card_id)
        invoice = get_or_sync_invoice(db, household_id=household_a, account=account, competence="2026-09", as_of=date(2026, 9, 5))
        db.commit()
        invoice_id = invoice.id

    with session_factory() as db:
        with pytest.raises(LookupError):
            close_invoice(db, household_id=household_b, invoice_id=invoice_id, as_of=date(2026, 9, 10))
        with pytest.raises(LookupError):
            invoice_divergence(db, household_id=household_b, invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# 3. Thin HTTP-level wiring smoke tests.
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


def _setup_household(client, *, username="admin-invoice-http"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família HTTP Cartão",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201
    complete_mfa_enrollment(client)


def _create_account(client, *, name, account_type="checking", card_closing_day=None, card_due_day=None):
    payload = {"name": name, "account_type": account_type}
    if card_closing_day is not None:
        payload["card_closing_day"] = card_closing_day
    if card_due_day is not None:
        payload["card_due_day"] = card_due_day
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_card_invoices_endpoints_require_authentication() -> None:
    client, _ = _client()
    with client:
        assert client.get("/api/card-invoices", params={"account_id": "x"}).status_code == 401
        assert client.post("/api/card-invoices/sync", json={"account_id": "x"}).status_code == 401


def test_sync_and_get_card_invoices_returns_current_cycle() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        card_id = _create_account(client, name="Cartão HTTP", account_type="credit_card", card_closing_day=10, card_due_day=17)

        response = client.post("/api/card-invoices/sync", json={"account_id": card_id})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["account_id"] == card_id
        assert body["status"] in {"open", "closed", "partially_paid", "paid"}

        listing = client.get("/api/card-invoices", params={"account_id": card_id})
        assert listing.status_code == 200
        assert any(item["id"] == body["id"] for item in listing.json())


def test_pay_card_invoice_lifecycle_endpoint_rejects_still_open_invoice() -> None:
    from app.services.finance import month_key

    client, session_factory = _client()
    with client:
        _setup_household(client)
        card_id = _create_account(client, name="Cartão HTTP2", account_type="credit_card", card_closing_day=10, card_due_day=17)
        checking_id = _create_account(client, name="Conta HTTP2")
        # A competence far enough in the future that "today" (whenever this
        # test actually runs) can never have reached its closing date --
        # the endpoint contract under test is "still open is rejected",
        # not any particular calendar date.
        future_competence = month_key(date(date.today().year + 5, 6, 1))
        sync = client.post("/api/card-invoices/sync", json={"account_id": card_id, "competence": future_competence})
        assert sync.status_code == 201
        body = sync.json()
        assert body["status"] == "open"
        invoice_id = body["id"]

        pay = client.post(
            f"/api/card-invoices/{invoice_id}/pay",
            json={
                "paying_account_id": checking_id,
                "amount": "10.00",
                "booked_at": date.today().isoformat(),
                "description": "Pagamento",
                "confirmed": True,
            },
        )
        assert pay.status_code == 409
        assert "não fechou" in pay.json()["detail"]
