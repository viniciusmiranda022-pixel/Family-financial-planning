"""Tests for the manual go-live financial flows -- slice 3: Contas a pagar
+ Pagamento de fatura.

Work Order: `docs/WORK_ORDER_MANUAL_PAYABLES_CARD_PAYMENT.md`.

Scope covered here: the new manual card-invoice payment command (`POST
/api/card-payment-reconciliations/pay`,
`app/services/card_payment_reconciliation.py::pay_card_invoice`) that
creates the checking-side (bank-debit) reconciliation leg for a payment
that has not been imported yet and links it to the invoice's existing
payment-received line in one atomic call -- reusing `link_card_payment`'s
exact tolerance/direction contract (no second reconciliation policy,
INV-002). Also covers the read-only "Contas a pagar" card-invoice listing
(`GET /api/card-payment-reconciliations/invoices`,
`list_card_invoice_obligations`) and the combined resgate-then-pagamento
flow, which is exactly two independent calls to already-canonical
commands (`POST /api/transactions` with `movement_type=redemption`, this
slice's `pay` endpoint) -- never a new backend orchestration endpoint.

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_manual_transfers.py` and `tests/test_card_payment_reconciliation.py`.
"""

import json
import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-manual-payables-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "manual-payables-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AuditEvent,
    Category,
    DuplicateGroup,
    Household,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402


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


def _setup_household(
    client,
    session_factory,
    *,
    household_name="Família Contas a Pagar",
    username="admin-payables",
    password="senha-local-segura",
):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": household_name,
            "name": "Admin",
            "username": username,
            "password": password,
        },
    )
    assert setup.status_code == 201
    with session_factory() as db:
        household_id = db.scalar(select(User.household_id).where(User.username == username.lower()))
    return household_id


def _create_account(client, *, name, account_type="checking", last_four=None):
    payload = {"name": name, "account_type": account_type}
    if last_four is not None:
        payload["last_four"] = last_four
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


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


def _card_invoice_line(
    session_factory,
    *,
    household_id: str,
    credit_card_account_id: str,
    amount: str = "1500.00",
    day: int = 1,
    competence: str = "2026-08",
    description: str = "Pagamento em 10 AGO",
) -> str:
    """The invoice's own "payment received" line, the way a real Itaú/Nubank
    PDF import would persist it: positive amount, `credit_card` account,
    `transaction_type="reconciliation"`, unlinked -- an open obligation
    "Contas a pagar" must be able to show and this slice's `pay` endpoint
    must be able to settle."""

    with session_factory() as db:
        category = db.scalar(
            select(Category).where(Category.household_id == household_id, Category.name == "Conciliação")
        )
        if category is None:
            category = Category(household_id=household_id, name="Conciliação", color="#64748B")
            db.add(category)
            db.flush()
        line = Transaction(
            household_id=household_id,
            account_id=credit_card_account_id,
            category_id=category.id,
            booked_at=date(2026, 8, day),
            occurred_at=date(2026, 8, day),
            competence=competence,
            description=description,
            normalized_description=description.upper(),
            amount=Decimal(amount),
            transaction_type="reconciliation",
            fingerprint=f"invoiceline{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=70,
            confidence=Decimal("1"),
            excluded=True,
            reviewed=True,
        )
        db.add(line)
        db.commit()
        return line.id


def _bank_debit_line(
    session_factory,
    *,
    household_id: str,
    checking_account_id: str,
    amount: str,
    day: int = 10,
    month: int = 8,
    year: int = 2026,
    competence: str = "2026-08",
    description: str = "Débito fatura",
) -> str:
    """An already-imported bank-statement debit: negative amount, `checking`
    account, `transaction_type="reconciliation"`, unlinked -- exactly the
    kind of row a real Itaú/Nubank bank-statement import would leave
    sitting unlinked in `list_card_payment_reconciliations`. This is the
    bank fact `pay_card_invoice` must now reuse (link) instead of
    duplicating with a second, manually-created leg for the very same
    real-world debit (REQUEST_CHANGES 2026-09-07, `BLOQUEIO DE MERGE`)."""

    with session_factory() as db:
        category = db.scalar(
            select(Category).where(Category.household_id == household_id, Category.name == "Conciliação")
        )
        if category is None:
            category = Category(household_id=household_id, name="Conciliação", color="#64748B")
            db.add(category)
            db.flush()
        line = Transaction(
            household_id=household_id,
            account_id=checking_account_id,
            category_id=category.id,
            booked_at=date(year, month, day),
            occurred_at=date(year, month, day),
            competence=competence,
            description=description,
            normalized_description=description.upper(),
            amount=Decimal(amount),
            transaction_type="reconciliation",
            fingerprint=f"bankdebit{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=70,
            confidence=Decimal("1"),
            excluded=True,
            reviewed=True,
        )
        db.add(line)
        db.commit()
        return line.id


