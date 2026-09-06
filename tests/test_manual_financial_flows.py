"""Tests for the manual go-live financial flows -- slice 1: Navegação + Entradas/Saídas.

Work Order: `docs/WORK_ORDER_MANUAL_FINANCIAL_FLOWS_GO_LIVE.md`.

Scope covered here (see the Technical Challenge / engineering review on PR 47
for the slices deliberately deferred -- `transfer`, `reconciliation`, invoice
payment and the associated `transfer_group_id` index belong to slices 2/3 and
are not implemented in this file): the manual quick-entry command
(`POST /api/transactions`, reused by the new structured `Entradas`/`Saídas`
screens) already supported `expense|income|investment|redemption|refund`.
This slice adds:

- installment fields (`installment_current`/`installment_total`) on manual
  expense creation, reusing the existing columns and the canonical
  `_future_installments` projection function already exercised by the
  capture/import paths -- no parallel calculation engine;
- `card_last_four` populated the same way `confirm_capture` and the document
  import path already do (`account.last_four`), so a manually entered card
  purchase carries the same lineage as an imported one (INV-017).

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_card_payment_reconciliation.py::test_http_endpoints_authorize_isolate_and_audit`.
"""

import json
import os
import uuid
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-manual-flows-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "manual-financial-flows-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.api import _future_installments  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, Household, Transaction, User  # noqa: E402
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


def _setup_household(client, *, household_name="Família Fluxos", username="admin-fluxos", password="senha-local-segura"):
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
    return setup.json()


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


def test_manual_income_creates_real_income_without_parallel_policy():
    """`Entradas` posts to the same canonical command as before; income is
    unaffected by this slice's changes (no card/installment fields apply)."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Salário",
                "amount": 4000,
                "movement_type": "income",
                "account_id": itau,
            },
        )
        assert response.status_code == 201, response.text
        transaction_id = response.json()["id"]

        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["cash_in"] == before["cash_in"] + 4000

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == transaction_id)
        assert row["type"] == "income"
        assert row["amount"] == 4000
        assert row["category"] == "Receitas"

        with session_factory() as db:
            event = db.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "transaction.create_manual")
            )
            assert event is not None
            assert json.loads(event.details)["movement_type"] == "income"


def test_manual_expense_at_sight_in_checking_account():
    """`Saídas` posts an ordinary at-sight expense to a checking account."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _non_system_category_id(client)

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-12",
                "description": "Supermercado",
                "amount": 250,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text
        transaction_id = response.json()["id"]

        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["spending"] == before["spending"] + 250

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == transaction_id)
        assert row["type"] == "expense"
        assert row["amount"] == -250
        assert row["installment"] is None


def test_checking_account_expense_defaults_competence_to_booked_at_month():
    """No invoice competence concern for a checking account (INV-017 is
    specifically about the *card* purchase); omitting `competence` here must
    keep the pre-existing, backward-compatible fallback to `booked_at`'s
    month -- this slice must not force an explicit competence where the
    contract never required one."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-12",
                "description": "Supermercado",
                "amount": 250,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2026-08"


def test_credit_card_expense_without_explicit_competence_is_rejected():
    """INV-017: a card purchase's competence follows the invoice, not
    necessarily the purchase date. This app has no invoice entity yet to
    derive that automatically (lands with the "Contas a pagar" slice), so
    the command must fail closed instead of fabricating
    `booked_at.strftime("%Y-%m")` -- see the engineering review on PR 47."""
    client, _ = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-29",
                "description": "Compra perto do fechamento da fatura",
                "amount": 80,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
            },
        )
        assert response.status_code == 422
        assert "competência" in response.json()["detail"].lower()


def test_credit_card_expense_persists_explicitly_confirmed_competence():
    """The confirmed competence is persisted verbatim even when it differs
    from `booked_at`'s month (e.g. a purchase near the statement's closing
    date that lands in the *following* month's invoice) -- `booked_at`
    itself is untouched, preserving it as lineage per INV-017."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-29",
                "description": "Compra perto do fechamento da fatura",
                "amount": 80,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "competence": "2026-09",
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.booked_at.isoformat() == "2026-08-29"
            assert transaction.competence == "2026-09"


