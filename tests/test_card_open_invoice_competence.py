"""Regression suite for the hotfix in
`docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md` (PR #81).

Diagnosis (see the PR #81 comment for the full write-up): two independent
bugs fed the reported symptom -- a card purchase made while the next
invoice is still open was recorded under `booked_at`'s month instead of
the invoice's canonical competence.

1. `create_manual_transaction` (`POST /api/transactions`) and
   `preview_manual_installment` (`GET /transactions/manual/installment-preview`)
   called `_resolve_expense_competence(..., account_name=..., account_institution=...)`
   -- keyword arguments the function no longer accepts since the card-cycle
   engine (`# FAMILY_FINANCE_CARD_CYCLES_PRIVILEGE_V13`) replaced the old
   name-based lookup. Every manual expense (card or not) crashed with a
   `TypeError`/500 before ever reaching the competence calculation.
2. `confirm_capture` (`POST /api/captures/{id}/confirm`) never called the
   canonical helper at all: it persisted
   `competence=proposal.booked_at.strftime("%Y-%m")` unconditionally for
   every confirmed transaction, including card expenses. Because Smart
   Capture confirmation was the one path that didn't already crash, this is
   the concrete mechanism that produced the report's September/October
   symptom: a card purchase confirmed via Central Inteligente always landed
   in `booked_at`'s own month, completely ignoring `card_closing_day`/
   `card_due_day`, in direct violation of INV-017.

Both paths are fixed by routing every expense through the single canonical
helper (`_resolve_expense_competence` -> `_card_invoice_competence`), never
recomputing competence a second way.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-card-competence-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "card-competence-hotfix-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import _card_invoice_competence, _resolve_expense_competence  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction, User  # noqa: E402
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


def _setup_household(client, *, username="admin-cartao"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Cartão",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201
    assert setup.json()["mfa_required"] is True
    assert setup.json()["mode"] == "enroll"
    complete_mfa_enrollment(client)


def _create_account(
    client,
    *,
    name,
    account_type="checking",
    card_closing_day=None,
    card_due_day=None,
):
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


# ---------------------------------------------------------------------------
# 1. Unit tests of the isolated canonical helper (`_card_invoice_competence`).
# ---------------------------------------------------------------------------

def test_card_invoice_competence_before_closing_stays_in_current_invoice():
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    assert _card_invoice_competence(account, date(2026, 9, 20)) == "2026-09"


def test_card_invoice_competence_on_closing_day_stays_in_current_invoice():
    """The cutoff is exclusive: a purchase made exactly on the closing day
    still belongs to the invoice that closes that day, not the next one."""
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    assert _card_invoice_competence(account, date(2026, 9, 25)) == "2026-09"


def test_card_invoice_competence_after_closing_rolls_to_next_invoice():
    """Core bug scenario: a purchase made the day after closing belongs to
    the invoice that is still open, not the one that just closed."""
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    assert _card_invoice_competence(account, date(2026, 9, 26)) == "2026-10"


def test_card_invoice_competence_rolls_over_december_to_january():
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    assert _card_invoice_competence(account, date(2026, 12, 28)) == "2027-01"


def test_card_invoice_competence_due_month_after_closing_month_when_due_day_before_closing_day():
    """FINANCIAL_RULES.md: competence follows the invoice's due month, not
    necessarily its closing month. When the configured due day is earlier
    than the closing day, the due date falls in the month *after* closing
    (a "closes late, due early next month" cycle), so the competence is one
    month past the closing month even for a purchase made before closing."""
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=24, card_due_day=2)
    # Closes 2026-09-24 (the 20th is still within that invoice), due
    # 2026-10-02 -> competence is October, not September.
    assert _card_invoice_competence(account, date(2026, 9, 20)) == "2026-10"


def test_card_invoice_competence_fails_closed_without_configured_cycle():
    account = Account(account_type="credit_card", name="Cartão sem ciclo")
    with pytest.raises(HTTPException) as exc_info:
        _card_invoice_competence(account, date(2026, 9, 20))
    assert exc_info.value.status_code == 422


def test_card_invoice_competence_non_card_account_uses_booked_at_month():
    account = Account(account_type="checking", name="Conta corrente")
    assert _card_invoice_competence(account, date(2026, 9, 26)) == "2026-09"


def test_resolve_expense_competence_accepts_explicit_value_matching_cycle():
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    resolved = _resolve_expense_competence(
        account=account, competence="2026-10", booked_at=date(2026, 9, 26)
    )
    assert resolved == "2026-10"


def test_resolve_expense_competence_rejects_explicit_value_diverging_from_cycle():
    account = Account(account_type="credit_card", name="Cartão", card_closing_day=25, card_due_day=28)
    with pytest.raises(HTTPException) as exc_info:
        _resolve_expense_competence(
            account=account, competence="2026-09", booked_at=date(2026, 9, 26)
        )
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# 2. `POST /api/transactions` (manual entry) -- the crash bug and the
#    card-cycle regression, end to end.
# ---------------------------------------------------------------------------

def test_manual_card_expense_after_closing_uses_next_invoice_competence():
    """Reproduces the Work Order's exact scenario: a card that closes on the
    2nd, purchase made well after closing while the next invoice is open --
    must be counted in the open invoice's competence, not `booked_at`'s
    month."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Compra com fatura de outubro ainda aberta",
                "amount": 200,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            # booked_at is lineage and is never rewritten.
            assert transaction.booked_at.isoformat() == "2026-09-15"
            assert transaction.occurred_at.isoformat() == "2026-09-15"
            # competence follows the still-open October invoice.
            assert transaction.competence == "2026-10"


