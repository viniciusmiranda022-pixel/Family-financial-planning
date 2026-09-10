"""Tests for the manual go-live financial flows -- slice 4: Lançamentos
como livro-razão.

Work Order: `docs/WORK_ORDER_MANUAL_LEDGER.md`.

Scope covered here: `GET /api/transactions` becoming the read-only
livro-razão surface over every canonical fact slices 1-3 (and import) already
persist -- new `movement_type`/`origin` filters and provenance fields
(`classification_source`/`origin_label`, `document_id`/`document_name`/
`document_type`, `linked_transaction_id` and its counterpart's
description/date/amount, `canonical_status`, `duplicate_group_id`,
`competence`). No new write endpoint, no new financial rule: every value
this module asserts on was already computed by a slice 1-3 canonical
command (`create_manual_transaction`, `create_internal_transfer`,
`pay_card_invoice`, `confirm_capture`, the document import pipeline) or is a
household-scoped read of an already-existing column.

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_manual_transfers.py` and
`tests/test_manual_payables_card_payment.py`.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-manual-ledger-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "manual-ledger-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, AuditEvent, Category, Document, Household, Transaction, User  # noqa: E402
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
    client, *, household_name="Família Livro-Razão", username="admin-ledger", password="senha-local-segura"
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
        return household.id


def _create_account(
    client,
    *,
    name,
    account_type="checking",
    last_four=None,
    card_closing_day=None,
    card_due_day=None,
):
    payload = {"name": name, "account_type": account_type}
    if last_four is not None:
        payload["last_four"] = last_four
    if card_closing_day is not None:
        payload["card_closing_day"] = card_closing_day
    if card_due_day is not None:
        payload["card_due_day"] = card_due_day
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _non_system_category_ids(client, count: int = 1) -> list[str]:
    categories = client.get("/api/categories").json()
    ids = [
        item["id"]
        for item in categories
        if item["name"] not in {"Conciliação", "Transferência patrimonial", "Transferência interna", "Receitas"}
    ]
    assert len(ids) >= count
    return ids[:count]


def _non_system_category_id(client) -> str:
    return _non_system_category_ids(client, 1)[0]


def _patrimonial_category_id(client) -> str:
    categories = client.get("/api/categories").json()
    (category,) = (item for item in categories if item["name"] == "Transferência patrimonial")
    return category["id"]


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
    PDF import would persist it (same fixture shape as
    `tests/test_manual_payables_card_payment.py::_card_invoice_line`)."""
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


def _legacy_transfer_row(
    session_factory,
    *,
    household_id: str,
    account_id: str | None,
    category_id: str | None,
    amount: str = "150.00",
    description: str = "Transferência legada",
    day: int = 6,
) -> str:
    """A `transaction_type="transfer"` row persisted the way data predating
    the canonical `/api/transfers` category convention (or any other
    inconsistent/unresolved reference) would look: never produced by a
    slice 1-3 canonical command, but a real state the read model must not
    silently mislabel. No API in this codebase creates this shape -- it is
    inserted directly to reproduce exactly the historical/legacy fact the
    PR #50 review flagged."""
    with session_factory() as db:
        row = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=date(2026, 8, day),
            occurred_at=date(2026, 8, day),
            description=description,
            normalized_description=description.upper(),
            amount=Decimal(amount),
            transaction_type="transfer",
            classification_source="legacy",
            fingerprint=f"legacytransfer{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
        )
        db.add(row)
        db.commit()
        return row.id


def _csv(rows: str) -> bytes:
    return ("date,title,amount\n" + rows).encode()