def test_checking_account_expense_rejects_competence_diverging_from_booked_at():
    """Engineering review on PR 47 (head `5671ac0`): `docs/FINANCIAL_RULES.md`
    is explicit that checking accounts use the exact date of the entry, not a
    human-adjustable competence like a card's invoice month (INV-017 is
    specific to `credit_card`). `create_manual_transaction` must not silently
    persist a checking-account expense under a fabricated period that
    contradicts its own `booked_at` -- it must fail closed (422) instead."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-12",
                "description": "Supermercado",
                "amount": 250,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
                "competence": "2026-09",
            },
        )
        assert response.status_code == 422
        assert "competência" in response.json()["detail"].lower()

        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_checking_account_expense_accepts_competence_equal_to_booked_at_month():
    """A competence that merely restates `booked_at`'s own month is not a
    divergence and must not be rejected -- only a genuinely different period
    is forbidden for non-card accounts."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-12",
                "description": "Supermercado",
                "amount": 250,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
                "competence": "2026-08",
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            transaction = db.get(Transaction, response.json()["id"])
            assert transaction.competence == "2026-08"


def test_explicit_competence_rejected_outside_expense_movement_type():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Receita não tem competência de fatura",
                "amount": 100,
                "movement_type": "income",
                "account_id": itau,
                "competence": "2026-08",
            },
        )
        assert response.status_code == 422


def test_manual_expense_in_credit_card_account_carries_card_lineage():
    """A card expense (no installment) still records `card_last_four` from
    the account, the same lineage `confirm_capture`/document import already
    attach -- not a parallel rule invented for the manual form."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(
            client, name="Nubank Cartão", account_type="credit_card", last_four="4242"
        )
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Farmácia",
                "amount": 80,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "competence": "2026-08",
            },
        )
        assert response.status_code == 201, response.text
        transaction_id = response.json()["id"]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.installment_current is None
            assert transaction.installment_total is None
            assert transaction.card_last_four == "4242"


def test_manual_installment_expense_populates_installment_and_card_fields():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Geladeira parcelada",
                "amount": 300,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 2,
                "installment_total": 10,
                "competence": "2026-08",
            },
        )
        assert response.status_code == 201, response.text
        transaction_id = response.json()["id"]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.installment_current == 2
            assert transaction.installment_total == 10
            assert transaction.card_last_four is None or isinstance(transaction.card_last_four, str)

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == transaction_id)
        assert row["installment"] == "2/10"


def test_manual_installment_feeds_canonical_projection_without_duplicating_observed_installment():
    """The manual command must feed the same `_future_installments` the
    capture/import paths already use (no parallel calculation), and a later
    *observed* installment of the same purchase must not sum on top of the
    earlier one's own future projection -- INV-014/INV-015 style dedup."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        # Parcela 2/4 observed in August.
        first = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Geladeira parcelada",
                "amount": 100,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 2,
                "installment_total": 4,
                "competence": "2026-08",
            },
        )
        assert first.status_code == 201, first.text

        # Parcela 3/4 of the *same* purchase, observed the following month.
        second = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-05",
                "description": "Geladeira parcelada",
                "amount": 100,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 3,
                "installment_total": 4,
                "competence": "2026-09",
            },
        )
        assert second.status_code == 201, second.text

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-fluxos"))
            # Only the latest observed installment (3/4) should seed the
            # projection: one remaining month (October), not the union of
            # both installments' own remaining schedules.
            assert _future_installments(db, household_id) == {"2026-10": Decimal("100.00")}