def _pay_payload(*, card_transaction_id, paying_account_id, amount, booked_at="2026-08-10", **extra):
    payload = {
        "card_transaction_id": card_transaction_id,
        "paying_account_id": paying_account_id,
        "amount": amount,
        "booked_at": booked_at,
        "description": "Pagamento da fatura Itaú",
        "confirmed": True,
    }
    payload.update(extra)
    return payload


def test_manual_payment_with_sufficient_amount_is_reconciliation_with_zero_new_expense() -> None:
    """INV-002: the manual payment leg is `reconciliation`, excluded from
    every operating total, and leaves an audit trail -- exactly like an
    imported payment link, never a second expense for the invoice."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card", last_four="1234")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["card_transaction_id"] == invoice_id
        checking_id = body["checking_transaction_id"]
        assert checking_id != invoice_id

        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["spending"] == before["spending"]
        assert after["cash_in"] == before["cash_in"]

        with session_factory() as db:
            leg = db.get(Transaction, checking_id)
            invoice = db.get(Transaction, invoice_id)
            assert leg.transaction_type == "reconciliation"
            assert leg.excluded is True
            assert leg.amount == Decimal("-1500.00")
            assert leg.account_id == itau
            assert leg.category.name == "Conciliação"
            assert leg.linked_transaction_id == invoice.id
            assert invoice.linked_transaction_id == leg.id

            event = db.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "card_payment_reconciliation.pay")
            )
            assert event is not None
            details = json.loads(event.details)
            assert details["card_transaction_id"] == invoice_id
            assert details["paying_account_id"] == itau


def test_manual_payment_rejects_amount_outside_tolerance_partial_payment() -> None:
    """Partial payment is explicitly out of scope: the contract requires the
    amount paid to match the invoice within the standard R$ 0.01 tolerance;
    anything else is rejected, never silently accepted or invented."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1000.00"),
        )
        assert response.status_code == 409, response.text
        assert "parcial" in response.json()["detail"]

        with session_factory() as db:
            invoice = db.get(Transaction, invoice_id)
            assert invoice.linked_transaction_id is None
            assert db.scalar(select(Transaction).where(Transaction.id != invoice_id)) is None


def test_manual_payment_rejects_already_paid_invoice() -> None:
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )
        first = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=invoice_id,
                paying_account_id=itau,
                amount="1500.00",
                booked_at="2026-08-11",
            ),
        )
        assert second.status_code == 409, second.text
        assert "já está paga" in second.json()["detail"]


def test_manual_payment_rejects_paying_account_not_checking() -> None:
    """No second direction policy: the paying account must be `checking`,
    exactly like every existing card-payment-reconciliation candidate."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        outro_cartao = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=outro_cartao, amount="1500.00"),
        )
        assert response.status_code == 409, response.text
        assert "conta corrente" in response.json()["detail"]


def test_manual_payment_rejects_card_transaction_id_that_is_not_a_pending_invoice() -> None:
    """A checking-side reconciliation row (or any other transaction) must
    never be accepted as the "invoice" argument -- only an unlinked
    credit-card-side reconciliation row is."""
    client, session_factory = _client()
    with client:
        _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Corrente")

        expense = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Supermercado",
                "amount": 200,
                "movement_type": "expense",
                "account_id": itau,
                "category_name": "Mercado",
            },
        )
        assert expense.status_code == 201, expense.text

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=expense.json()["id"], paying_account_id=nubank, amount="200.00"
            ),
        )
        assert response.status_code == 409, response.text
        assert "conciliação" in response.json()["detail"]


def test_manual_payment_rejects_checking_side_reconciliation_row_as_invoice() -> None:
    """A checking-side reconciliation row (e.g. an already-imported bank
    debit) is the wrong side of the pair -- only a `credit_card`-side row
    is a payable "fatura"."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        checking_side_id = _card_invoice_line(
            session_factory,
            household_id=household,
            credit_card_account_id=itau,
            amount="-200.00",
            description="Débito fatura",
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=checking_side_id, paying_account_id=cartao, amount="200.00"),
        )
        assert response.status_code == 409, response.text
        assert "fatura de cartão" in response.json()["detail"]


