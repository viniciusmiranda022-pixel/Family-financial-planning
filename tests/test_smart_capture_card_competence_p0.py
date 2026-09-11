"""Regression suite for issue #84 (P0 — Smart Capture must enforce canonical
credit-card competence).

## Diagnosis

A historical audit found two real `capture_confirmed` credit-card
transactions persisted with a `competence` that diverged from the invoice
cycle:

- Itaú Personnalité (`card_closing_day=2`, `card_due_day=8`) — purchase on
  2026-09-09 was persisted as `2026-09`; the canonical competence is
  `2026-10`.
- Nubank Cartão (`card_closing_day=24`, `card_due_day=1`) — purchase on
  2026-09-10 was persisted as `2026-09`; the canonical competence is
  `2026-10`.

The historical rows were corrected directly in PostgreSQL after a validated
backup and dry-run rollback; that data repair is out of scope here. This
suite is only about proving the *code path* cannot reproduce the defect.

Tracing `POST /api/captures/{capture_id}/confirm` (`confirm_capture` in
`app/api.py`) against the git history shows the root cause was already fixed
by commit `873c371` ("fix: route every card-expense consumer through the
single canonical competence helper", merged to `main` via PR #81 on
2026-09-10T17:02:15Z, well before this main branch's current head): before
that commit, `confirm_capture` computed
`competence=proposal.booked_at.strftime("%Y-%m")` unconditionally -- a
second, parallel calculation that never consulted the account's configured
card cycle, exactly matching the reported symptom. After that commit
(present on the `main` this branch is based on --
`git merge-base --is-ancestor 873c371 <base>` confirms it), expense
confirmations already resolve competence through the single canonical
`app.services.card_competence.resolve_expense_competence` ->
`card_invoice_competence` helper, the same one `create_manual_transaction`
uses. The two historical rows were therefore almost certainly confirmed
before that fix was deployed on 2026-09-10, not produced by a bug still
present in the current code.

What was still missing -- and what this suite adds -- is:

1. dedicated regression coverage pinned to the *exact* real production card
   configurations and the *exact* historical defect dates named in the
   issue, rather than only the hypothetical configurations already covered
   by `tests/test_card_open_invoice_competence.py` (PR #81's own hotfix
   suite);
2. proof that a Smart Capture transaction proposal cannot smuggle an
   explicit `competence` (the schema field exists for payroll items and is
   not kind-restricted) past the canonical resolver for a card expense --
   this PR additionally hardens `confirm_capture` to forward any such value
   into `resolve_expense_competence` instead of silently discarding it
   unread, so a conflicting value is explicitly rejected (422, fail-closed)
   rather than merely happening to be safe because it was never looked at;
3. proof that non-card confirmations remain byte-for-byte unaffected.

No change was made to the competence *formula* itself
(`app/services/card_competence.py`) -- only `confirm_capture`'s handling of
an explicit, client-supplied `competence` on a card expense proposal.
"""