def test_future_installments_anchors_schedule_on_confirmed_competence_not_booked_at():
    """INV-017 regression (engineering review on PR 47, head `7c8a25e`): the
    remaining-installment schedule fed into the canonical projection must be
    anchored on the *confirmed* invoice competence, not on `booked_at`'s
    calendar month, whenever they diverge.

    Reproduction from the review: a card purchase booked 2026-08-29 with
    invoice competence confirmed as 2026-09 and installment 1/3 belongs to
    September's invoice. Its two remaining installments must land on October
    and November -- anchoring on `booked_at` instead would (incorrectly)
    schedule September and October, making September appear simultaneously
    as the observed installment's own competence and as a projected future
    installment (double counting the same fact)."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-29",
                "description": "Compra perto do fechamento da fatura",
                "amount": 90,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        )
        assert response.status_code == 201, response.text

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-fluxos"))
            assert _future_installments(db, household_id) == {
                "2026-10": Decimal("90.00"),
                "2026-11": Decimal("90.00"),
            }


def test_future_installments_replaces_observed_competence_month_without_duplication():
    """Continuation of the scenario above: once the September installment
    (2/3) is itself observed with its own confirmed competence, it must
    replace the first observation's projection instead of adding to it, and
    the September competence must never reappear as a projected month."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        first = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-29",
                "description": "Compra perto do fechamento da fatura",
                "amount": 90,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        )
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-29",
                "description": "Compra perto do fechamento da fatura",
                "amount": 90,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 2,
                "installment_total": 3,
                "competence": "2026-10",
            },
        )
        assert second.status_code == 201, second.text

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-fluxos"))
            # Only the latest observed installment (2/3, competence 2026-10)
            # seeds the projection: one remaining month (November) -- not
            # the union with the first observation's own schedule, and never
            # 2026-09 or 2026-10 themselves (already-observed competences).
            assert _future_installments(db, household_id) == {"2026-11": Decimal("90.00")}


def test_installment_fields_require_expense_movement_type():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Receita não pode ser parcelada",
                "amount": 300,
                "movement_type": "income",
                "account_id": itau,
                "installment_current": 1,
                "installment_total": 3,
            },
        )
        assert response.status_code == 422


def test_installment_current_cannot_exceed_total():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        category_id = _non_system_category_id(client)
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Parcela inválida",
                "amount": 300,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
                "installment_current": 5,
                "installment_total": 3,
            },
        )
        assert response.status_code == 422


def test_installment_current_and_total_must_be_provided_together():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        category_id = _non_system_category_id(client)
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Só a parcela atual",
                "amount": 300,
                "movement_type": "expense",
                "account_id": itau,
                "category_id": category_id,
                "installment_current": 2,
            },
        )
        assert response.status_code == 422


def test_manual_transaction_rejects_account_from_another_household():
    """Household isolation: an account id that exists but belongs to a
    different household must never be usable for a manual entry."""
    client, session_factory = _client()
    with client:
        _setup_household(client)

        _create_household_admin(
            session_factory,
            household_name="Outra Família",
            username="admin-outra-fluxos",
            password="outra-senha-segura",
        )
        with session_factory() as db:
            other_household_id = db.scalar(select(User.household_id).where(User.username == "admin-outra-fluxos"))
        from app.models import Account

        with session_factory() as db:
            foreign_account = Account(household_id=other_household_id, name="Conta de outra família", account_type="checking")
            db.add(foreign_account)
            db.commit()
            foreign_account_id = foreign_account.id

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Receita em conta de outra família",
                "amount": 100,
                "movement_type": "income",
                "account_id": foreign_account_id,
            },
        )
        assert response.status_code == 404