def test_manual_card_expense_before_closing_stays_in_current_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-01",
                "description": "Compra antes do fechamento",
                "amount": 120,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2026-09"


def test_manual_card_expense_december_january_rollover():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-12-20",
                "description": "Compra de fim de ano",
                "amount": 300,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2027-01"


def test_manual_card_expense_without_configured_cycle_fails_closed():
    """A card with no persisted `card_closing_day`/`card_due_day` must never
    have a competence invented for it -- explicit 422, no transaction
    created."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(client, name="Cartão sem ciclo", account_type="credit_card")
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Compra sem ciclo configurado",
                "amount": 90,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert response.status_code == 422
        assert "fechamento" in response.json()["detail"].lower()
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_manual_non_card_expense_keeps_date_based_competence_no_regression():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        checking = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Supermercado",
                "amount": 90,
                "movement_type": "expense",
                "account_id": checking,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text
        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2026-09"


# ---------------------------------------------------------------------------
# 3. Smart Capture confirmation -- the actual production-shaped bug: this
#    path never called the canonical helper at all.
# ---------------------------------------------------------------------------

def _preview_and_confirm(client, *, text, account_id, overrides):
    preview = client.post(
        "/api/captures/preview",
        data={"text": text, "account_id": account_id, "document_type": "auto"},
    )
    assert preview.status_code == 201, preview.text
    capture_data = preview.json()
    items = capture_data["items"]
    items[0].update(overrides)
    confirm = client.post(
        f"/api/captures/{capture_data['id']}/confirm",
        json={"items": items},
    )
    return confirm


def test_confirm_capture_card_expense_after_closing_uses_next_invoice_competence():
    """The exact production symptom from the Work Order, reproduced through
    Central Inteligente / Smart Capture confirmation rather than the manual
    form: a card purchase confirmed while the next invoice is still open
    must be counted in that invoice's competence, not `booked_at`'s month."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Nubank Cartão", account_type="credit_card", card_closing_day=24, card_due_day=2
        )
        category_id = _category_id(client)

        confirm = _preview_and_confirm(
            client,
            text="Gastei R$ 200 com compras",
            account_id=cartao,
            overrides={
                "booked_at": "2026-09-25",
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.booked_at.isoformat() == "2026-09-25"
            # Closes the 24th, due the 2nd (before the closing day) -> the
            # due date rolls into November, so competence is November.
            assert transaction.competence == "2026-11"


def test_confirm_capture_card_expense_before_closing_stays_in_current_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        confirm = _preview_and_confirm(
            client,
            text="Gastei R$ 50 com compras",
            account_id=cartao,
            overrides={
                "booked_at": "2026-09-01",
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-09"


def test_confirm_capture_card_expense_without_cycle_fails_closed():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(client, name="Cartão sem ciclo", account_type="credit_card")
        category_id = _category_id(client)

        preview = client.post(
            "/api/captures/preview",
            data={"text": "Gastei R$ 30 com compras", "account_id": cartao, "document_type": "auto"},
        )
        assert preview.status_code == 201, preview.text
        capture_data = preview.json()
        items = capture_data["items"]
        items[0].update(
            {
                "booked_at": "2026-09-15",
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            }
        )
        confirm = client.post(
            f"/api/captures/{capture_data['id']}/confirm",
            json={"items": items},
        )
        assert confirm.status_code == 422
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_confirm_capture_non_expense_movement_unaffected_by_card_cycle():
    """A card *payment* (reconciliation) confirmed via capture is not a
    purchase and must keep using `booked_at`'s own month -- the card cycle
    concept only applies to expense competence (INV-002/INV-017)."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Nubank Cartão", account_type="credit_card", card_closing_day=24, card_due_day=2
        )

        confirm = _preview_and_confirm(
            client,
            text="Fatura paga do cartão, R$ 800",
            account_id=cartao,
            overrides={"booked_at": "2026-09-25", "account_id": cartao},
        )
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]
        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.transaction_type == "reconciliation"
            assert transaction.competence == "2026-09"


# ---------------------------------------------------------------------------
# 4. Parity between the persisted `competence` and the cartões/dashboard
#    view -- both must read the same canonical fact, never recompute it.
# ---------------------------------------------------------------------------

def test_credit_card_summary_and_dashboard_agree_with_persisted_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Compra com fatura de outubro ainda aberta",
                "amount": 200,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            persisted_competence = transaction.competence
        assert persisted_competence == "2026-10"

        # September's view (the purchase's booked_at month) must NOT show it.
        september_summary = client.get("/api/credit-cards/summary", params={"month": "2026-09"}).json()
        september_row = next(row for row in september_summary["rows"] if row["account_id"] == cartao)
        assert september_row["actual_spending"] == 0.0

        # October's view (the persisted competence) must show it.
        october_summary = client.get("/api/credit-cards/summary", params={"month": "2026-10"}).json()
        october_row = next(row for row in october_summary["rows"] if row["account_id"] == cartao)
        assert october_row["actual_spending"] == 200.0

        october_dashboard = client.get("/api/dashboard", params={"month": "2026-10"}).json()
        assert october_dashboard["spending"] >= 200.0


# ---------------------------------------------------------------------------
# 5. Future installment projection stays anchored on the canonical
#    competence, still deduplicated -- no regression from this fix.
# ---------------------------------------------------------------------------

def test_installment_projection_anchors_on_canonical_open_invoice_competence():
    from app.api import _future_installments

    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Geladeira parcelada com fatura aberta",
                "amount": 100,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2026-10"
            household_id = db.scalar(select(User.household_id))
            # Anchored on the October invoice competence, not booked_at's
            # September -- remaining installments are November and
            # December, never duplicated, never September/October
            # themselves (already the observed installment's own month).
            assert _future_installments(db, household_id) == {
                "2026-11": Decimal("100.00"),
                "2026-12": Decimal("100.00"),
            }


# ---------------------------------------------------------------------------
# 6. `PATCH /transactions/{id}` never recomputes competence -- the schema
#    doesn't expose `booked_at`/`account_id`/`competence` as editable
#    fields, so there is no PATCH path that could affect INV-017.
# ---------------------------------------------------------------------------

def test_patch_transaction_never_touches_persisted_competence():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_account(
            client, name="Itaú Cartão", account_type="credit_card", card_closing_day=2, card_due_day=9
        )
        category_id = _category_id(client)
        other_category_id = _category_id(client, name="Transporte")

        created = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-15",
                "description": "Compra com fatura de outubro ainda aberta",
                "amount": 200,
                "movement_type": "expense",
                "account_id": cartao,
                "category_id": category_id,
            },
        )
        assert created.status_code == 201, created.text
        transaction_id = created.json()["id"]

        patched = client.patch(
            f"/api/transactions/{transaction_id}",
            json={"category_id": other_category_id, "reviewed": True},
        )
        assert patched.status_code == 200, patched.text

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-10"
            assert transaction.booked_at.isoformat() == "2026-09-15"
            assert transaction.category_id == other_category_id

        # The schema itself never exposes booked_at/account_id/competence as
        # PATCH-able fields -- attempting to smuggle one in is a no-op,
        # confirming there is no second competence-recompute path here.
        smuggled = client.patch(
            f"/api/transactions/{transaction_id}",
            json={"competence": "2099-01"},
        )
        assert smuggled.status_code == 200, smuggled.text
        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-10"