def _create_manual_transaction(client, **overrides) -> dict:
    payload = {
        "booked_at": "2026-08-12",
        "description": "Lançamento manual",
        "amount": 100,
        "movement_type": "expense",
        "account_id": None,
    }
    payload.update(overrides)
    response = client.post("/api/transactions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _ledger(client, **params):
    response = client.get("/api/transactions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_ledger_lists_facts_from_every_slice_1_3_flow_plus_import() -> None:
    """Acceptance criterion 1: receita, despesa, transferência, aplicação,
    resgate, estorno, reconciliação/pagamento de fatura and an imported
    document are all queryable from the same livro-razão surface, each
    carrying the provenance the backend already computed -- never
    re-derived by this test's assertions, only read back."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        savings = _create_account(client, name="Poupança")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card", last_four="1234")
        category_id = _non_system_category_id(client)

        income = _create_manual_transaction(
            client, movement_type="income", amount=3000, account_id=checking, description="Salário de agosto"
        )
        expense = _create_manual_transaction(
            client,
            movement_type="expense",
            amount=150,
            account_id=checking,
            category_id=category_id,
            description="Supermercado",
        )
        investment = _create_manual_transaction(
            client, movement_type="investment", amount=1000, account_id=savings, description="Aplicação Privilège"
        )
        redemption = _create_manual_transaction(
            client, movement_type="redemption", amount=400, account_id=savings, description="Resgate Privilège"
        )
        refund = _create_manual_transaction(
            client, movement_type="refund", amount=50, account_id=checking, description="Estorno da loja"
        )

        transfer = client.post(
            "/api/transfers",
            json={
                "booked_at": "2026-08-15",
                "description": "Reforço da poupança",
                "amount": 200,
                "from_account_id": checking,
                "to_account_id": savings,
            },
        )
        assert transfer.status_code == 201, transfer.text
        transfer_body = transfer.json()

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-ledger"))
        invoice_id = _card_invoice_line(
            session_factory, household_id=household_id, credit_card_account_id=cartao, amount="800.00"
        )
        payment = client.post(
            "/api/card-payment-reconciliations/pay",
            json={
                "card_transaction_id": invoice_id,
                "paying_account_id": checking,
                "amount": 800,
                "booked_at": "2026-08-20",
                "description": "Pagamento da fatura Itaú",
                "confirmed": True,
            },
        )
        assert payment.status_code == 201, payment.text
        payment_body = payment.json()

        imported = client.post(
            "/api/imports",
            data={"account_id": checking, "document_type": "bank_statement"},
            files={"file": ("extrato.csv", _csv("2026-08-05,Farmácia,42.00\n"), "text/csv")},
        )
        assert imported.status_code == 201, imported.text
        imported_document_id = imported.json()["document_id"]

        rows = _ledger(client, month="2026-08", limit=500)
        by_id = {row["id"]: row for row in rows}

        assert by_id[income["id"]]["movement_type"] == "income"
        assert by_id[income["id"]]["manual"] is True
        assert by_id[income["id"]]["classification_source"] == "manual_confirmed"

        assert by_id[expense["id"]]["movement_type"] == "expense"

        assert by_id[investment["id"]]["movement_type"] == "investment"
        assert by_id[investment["id"]]["type"] == "transfer"
        assert Decimal(str(by_id[investment["id"]]["amount"])) < 0

        assert by_id[redemption["id"]]["movement_type"] == "redemption"
        assert by_id[redemption["id"]]["type"] == "transfer"
        assert Decimal(str(by_id[redemption["id"]]["amount"])) > 0

        assert by_id[refund["id"]]["movement_type"] == "refund"

        debit_row = by_id[transfer_body["from_transaction_id"]]
        credit_row = by_id[transfer_body["to_transaction_id"]]
        assert debit_row["movement_type"] == "transfer"
        assert credit_row["movement_type"] == "transfer"
        assert debit_row["transfer_group_id"] == transfer_body["transfer_group_id"]
        assert debit_row["linked_transaction_id"] == credit_row["id"]
        assert credit_row["linked_transaction_id"] == debit_row["id"]

        assert payment_body["checking_transaction_id"]
        reconciliation_rows = [row for row in rows if row["movement_type"] == "reconciliation"]
        assert len(reconciliation_rows) == 2  # invoice line + checking-side payment leg
        assert {row["linked_transaction_id"] for row in reconciliation_rows} == {
            row["id"] for row in reconciliation_rows
        }
        for row in reconciliation_rows:
            assert row["excluded"] is True

        imported_row = next(row for row in rows if row["description"] == "Farmácia")
        assert imported_row["manual"] is False
        assert imported_row["document_id"] == imported_document_id
        assert imported_row["document_name"] == "extrato.csv"
        assert imported_row["document_type"] == "bank_statement"
        assert imported_row["classification_source"] in {"local_rule", "builtin_rule"}


def test_ledger_movement_type_filter_matches_exactly_without_double_counting() -> None:
    """Filtering never re-classifies -- each of the seven canonical
    `movement_type` values returns exactly the rows that already carry that
    fact, with no extras and no omissions (acceptance criterion 4)."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        savings = _create_account(client, name="Poupança")

        investment = _create_manual_transaction(
            client, movement_type="investment", amount=500, account_id=savings, description="Aplicação"
        )
        redemption = _create_manual_transaction(
            client, movement_type="redemption", amount=300, account_id=savings, description="Resgate"
        )
        transfer = client.post(
            "/api/transfers",
            json={
                "booked_at": "2026-08-15",
                "description": "Transferência interna",
                "amount": 150,
                "from_account_id": checking,
                "to_account_id": savings,
            },
        ).json()

        investment_rows = _ledger(client, movement_type="investment")
        assert {row["id"] for row in investment_rows} == {investment["id"]}

        redemption_rows = _ledger(client, movement_type="redemption")
        assert {row["id"] for row in redemption_rows} == {redemption["id"]}

        transfer_rows = _ledger(client, movement_type="transfer")
        assert {row["id"] for row in transfer_rows} == {
            transfer["from_transaction_id"],
            transfer["to_transaction_id"],
        }
        # The patrimonial-movement rows never leak into the plain "transfer"
        # filter, and vice versa -- otherwise a household total derived by
        # summing filtered pages would double count or omit a movement.
        assert investment["id"] not in {row["id"] for row in transfer_rows}
        assert redemption["id"] not in {row["id"] for row in transfer_rows}
        assert transfer["from_transaction_id"] not in {row["id"] for row in investment_rows}
        assert transfer["from_transaction_id"] not in {row["id"] for row in redemption_rows}


def test_ledger_transfer_filter_includes_legacy_and_uncategorized_rows() -> None:
    """Regression for the PR #50 review finding: `_ledger_movement_type`
    labels *any* `transaction_type="transfer"` row without a household-scoped
    "Transferência patrimonial" category as `movement_type="transfer"` -- a
    legacy row tagged with a different/older category, or with no resolvable
    category at all, is still `"transfer"` in the unfiltered listing. The
    `movement_type=transfer` filter must select exactly that same set
    (acceptance item 1 from the review): it must never require the row to
    additionally carry the "Transferência interna" category name, since that
    name is only what the *current* `/api/transfers` canonical command
    happens to use -- it is not part of the label's own definition."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        savings = _create_account(client, name="Poupança")
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-ledger"))
        legacy_category_id = _non_system_category_id(client)

        legacy_categorized = _legacy_transfer_row(
            session_factory,
            household_id=household_id,
            account_id=checking,
            category_id=legacy_category_id,
            description="Transferência com categoria legada",
        )
        legacy_uncategorized = _legacy_transfer_row(
            session_factory,
            household_id=household_id,
            account_id=savings,
            category_id=None,
            description="Transferência sem categoria",
        )

        canonical_transfer = client.post(
            "/api/transfers",
            json={
                "booked_at": "2026-08-15",
                "description": "Transferência canônica",
                "amount": 150,
                "from_account_id": checking,
                "to_account_id": savings,
            },
        ).json()

        unfiltered = {row["id"]: row for row in _ledger(client, month="2026-08", limit=500)}
        assert unfiltered[legacy_categorized]["movement_type"] == "transfer"
        assert unfiltered[legacy_uncategorized]["movement_type"] == "transfer"

        transfer_filtered_ids = {row["id"] for row in _ledger(client, movement_type="transfer", limit=500)}
        assert legacy_categorized in transfer_filtered_ids
        assert legacy_uncategorized in transfer_filtered_ids
        assert canonical_transfer["from_transaction_id"] in transfer_filtered_ids
        assert canonical_transfer["to_transaction_id"] in transfer_filtered_ids

        # Neither legacy row is patrimonial: it must never leak into
        # investment/redemption just because it shares `transaction_type`.
        investment_or_redemption_ids = {
            row["id"]
            for row in _ledger(client, movement_type="investment", limit=500)
            + _ledger(client, movement_type="redemption", limit=500)
        }
        assert legacy_categorized not in investment_or_redemption_ids
        assert legacy_uncategorized not in investment_or_redemption_ids


def test_ledger_movement_type_filter_union_covers_every_labeled_row_without_gap() -> None:
    """Regression for the PR #50 review finding, acceptance item 2: the union
    of the seven `movement_type` filter results must equal exactly the set of
    row ids the unfiltered listing itself labels with each corresponding
    value -- no row whose own label says one thing is unreachable by the
    filter for that same value, across every canonical flow plus the
    legacy/uncategorized shapes a real household's history can contain."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        savings = _create_account(client, name="Poupança")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card", last_four="4321")
        category_id = _non_system_category_id(client)
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-ledger"))

        _create_manual_transaction(
            client, movement_type="income", amount=1000, account_id=checking, description="Salário"
        )
        _create_manual_transaction(
            client,
            movement_type="expense",
            amount=90,
            account_id=checking,
            category_id=category_id,
            description="Conta de luz",
        )
        _create_manual_transaction(
            client, movement_type="investment", amount=600, account_id=savings, description="Aplicação"
        )
        _create_manual_transaction(
            client, movement_type="redemption", amount=200, account_id=savings, description="Resgate"
        )
        _create_manual_transaction(
            client, movement_type="refund", amount=30, account_id=checking, description="Estorno"
        )
        client.post(
            "/api/transfers",
            json={
                "booked_at": "2026-08-15",
                "description": "Transferência canônica",
                "amount": 150,
                "from_account_id": checking,
                "to_account_id": savings,
            },
        )
        invoice_id = _card_invoice_line(
            session_factory, household_id=household_id, credit_card_account_id=cartao, amount="500.00"
        )
        client.post(
            "/api/card-payment-reconciliations/pay",
            json={
                "card_transaction_id": invoice_id,
                "paying_account_id": checking,
                "amount": 500,
                "booked_at": "2026-08-20",
                "description": "Pagamento da fatura",
                "confirmed": True,
            },
        )
        _legacy_transfer_row(
            session_factory,
            household_id=household_id,
            account_id=checking,
            category_id=category_id,
            description="Transferência com categoria legada",
        )
        _legacy_transfer_row(
            session_factory,
            household_id=household_id,
            account_id=savings,
            category_id=None,
            description="Transferência sem categoria",
        )

        rows = _ledger(client, month="2026-08", limit=500)
        labeled_ids_by_type: dict[str, set[str]] = {}
        for row in rows:
            labeled_ids_by_type.setdefault(row["movement_type"], set()).add(row["id"])

        for movement_type, expected_ids in labeled_ids_by_type.items():
            filtered_ids = {row["id"] for row in _ledger(client, movement_type=movement_type, limit=500)}
            assert filtered_ids == expected_ids, (
                f"movement_type={movement_type!r} filter diverges from the unfiltered label: "
                f"filter={filtered_ids!r} label={expected_ids!r}"
            )

        # Union across all seven filters accounts for every row the
        # unfiltered listing itself carries a movement_type for -- no gap.
        union_of_filters: set[str] = set()
        for movement_type in sorted(labeled_ids_by_type):
            union_of_filters |= {row["id"] for row in _ledger(client, movement_type=movement_type, limit=500)}
        assert union_of_filters == {row["id"] for row in rows}


def test_ledger_redemption_filter_includes_patrimonial_zero_amount_row() -> None:
    """Regression for the PR #50 review finding, BLOQUEIO DE MERGE #2:
    `_ledger_movement_type` labels a `transaction_type="transfer"` row whose
    category resolves to "Transferência patrimonial" as `"redemption"`
    whenever `amount` is not negative -- including `amount == 0` -- but the
    `movement_type=redemption` filter used to require `amount > 0`, so a
    zero-amount patrimonial row appeared in the unfiltered listing labeled
    `"redemption"` yet vanished under its own filter. No current manual/
    capture command can create `amount == 0` (both require `amount > 0`),
    but `Transaction.amount` carries no `CHECK != 0` constraint, so this is a
    real legacy/inconsistent state the read model must not silently drop
    from the filter that its own label promises."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        patrimonial_category_id = _patrimonial_category_id(client)
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-ledger"))

        zero_amount_row = _legacy_transfer_row(
            session_factory,
            household_id=household_id,
            account_id=checking,
            category_id=patrimonial_category_id,
            amount="0.00",
            description="Resgate patrimonial legado de valor zero",
        )

        unfiltered = {row["id"]: row for row in _ledger(client, month="2026-08", limit=500)}
        assert unfiltered[zero_amount_row]["movement_type"] == "redemption"

        redemption_ids = {row["id"] for row in _ledger(client, movement_type="redemption", limit=500)}
        assert zero_amount_row in redemption_ids

        investment_ids = {row["id"] for row in _ledger(client, movement_type="investment", limit=500)}
        assert zero_amount_row not in investment_ids

        # The label <-> filter union property (acceptance item 2) must keep
        # holding once a zero-amount patrimonial row is part of the data.
        rows = _ledger(client, month="2026-08", limit=500)
        labeled_ids_by_type: dict[str, set[str]] = {}
        for row in rows:
            labeled_ids_by_type.setdefault(row["movement_type"], set()).add(row["id"])
        for movement_type, expected_ids in labeled_ids_by_type.items():
            filtered_ids = {row["id"] for row in _ledger(client, movement_type=movement_type, limit=500)}
            assert filtered_ids == expected_ids, (
                f"movement_type={movement_type!r} filter diverges from the unfiltered label: "
                f"filter={filtered_ids!r} label={expected_ids!r}"
            )


def test_ledger_transfer_filter_household_isolation_holds_for_legacy_rows() -> None:
    """Regression for the PR #50 review finding, acceptance item 4: the fixed
    `movement_type=transfer` predicate joins through `Category` -- it must
    keep scoping strictly by the caller's own household, including for a
    legacy row whose category (or absence of one) sits outside the
    "Transferência interna"/"Transferência patrimonial" pair this filter
    used to hard-code."""
    client, session_factory = _client()
    with client:
        _setup_household(client, household_name="Família A", username="admin-a-legacy")
        checking_a = _create_account(client, name="Conta A")
        with session_factory() as db:
            household_a_id = db.scalar(select(User.household_id).where(User.username == "admin-a-legacy"))
        legacy_a = _legacy_transfer_row(
            session_factory,
            household_id=household_a_id,
            account_id=checking_a,
            category_id=None,
            description="Transferência legada da família A",
        )

        household_b_id = _create_household_admin(
            session_factory, household_name="Família B", username="admin-b-legacy", password="senha-local-segura"
        )
        login_b = client.post(
            "/api/auth/login", json={"username": "admin-b-legacy", "password": "senha-local-segura"}
        )
        assert login_b.status_code == 200
        checking_b = _create_account(client, name="Conta B")
        legacy_b = _legacy_transfer_row(
            session_factory,
            household_id=household_b_id,
            account_id=checking_b,
            category_id=None,
            description="Transferência legada da família B",
        )

        transfer_rows_b = {row["id"] for row in _ledger(client, movement_type="transfer", limit=500)}
        assert legacy_b in transfer_rows_b
        assert legacy_a not in transfer_rows_b

        login_a = client.post(
            "/api/auth/login", json={"username": "admin-a-legacy", "password": "senha-local-segura"}
        )
        assert login_a.status_code == 200
        transfer_rows_a = {row["id"] for row in _ledger(client, movement_type="transfer", limit=500)}
        assert legacy_a in transfer_rows_a
        assert legacy_b not in transfer_rows_a


def test_ledger_origin_filter_matches_classification_source_channel() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        category_id = _non_system_category_id(client)

        manual = _create_manual_transaction(
            client,
            movement_type="expense",
            amount=80,
            account_id=checking,
            category_id=category_id,
            description="Compra manual",
        )

        capture_preview = client.post(
            "/api/captures/preview",
            data={
                "text": "Gastei R$ 60 com combustível ontem",
                "account_id": checking,
                "document_type": "auto",
            },
        )
        assert capture_preview.status_code == 201, capture_preview.text
        capture_data = capture_preview.json()
        capture_confirm = client.post(
            f"/api/captures/{capture_data['id']}/confirm",
            json={"items": capture_data["items"]},
        )
        assert capture_confirm.status_code == 200, capture_confirm.text
        captured_id = capture_confirm.json()["result"]["transactions"][0]

        imported = client.post(
            "/api/imports",
            data={"account_id": checking, "document_type": "bank_statement"},
            files={"file": ("extrato.csv", _csv("2026-08-05,Padaria,12.00\n"), "text/csv")},
        )
        assert imported.status_code == 201, imported.text

        manual_rows = {row["id"] for row in _ledger(client, origin="manual")}
        capture_rows = {row["id"] for row in _ledger(client, origin="capture")}
        import_rows = {row["id"] for row in _ledger(client, origin="import")}

        assert manual["id"] in manual_rows
        assert manual["id"] not in capture_rows
        assert manual["id"] not in import_rows

        assert captured_id in capture_rows
        assert captured_id not in manual_rows

        import_descriptions = {row["description"] for row in _ledger(client, origin="import")}
        assert "Padaria" in import_descriptions
        assert len(import_rows) >= 1


def test_ledger_rejects_invalid_filters() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        assert _bad_filter_status(client, movement_type="not-a-type") == 422
        assert _bad_filter_status(client, origin="not-an-origin") == 422


def _bad_filter_status(client, **params) -> int:
    return client.get("/api/transactions", params=params).status_code


def test_ledger_requires_authentication() -> None:
    client, _ = _client()
    with client:
        response = client.get("/api/transactions")
        assert response.status_code == 401


def test_ledger_household_isolation() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client, household_name="Família A", username="admin-a")
        checking_a = _create_account(client, name="Conta A")
        fact_a = _create_manual_transaction(
            client, movement_type="income", amount=500, account_id=checking_a, description="Renda da família A"
        )

        _create_household_admin(
            session_factory, household_name="Família B", username="admin-b", password="senha-local-segura"
        )
        login_b = client.post("/api/auth/login", json={"username": "admin-b", "password": "senha-local-segura"})
        assert login_b.status_code == 200
        checking_b = _create_account(client, name="Conta B")
        fact_b = _create_manual_transaction(
            client, movement_type="income", amount=700, account_id=checking_b, description="Renda da família B"
        )

        rows_b = _ledger(client, limit=500)
        ids_b = {row["id"] for row in rows_b}
        assert fact_b["id"] in ids_b
        assert fact_a["id"] not in ids_b
        assert not any("família a" in row["description"].lower() for row in rows_b)

        login_a = client.post("/api/auth/login", json={"username": "admin-a", "password": "senha-local-segura"})
        assert login_a.status_code == 200
        rows_a = _ledger(client, limit=500)
        ids_a = {row["id"] for row in rows_a}
        assert fact_a["id"] in ids_a
        assert fact_b["id"] not in ids_a


def test_ledger_never_leaks_cross_household_data_through_inconsistent_references() -> None:
    """Acceptance criterion 5, explicitly "inclusive sob estado inconsistente
    de referência": a `Transaction` genuinely owned by household A whose
    `account_id`/`category_id`/`document_id`/`linked_transaction_id` happen
    to reference household B's rows (corruption no canonical command
    produces, but that a defensive read must never trust blindly) must
    still surface as household A's own fact -- without exposing household
    B's account/category/document/linked-transaction name or id."""
    client, session_factory = _client()
    with client:
        _setup_household(client, household_name="Família A Corrompida", username="admin-a-corrupt")
        with session_factory() as db:
            household_a_id = db.scalar(select(User.household_id).where(User.username == "admin-a-corrupt"))
        _create_account(client, name="Conta A")

        other_household_id = _create_household_admin(
            session_factory,
            household_name="Outra Família Secreta",
            username="admin-b-corrupt",
            password="senha-local-segura",
        )
        with session_factory() as db:
            foreign_account = Account(
                household_id=other_household_id, name="Conta Secreta B", account_type="checking"
            )
            foreign_category = Category(
                household_id=other_household_id, name="Categoria Secreta B", color="#000000"
            )
            db.add_all([foreign_account, foreign_category])
            db.flush()

            foreign_document = Document(
                household_id=other_household_id,
                original_name="extrato-sigiloso-b.csv",
                document_type="bank_statement",
                sha256="f" * 64,
                encrypted_path="/dev/null",
                status="imported",
            )
            db.add(foreign_document)
            db.flush()

            foreign_transaction = Transaction(
                household_id=other_household_id,
                account_id=foreign_account.id,
                category_id=foreign_category.id,
                booked_at=date(2026, 8, 1),
                description="Lançamento sigiloso da família B",
                normalized_description="LANCAMENTO SIGILOSO DA FAMILIA B",
                amount=Decimal("-99.00"),
                transaction_type="expense",
                fingerprint="foreign".ljust(64, "0"),
                source_priority=50,
                confidence=Decimal("1"),
            )
            db.add(foreign_transaction)
            db.flush()

            corrupted = Transaction(
                household_id=household_a_id,
                account_id=foreign_account.id,
                category_id=foreign_category.id,
                document_id=foreign_document.id,
                linked_transaction_id=foreign_transaction.id,
                booked_at=date(2026, 8, 10),
                description="Fato corrompido da família A",
                normalized_description="FATO CORROMPIDO DA FAMILIA A",
                amount=Decimal("-42.00"),
                transaction_type="expense",
                fingerprint="corrupted".ljust(64, "0"),
                source_priority=50,
                confidence=Decimal("1"),
                reviewed=True,
            )
            db.add(corrupted)
            db.commit()
            corrupted_id = corrupted.id

        rows = _ledger(client, month="2026-08", limit=500)
        by_id = {row["id"]: row for row in rows}
        assert corrupted_id in by_id
        row = by_id[corrupted_id]
        assert row["account"] == ""
        assert row["account_id"] is None
        assert row["category"] == "Revisar"
        assert row["category_id"] is None
        assert row["document_id"] is None
        assert row["document_name"] is None
        assert row["linked_transaction_id"] is None
        assert row["linked_description"] is None

        raw_text = client.get("/api/transactions", params={"month": "2026-08", "limit": 500}).text
        assert "Secreta" not in raw_text
        assert "sigiloso" not in raw_text.lower()
        assert "extrato-sigiloso-b.csv" not in raw_text


def test_ledger_edit_and_delete_guards_stay_enforced_for_linked_rows() -> None:
    """Acceptance criterion 6: the livro-razão is a query surface over the
    same guarded mutation endpoints slices 1-3 already shipped -- it must
    not become a second, looser path to break a transfer/reconciliation
    link. Regression for `update_transaction`/`delete_manual_transaction`'s
    existing guards, exercised again here because this slice is the one
    that makes those linked rows visible together in one screen."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        savings = _create_account(client, name="Poupança")
        cartao = _create_account(client, name="Itaú Cartão", account_type="credit_card")

        transfer = client.post(
            "/api/transfers",
            json={
                "booked_at": "2026-08-15",
                "description": "Reforço",
                "amount": 100,
                "from_account_id": checking,
                "to_account_id": savings,
            },
        ).json()
        leg_id = transfer["from_transaction_id"]

        patch_excluded = client.patch(f"/api/transactions/{leg_id}", json={"excluded": False})
        assert patch_excluded.status_code == 422

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-ledger"))
        invoice_id = _card_invoice_line(
            session_factory, household_id=household_id, credit_card_account_id=cartao, amount="500.00"
        )
        payment = client.post(
            "/api/card-payment-reconciliations/pay",
            json={
                "card_transaction_id": invoice_id,
                "paying_account_id": checking,
                "amount": 500,
                "booked_at": "2026-08-20",
                "description": "Pagamento fatura",
                "confirmed": True,
            },
        )
        assert payment.status_code == 201, payment.text
        checking_leg_id = payment.json()["checking_transaction_id"]

        delete_linked = client.delete(f"/api/transactions/{checking_leg_id}")
        assert delete_linked.status_code == 409

        patch_linked_excluded = client.patch(
            f"/api/transactions/{checking_leg_id}", json={"excluded": False}
        )
        assert patch_linked_excluded.status_code == 422


def test_ledger_reflects_manual_edit_with_audit_trail() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente")
        category_id, other_category_id = _non_system_category_ids(client, 2)
        transaction = _create_manual_transaction(
            client, movement_type="expense", amount=90, account_id=checking, category_id=category_id
        )

        patch = client.patch(
            f"/api/transactions/{transaction['id']}",
            json={"category_id": other_category_id, "reviewed": True},
        )
        assert patch.status_code == 200, patch.text

        rows = _ledger(client, limit=500)
        row = next(item for item in rows if item["id"] == transaction["id"])
        assert row["category_id"] == other_category_id

        with session_factory() as db:
            events = db.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == transaction["id"], AuditEvent.event_type == "transaction.update"
                )
            ).all()
            assert len(events) == 1


def test_ledger_shows_installment_facts_without_duplication() -> None:
    """Fluxo mínimo 3 (`docs/GO_LIVE_MANUAL_UX_PLAN.md`): the ledger is a
    read of persisted `Transaction` rows only -- it never adds a synthetic
    "projected" row for a future installment. Two real installments of the
    same parcelada purchase show up as exactly two rows, each carrying its
    own `installment_current/total`, never merged, inflated or duplicated
    by the ledger's own query/serialization."""
    client, _ = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client,
            name="Itaú Cartão",
            account_type="credit_card",
            last_four="4321",
            card_closing_day=25,
            card_due_day=25,
        )
        category_id = _non_system_category_id(client)

        first = _create_manual_transaction(
            client,
            movement_type="expense",
            amount=100,
            account_id=cartao,
            category_id=category_id,
            description="Compra parcelada",
            installment_current=1,
            installment_total=3,
            competence="2026-08",
        )
        second = _create_manual_transaction(
            client,
            movement_type="expense",
            amount=100,
            account_id=cartao,
            category_id=category_id,
            description="Compra parcelada",
            installment_current=2,
            installment_total=3,
            booked_at="2026-09-12",
            competence="2026-09",
        )

        rows = _ledger(client, limit=500)
        matching = {row["id"]: row for row in rows if row["description"] == "Compra parcelada"}
        # Exactly the two real installments -- never a third, projected row
        # for the still-future 3/3 installment, and neither row merged or
        # duplicated by the ledger's own query/serialization.
        assert set(matching) == {first["id"], second["id"]}
        assert matching[first["id"]]["installment"] == "1/3"
        assert matching[second["id"]]["installment"] == "2/3"