def test_deleting_a_manual_transaction_is_isolated_by_household():
    """A user logged into a different household must never be able to
    delete another household's manual entry, even by guessing its id."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        created = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Salário",
                "amount": 100,
                "movement_type": "income",
                "account_id": itau,
            },
        )
        assert created.status_code == 201, created.text
        transaction_id = created.json()["id"]

        _create_household_admin(
            session_factory,
            household_name="Outra Família",
            username="admin-outra-delete",
            password="outra-senha-segura",
        )
        login = client.post(
            "/api/auth/login",
            json={"username": "admin-outra-delete", "password": "outra-senha-segura"},
        )
        assert login.status_code == 200, login.text

        response = client.delete(f"/api/transactions/{transaction_id}")
        assert response.status_code == 404

        with session_factory() as db:
            assert db.get(Transaction, transaction_id) is not None


def test_manual_transaction_requires_authentication():
    client, _ = _client()
    with client:
        # No session cookie yet: the manual-entry command must reject before
        # ever looking at accounts/categories.
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Sem sessão",
                "amount": 100,
                "movement_type": "income",
                "account_id": "does-not-matter",
            },
        )
        assert response.status_code == 401


def test_installment_preview_exposes_canonical_schedule_before_confirmation():
    """`docs/GO_LIVE_MANUAL_UX_PLAN.md`: the `Saídas` installment prévia must
    show the per-installment value, current/total, the future months and the
    effect on the canonical projection *before* the purchase is confirmed --
    derived from the same backend path `_future_installments` uses, not a
    JS formula. Nothing is persisted by the preview call."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")

        preview = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": itau,
                "amount": "100.00",
                "booked_at": "2026-08-05",
                "installment_current": 2,
                "installment_total": 4,
            },
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["monthly_payment"] == 100.0
        assert body["installment_current"] == 2
        assert body["installment_total"] == 4
        # Two installments remain after the 2nd of 4: September and October.
        assert body["schedule"] == [
            {"month": "2026-09", "amount": 100.0},
            {"month": "2026-10", "amount": 100.0},
        ]
        effect_by_month = {row["month"]: row for row in body["canonical_projection_effect"]}
        assert effect_by_month["2026-09"]["installments_before"] == 0.0
        assert effect_by_month["2026-09"]["installments_after"] == 100.0
        assert effect_by_month["2026-10"]["installments_before"] == 0.0
        assert effect_by_month["2026-10"]["installments_after"] == 100.0

        # No transaction was created by the preview.
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id))
            assert _future_installments(db, household_id) == {}


def test_installment_preview_reflects_already_persisted_commitments():
    """The "before" side of the preview's canonical effect must reflect
    commitments already persisted (via `_future_installments`), so the
    prévia for a *second* parcelled purchase shows its real cumulative
    effect on the same future month, not just its own isolated schedule."""
    client, _ = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        category_id = _non_system_category_id(client)

        first = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-05",
                "description": "Geladeira parcelada",
                "amount": 100,
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-08",
            },
        )
        assert first.status_code == 201, first.text

        # A second, unrelated purchase (in a different, non-card account)
        # whose remaining schedule also lands on 2026-09.
        preview = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": itau,
                "amount": "50.00",
                "booked_at": "2026-08-20",
                "installment_current": 1,
                "installment_total": 2,
            },
        )
        assert preview.status_code == 200, preview.text
        effect_by_month = {row["month"]: row for row in preview.json()["canonical_projection_effect"]}
        # Already-persisted purchase alone contributes 100.00 in 2026-09.
        assert effect_by_month["2026-09"]["installments_before"] == 100.0
        # Adding the previewed purchase's own 50.00 for that month.
        assert effect_by_month["2026-09"]["installments_after"] == 150.0


def test_installment_preview_anchors_on_confirmed_competence_not_booked_at():
    """INV-017 regression (engineering review on PR 47, head `7c8a25e`): when
    the confirmed invoice competence diverges from `booked_at`'s month, the
    prévia must anchor the remaining schedule on the competence, exactly as
    `_future_installments` does once the purchase is persisted -- otherwise
    the prévia and the canonical projection would disagree about which
    months are affected for the very same fact.

    Same reproduction the review used: booked 2026-08-29, competence
    confirmed 2026-09, installment 1/3 -- remaining installments must be
    2026-10 and 2026-11, never 2026-09 (the observed installment's own
    competence) or 2026-08 (`booked_at`'s month)."""
    client, _ = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")

        preview = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": nubank_card,
                "amount": "90.00",
                "booked_at": "2026-08-29",
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["schedule"] == [
            {"month": "2026-10", "amount": 90.0},
            {"month": "2026-11", "amount": 90.0},
        ]