def test_manual_payment_requires_authentication() -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id="does-not-matter", paying_account_id="also-does-not-matter", amount="100.00"
            ),
        )
        assert response.status_code == 401


def test_manual_payment_household_isolation() -> None:
    """A user must never be able to pay another household's invoice, or use
    another household's account as the payer -- even by guessing ids."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")

        _create_household_admin(
            session_factory,
            household_name="Outra Família",
            username="admin-outra-payables",
            password="outra-senha-segura",
        )
        with session_factory() as db:
            other_household_id = db.scalar(
                select(User.household_id).where(User.username == "admin-outra-payables")
            )
            other_card = Account(
                household_id=other_household_id, name="Cartão de outra família", account_type="credit_card"
            )
            db.add(other_card)
            db.commit()
            other_card_id = other_card.id

        foreign_invoice_id = _card_invoice_line(
            session_factory, household_id=other_household_id, credit_card_account_id=other_card_id
        )

        # This household's account paying a foreign invoice: 404 (invoice
        # id does not resolve within this household).
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=foreign_invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 404, response.text

        # A foreign account id as the payer: 404 (account lookup is
        # household-scoped).
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        own_invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=own_invoice_id, paying_account_id=other_card_id, amount="1500.00"
            ),
        )
        assert response.status_code == 404, response.text

        with session_factory() as db:
            assert db.get(Transaction, foreign_invoice_id).linked_transaction_id is None
            assert db.get(Transaction, own_invoice_id).linked_transaction_id is None


def test_manual_payment_large_amount_requires_confirmation() -> None:
    """Reuses the same large-entry confirmation guard every other manual
    command already applies (`_large_entry_threshold`) -- no new policy."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory,
            household_id=household,
            credit_card_account_id=cartao,
            amount="50000.00",
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="50000.00"),
        )
        assert response.status_code == 409, response.text
        with session_factory() as db:
            assert db.get(Transaction, invoice_id).linked_transaction_id is None

        confirmed = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=invoice_id,
                paying_account_id=itau,
                amount="50000.00",
                confirmed_large_amount=True,
            ),
        )
        assert confirmed.status_code == 201, confirmed.text


def test_manual_payment_leg_cannot_be_deleted_or_have_excluded_flipped_without_unlinking() -> None:
    """A linked reconciliation leg is protected the same way a transfer leg
    is: category/`excluded` cannot be changed and the row cannot be deleted
    in isolation, because that would orphan the still-linked counterpart or
    fabricate a second expense for an already-settled invoice (INV-002)."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        checking_id = response.json()["checking_transaction_id"]

        patched = client.patch(f"/api/transactions/{checking_id}", json={"excluded": False})
        assert patched.status_code == 422, patched.text

        deleted = client.delete(f"/api/transactions/{checking_id}")
        assert deleted.status_code == 409, deleted.text

        unlinked = client.post(
            "/api/card-payment-reconciliations/unlink",
            json={"transaction_id": checking_id, "reason": "Vínculo incorreto para teste"},
        )
        assert unlinked.status_code == 200, unlinked.text

        deleted_after_unlink = client.delete(f"/api/transactions/{checking_id}")
        assert deleted_after_unlink.status_code == 200, deleted_after_unlink.text


def test_card_invoice_obligations_reflect_only_provable_states() -> None:
    """"Contas a pagar" never invents a status: an unlinked invoice is
    `pending`, a linked one is `paid` -- nothing in between is fabricated."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        pending_id = _card_invoice_line(
            session_factory,
            household_id=household,
            credit_card_account_id=cartao,
            amount="900.00",
            description="Pagamento em 10 AGO",
        )
        paid_id = _card_invoice_line(
            session_factory,
            household_id=household,
            credit_card_account_id=cartao,
            amount="450.00",
            day=2,
            description="Pagamento em 12 AGO",
        )

        items = client.get("/api/card-payment-reconciliations/invoices").json()
        by_id = {item["card_transaction_id"]: item for item in items}
        assert by_id[pending_id]["status"] == "pending"
        assert by_id[pending_id]["paid_transaction_id"] is None
        assert by_id[paid_id]["status"] == "pending"

        pay = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=paid_id, paying_account_id=itau, amount="450.00"),
        )
        assert pay.status_code == 201, pay.text

        items = client.get("/api/card-payment-reconciliations/invoices").json()
        by_id = {item["card_transaction_id"]: item for item in items}
        assert by_id[pending_id]["status"] == "pending"
        assert by_id[paid_id]["status"] == "paid"
        assert by_id[paid_id]["paid_transaction_id"] == pay.json()["checking_transaction_id"]