import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", f"sqlite:////tmp/ffp-smart-capture-competence-p0-{uuid.uuid4().hex}.sqlite"
)
os.environ.setdefault("SECRET_KEY", "smart-capture-competence-p0-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Transaction  # noqa: E402


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


def _setup_household(client, *, username="admin-smart-capture-p0"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Smart Capture P0",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201


def _create_card(client, *, name, closing_day, due_day):
    response = client.post(
        "/api/accounts",
        json={
            "name": name,
            "account_type": "credit_card",
            "card_closing_day": closing_day,
            "card_due_day": due_day,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_checking(client, *, name="Conta corrente"):
    response = client.post("/api/accounts", json={"name": name, "account_type": "checking"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _category_id(client, name="Mercado e itens domésticos"):
    categories = client.get("/api/categories").json()
    return next(item["id"] for item in categories if item["name"] == name)


def _preview(client, *, text, account_id):
    preview = client.post(
        "/api/captures/preview",
        data={"text": text, "account_id": account_id, "document_type": "auto"},
    )
    assert preview.status_code == 201, preview.text
    return preview.json()


def _confirm_expense(client, *, account_id, booked_at, extra=None):
    category_id = _category_id(client)
    capture_data = _preview(client, text="Gastei R$ 200 com compras", account_id=account_id)
    items = capture_data["items"]
    items[0].update(
        {
            "booked_at": booked_at,
            "movement_type": "expense",
            "account_id": account_id,
            "category_id": category_id,
            **(extra or {}),
        }
    )
    return client.post(f"/api/captures/{capture_data['id']}/confirm", json={"items": items})


# ---------------------------------------------------------------------------
# 1. Itaú Personnalité -- real production cycle: closes day 2, due day 8.
#    due_day (8) >= closing_day (2) -> due month == closing month.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("booked_at", "expected_competence"),
    [
        ("2026-09-02", "2026-09"),
        ("2026-09-03", "2026-10"),
        # Regression for the historical Itaú fuel defect (issue #84): this
        # exact purchase date was persisted as `2026-09` in production before
        # commit 873c371; it must resolve to `2026-10`.
        ("2026-09-09", "2026-10"),
    ],
)
def test_itau_close2_due8_confirm_capture_competence(booked_at, expected_competence):
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_card(client, name="Itaú Personnalité", closing_day=2, due_day=8)

        confirm = _confirm_expense(client, account_id=cartao, booked_at=booked_at)
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.booked_at.isoformat() == booked_at
            assert transaction.competence == expected_competence


# ---------------------------------------------------------------------------
# 2. Mercado Pago -- real production cycle: closes day 9, due day 14.
#    due_day (14) >= closing_day (9) -> due month == closing month.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("booked_at", "expected_competence"),
    [
        ("2026-09-09", "2026-09"),
        ("2026-09-10", "2026-10"),
    ],
)
def test_mercado_pago_close9_due14_confirm_capture_competence(booked_at, expected_competence):
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_card(client, name="Mercado Pago", closing_day=9, due_day=14)

        confirm = _confirm_expense(client, account_id=cartao, booked_at=booked_at)
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == expected_competence


# ---------------------------------------------------------------------------
# 3. Nubank Cartão -- real production cycle: closes day 24, due day 1.
#    due_day (1) < closing_day (24) -> due month is one month after closing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("booked_at", "expected_competence"),
    [
        ("2026-09-24", "2026-10"),
        ("2026-09-25", "2026-11"),
        # Regression for the historical Nubank "Claude AI" defect (issue
        # #84): this exact purchase date was persisted as `2026-09` in
        # production before commit 873c371; it must resolve to `2026-10`.
        ("2026-09-10", "2026-10"),
    ],
)
def test_nubank_close24_due1_confirm_capture_competence(booked_at, expected_competence):
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_card(client, name="Nubank Cartão", closing_day=24, due_day=1)

        confirm = _confirm_expense(client, account_id=cartao, booked_at=booked_at)
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == expected_competence


# ---------------------------------------------------------------------------
# 4. A card expense proposal must never be able to smuggle an explicit,
#    conflicting `competence` past the canonical resolver.
# ---------------------------------------------------------------------------

def test_confirm_capture_card_expense_rejects_conflicting_explicit_competence():
    """`CaptureItemRequest.competence` exists for payroll items and is not
    kind-restricted, so nothing in the schema stops a transaction proposal
    from also carrying one. For a card expense this must never be trusted:
    a value that conflicts with the invoice cycle must be explicitly
    rejected (422, fail-closed) by the same canonical resolver
    `create_manual_transaction` uses, and no transaction may be persisted --
    not silently overridden, and not silently persisted as sent."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_card(client, name="Itaú Personnalité", closing_day=2, due_day=8)

        # 2026-09-09 canonically resolves to 2026-10 (see test above); claim
        # an explicit, conflicting 2026-09 instead.
        confirm = _confirm_expense(
            client,
            account_id=cartao,
            booked_at="2026-09-09",
            extra={"competence": "2026-09-01"},
        )
        assert confirm.status_code == 422, confirm.text

        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_confirm_capture_card_expense_accepts_explicit_competence_matching_cycle():
    """The resolver contract is a fail-closed *check*, not a blanket ban: an
    explicit competence that already agrees with the canonical calculation
    is accepted, exactly like `create_manual_transaction`."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        cartao = _create_card(client, name="Itaú Personnalité", closing_day=2, due_day=8)

        confirm = _confirm_expense(
            client,
            account_id=cartao,
            booked_at="2026-09-09",
            extra={"competence": "2026-10-01"},
        )
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-10"


# ---------------------------------------------------------------------------
# 5. Non-card confirmations are byte-for-byte unaffected.
# ---------------------------------------------------------------------------

def test_confirm_capture_non_card_expense_unaffected():
    """A checking-account expense has no invoice cycle: competence always
    follows `booked_at`'s own month, and an explicit competence is only
    accepted when it already matches that month (pre-existing behavior,
    untouched by this P0 -- the explicit-competence forwarding added here is
    scoped to `account.account_type == "credit_card"` only)."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        conta = _create_checking(client)

        confirm = _confirm_expense(client, account_id=conta, booked_at="2026-09-09")
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-09"


def test_confirm_capture_non_card_expense_ignores_stray_competence_field():
    """A stray `competence` on a non-card expense proposal must not change
    behavior at all (untouched by this P0's forwarding, which only applies
    to credit-card accounts) -- it is silently discarded exactly as before,
    since checking accounts have no invoice cycle for a resolver to enforce
    against."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        conta = _create_checking(client)

        confirm = _confirm_expense(
            client,
            account_id=conta,
            booked_at="2026-09-09",
            extra={"competence": "2099-01-01"},
        )
        assert confirm.status_code == 200, confirm.text
        transaction_id = confirm.json()["result"]["transactions"][0]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.competence == "2026-09"