def test_installment_preview_matches_persisted_projection_for_divergent_competence():
    """End-to-end proof that preview and `_future_installments` never drift:
    the prévia for a not-yet-confirmed purchase must show the exact same
    schedule that `_future_installments` reports once that same purchase is
    actually persisted with a competence diverging from `booked_at`."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        category_id = _non_system_category_id(client)

        params = {
            "account_id": nubank_card,
            "amount": "90.00",
            "booked_at": "2026-08-29",
            "installment_current": 1,
            "installment_total": 3,
            "competence": "2026-09",
        }
        preview = client.get("/api/transactions/manual/installment-preview", params=params)
        assert preview.status_code == 200, preview.text
        previewed_schedule = {row["month"]: row["amount"] for row in preview.json()["schedule"]}

        created = client.post(
            "/api/transactions",
            json={
                "booked_at": params["booked_at"],
                "description": "Compra perto do fechamento da fatura",
                "amount": float(params["amount"]),
                "movement_type": "expense",
                "account_id": nubank_card,
                "category_id": category_id,
                "installment_current": params["installment_current"],
                "installment_total": params["installment_total"],
                "competence": params["competence"],
            },
        )
        assert created.status_code == 201, created.text

        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-fluxos"))
            persisted_schedule = {
                month: float(value) for month, value in _future_installments(db, household_id).items()
            }
        assert previewed_schedule == persisted_schedule == {"2026-10": 90.0, "2026-11": 90.0}


def test_installment_preview_rejects_current_greater_than_total():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        response = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": itau,
                "amount": "100.00",
                "booked_at": "2026-08-05",
                "installment_current": 5,
                "installment_total": 3,
            },
        )
        assert response.status_code == 422


def test_installment_preview_requires_authentication():
    client, _ = _client()
    with client:
        response = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": "irrelevant-without-auth",
                "amount": "100.00",
                "booked_at": "2026-08-05",
                "installment_current": 1,
                "installment_total": 3,
            },
        )
        assert response.status_code == 401


def test_installment_preview_requires_known_account():
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": "does-not-exist",
                "amount": "100.00",
                "booked_at": "2026-08-05",
                "installment_current": 1,
                "installment_total": 3,
            },
        )
        assert response.status_code == 404


def test_installment_preview_rejects_competence_diverging_from_booked_at_for_non_card_account():
    """Engineering review on PR 47 (head `5671ac0`, gap 1): without knowing
    the account, the prévia could show a schedule anchored on a competence
    the POST would reject outright. A checking account's prévia must apply
    the exact same `_resolve_expense_competence` policy `create_manual_transaction`
    does -- reject a competence diverging from `booked_at`'s month instead of
    silently anchoring the schedule on it."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente", account_type="checking")
        response = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": itau,
                "amount": "90.00",
                "booked_at": "2026-08-12",
                "installment_current": 1,
                "installment_total": 3,
                "competence": "2026-09",
            },
        )
        assert response.status_code == 422
        assert "competência" in response.json()["detail"].lower()


def test_installment_preview_requires_competence_for_card_account():
    """The prévia must fail closed exactly like `create_manual_transaction`
    when a card purchase omits the explicitly confirmed invoice competence
    (INV-017) -- never fabricate one from `booked_at` to show a schedule."""
    client, _ = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        response = client.get(
            "/api/transactions/manual/installment-preview",
            params={
                "account_id": nubank_card,
                "amount": "90.00",
                "booked_at": "2026-08-29",
                "installment_current": 1,
                "installment_total": 3,
            },
        )
        assert response.status_code == 422
        assert "competência" in response.json()["detail"].lower()