def test_combined_redemption_then_payment_flow_creates_two_separate_facts() -> None:
    """`docs/GO_LIVE_MANUAL_UX_PLAN.md`'s worked example: resgatar do
    Privilège DI, depois pagar a fatura -- two independent canonical calls
    (slice 2's redemption command, this slice's payment command), never a
    new orchestration endpoint, never a mutation of
    `FinancialProfile.investment_balance`, and R$ 0 of new expense from the
    payment leg."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="5000.00"
        )

        profile = client.get("/api/profile").json()
        profile["investment_balance"] = "10000.00"
        assert client.put("/api/profile", json=profile).status_code == 200

        before = client.get("/api/dashboard?month=2026-08").json()

        redemption = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-09",
                "description": "Resgate do Privilège DI para pagar fatura",
                "amount": 4000,
                "movement_type": "redemption",
                "account_id": itau,
            },
        )
        assert redemption.status_code == 201, redemption.text

        payment = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=invoice_id,
                paying_account_id=itau,
                amount="5000.00",
                confirmed_large_amount=True,
            ),
        )
        assert payment.status_code == 201, payment.text

        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["spending"] == before["spending"]
        assert after["cash_in"] == before["cash_in"]
        assert after["investment_balance"] == before["investment_balance"] == 10000.0

        with session_factory() as db:
            redemption_row = db.get(Transaction, redemption.json()["id"])
            payment_row = db.get(Transaction, payment.json()["checking_transaction_id"])
            assert redemption_row.id != payment_row.id
            assert redemption_row.transaction_type == "transfer"
            assert redemption_row.amount == Decimal("4000.00")
            assert payment_row.transaction_type == "reconciliation"
            assert payment_row.amount == Decimal("-5000.00")
            # Two separate, independently auditable facts -- no shared group.
            assert redemption_row.transfer_group_id != payment_row.linked_transaction_id


def test_no_redemption_is_ever_created_automatically_by_paying_an_invoice() -> None:
    """Paying an invoice never creates, infers or requires a redemption --
    the combined flow is opt-in only, driven entirely by a separate,
    explicit user action."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transfer_legs = db.scalars(
                select(Transaction).where(Transaction.transaction_type == "transfer")
            ).all()
            assert transfer_legs == []


def test_paid_invoice_appears_in_ledger_without_double_counting_purchases() -> None:
    """The invoice's own purchases (a separate, already-existing expense
    fact this slice does not touch) and the new payment leg both appear in
    the ledger, but only the purchase counts as spending -- the payment
    never does (INV-002 regression at the ledger/dashboard level)."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        purchase = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-03",
                "description": "Compra na fatura",
                "amount": 300,
                "movement_type": "expense",
                "account_id": cartao,
                "category_name": "Compras",
                "competence": "2026-08",
            },
        )
        assert purchase.status_code == 201, purchase.text
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="300.00"
        )

        before = client.get("/api/dashboard?month=2026-08").json()
        payment = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="300.00"),
        )
        assert payment.status_code == 201, payment.text
        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["spending"] == before["spending"]

        rows = {row["id"]: row for row in client.get("/api/transactions?month=2026-08").json()}
        assert purchase.json()["id"] in rows
        assert payment.json()["checking_transaction_id"] in rows
        assert rows[payment.json()["checking_transaction_id"]]["type"] == "reconciliation"


def test_pay_invoice_links_existing_unique_bank_debit_instead_of_creating_new_leg() -> None:
    """REQUEST_CHANGES 2026-09-07 (`BLOQUEIO DE MERGE`), regression (a) + (d):
    a single already-imported, unlinked checking-side debit that
    deterministically matches the invoice must be linked -- never
    duplicated with a second, manually-created leg for the very same
    real-world payment -- and must not spawn an artificial `DuplicateGroup`
    or override the imported row's own classification."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="1500.00"
        )
        bank_debit_id = _bank_debit_line(
            session_factory, household_id=household, checking_account_id=itau, amount="-1500.00", day=10
        )

        with session_factory() as db:
            transactions_before = set(db.scalars(select(Transaction.id)).all())
        assert transactions_before == {invoice_id, bank_debit_id}

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["checking_transaction_id"] == bank_debit_id
        assert body["review_items"] == 0

        with session_factory() as db:
            transactions_after = set(db.scalars(select(Transaction.id)).all())
            assert transactions_after == transactions_before  # zero new rows

            invoice = db.get(Transaction, invoice_id)
            bank_debit = db.get(Transaction, bank_debit_id)
            assert invoice.linked_transaction_id == bank_debit.id
            assert bank_debit.linked_transaction_id == invoice.id

            # (d): no artificial DuplicateGroup, no source-precedence override --
            # `link_card_payment` only ever touches `linked_transaction_id`.
            assert db.scalar(select(DuplicateGroup)) is None
            assert bank_debit.canonical_status == "unassigned"
            assert bank_debit.possible_duplicate is False
            assert bank_debit.account_id == itau


def test_pay_invoice_rejects_ambiguous_bank_evidence_without_creating_or_auto_resolving() -> None:
    """REQUEST_CHANGES 2026-09-07, regression (b): two equally eligible
    bank-debit candidates for the same invoice must never be auto-resolved
    nor papered over by fabricating a third, manual leg -- the human must
    resolve the ambiguity explicitly via `POST
    /card-payment-reconciliations/link` first."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="1500.00"
        )
        debit_one = _bank_debit_line(
            session_factory,
            household_id=household,
            checking_account_id=itau,
            amount="-1500.00",
            day=10,
            description="Débito 1",
        )
        debit_two = _bank_debit_line(
            session_factory,
            household_id=household,
            checking_account_id=itau,
            amount="-1500.00",
            day=12,
            description="Débito 2",
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 409, response.text
        assert "ambígua" in response.json()["detail"]

        with session_factory() as db:
            assert db.get(Transaction, invoice_id).linked_transaction_id is None
            assert db.get(Transaction, debit_one).linked_transaction_id is None
            assert db.get(Transaction, debit_two).linked_transaction_id is None
            transactions = set(db.scalars(select(Transaction.id)).all())
            assert transactions == {invoice_id, debit_one, debit_two}  # no new row
            assert db.scalar(select(DuplicateGroup)) is None


def test_pay_invoice_creates_manual_leg_when_no_bank_evidence_matches() -> None:
    """REQUEST_CHANGES 2026-09-07, regression (c): an existing checking-side
    reconciliation row that does not match this invoice (wrong amount) must
    not block, or get hijacked into, a manual payment -- the manual leg is
    still created exactly as before this review round."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="1500.00"
        )
        unrelated_debit_id = _bank_debit_line(
            session_factory, household_id=household, checking_account_id=itau, amount="-42.00", day=10
        )

        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 201, response.text
        checking_id = response.json()["checking_transaction_id"]
        assert checking_id not in (invoice_id, unrelated_debit_id)

        with session_factory() as db:
            leg = db.get(Transaction, checking_id)
            assert leg.account_id == itau
            assert leg.amount == Decimal("-1500.00")
            assert leg.linked_transaction_id == invoice_id
            assert db.get(Transaction, unrelated_debit_id).linked_transaction_id is None
            transactions = set(db.scalars(select(Transaction.id)).all())
            assert transactions == {invoice_id, unrelated_debit_id, checking_id}


def test_pay_invoice_ignores_already_linked_checking_row_as_evidence() -> None:
    """A checking-side reconciliation row that is already linked to a
    *different* card invoice must never count as a candidate for this one --
    `_deterministic_bank_evidence_for_card` mirrors
    `list_card_payment_reconciliations`'s own exclusion of resolved rows
    from the candidate graph."""
    client, session_factory = _client()
    with client:
        household = _setup_household(client, session_factory)
        itau = _create_account(client, name="Itaú Corrente")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")
        other_invoice_id = _card_invoice_line(
            session_factory,
            household_id=household,
            credit_card_account_id=cartao,
            amount="1500.00",
            day=1,
            competence="2026-07",
            description="Fatura de julho",
        )
        pay_other = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(
                card_transaction_id=other_invoice_id,
                paying_account_id=itau,
                amount="1500.00",
                booked_at="2026-07-10",
            ),
        )
        assert pay_other.status_code == 201, pay_other.text
        already_linked_checking_id = pay_other.json()["checking_transaction_id"]

        invoice_id = _card_invoice_line(
            session_factory, household_id=household, credit_card_account_id=cartao, amount="1500.00"
        )
        response = client.post(
            "/api/card-payment-reconciliations/pay",
            json=_pay_payload(card_transaction_id=invoice_id, paying_account_id=itau, amount="1500.00"),
        )
        assert response.status_code == 201, response.text
        new_checking_id = response.json()["checking_transaction_id"]
        assert new_checking_id != already_linked_checking_id

        with session_factory() as db:
            assert db.get(Transaction, already_linked_checking_id).linked_transaction_id == other_invoice_id
            assert db.get(Transaction, new_checking_id).linked_transaction_id == invoice_id
