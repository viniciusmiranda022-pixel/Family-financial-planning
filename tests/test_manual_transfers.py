"""Tests for the manual go-live financial flows -- slice 2: Transferências
+ Aplicação/Resgate.

Work Order: `docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md`.

Scope covered here: the new structured transfer command (`POST
/api/transfers`, `app/services/transfers.py::create_internal_transfer`)
that creates two atomically-linked `transaction_type = "transfer"` legs
between two accounts of the same household (INV-001) -- `transfer` and the
`transfer_group_id` index were deliberately deferred from slice 1 (see
`tests/test_manual_financial_flows.py`'s module docstring) to this slice.
Also covers the required regression checks for the already-existing
`investment`/`redemption` movement types on `POST /api/transactions`
(INV-003/INV-004), which this slice reuses unchanged rather than
duplicating into a second command.

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_manual_financial_flows.py`.
"""

import json
import os
import uuid
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-manual-transfers-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "manual-transfers-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

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


def _setup_household(client, *, household_name="Família Transferências", username="admin-transf", password="senha-local-segura"):
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


def _deactivate_account(session_factory, account_id: str) -> None:
    """There is no API to deactivate an account (out of scope for this
    slice); flip the row directly to set up the inactive-account fixture."""
    from app.models import Account

    with session_factory() as db:
        account = db.get(Account, account_id)
        account.active = False
        db.commit()


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


def _transfer_payload(*, booked_at, description, amount, from_account_id, to_account_id, **extra):
    payload = {
        "booked_at": booked_at,
        "description": description,
        "amount": amount,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
    }
    payload.update(extra)
    return payload


def test_transfer_creates_two_legs_with_zero_operational_effect() -> None:
    """INV-001: both legs are created atomically, excluded from operational
    totals, and the dashboard/cash flow is unaffected -- the transfer only
    moves money between two accounts already inside the household."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta", account_type="checking")

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência para reserva",
                amount=500,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["transfer_group_id"]
        assert body["from_transaction_id"] != body["to_transaction_id"]

        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["cash_in"] == before["cash_in"]
        assert after["spending"] == before["spending"]

        with session_factory() as db:
            debit = db.get(Transaction, body["from_transaction_id"])
            credit = db.get(Transaction, body["to_transaction_id"])
            assert debit.transaction_type == "transfer"
            assert credit.transaction_type == "transfer"
            assert debit.excluded is True
            assert credit.excluded is True
            assert debit.amount == Decimal("-500.00")
            assert credit.amount == Decimal("500.00")
            assert debit.account_id == itau
            assert credit.account_id == nubank
            assert debit.transfer_group_id == credit.transfer_group_id == body["transfer_group_id"]
            assert debit.linked_transaction_id == credit.id
            assert credit.linked_transaction_id == debit.id
            assert debit.category.name == "Transferência interna"
            assert credit.category.name == "Transferência interna"


def test_transfer_rejects_same_account_as_origin_and_destination() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência inválida",
                amount=100,
                from_account_id=itau,
                to_account_id=itau,
            ),
        )
        assert response.status_code == 422


def test_transfer_rejects_destination_account_from_another_household() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

        _create_household_admin(
            session_factory,
            household_name="Outra Família",
            username="admin-outra-transf",
            password="outra-senha-segura",
        )
        with session_factory() as db:
            other_household_id = db.scalar(
                select(User.household_id).where(User.username == "admin-outra-transf")
            )
        from app.models import Account

        with session_factory() as db:
            foreign_account = Account(
                household_id=other_household_id, name="Conta de outra família", account_type="checking"
            )
            db.add(foreign_account)
            db.commit()
            foreign_account_id = foreign_account.id

        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência para outra família",
                amount=100,
                from_account_id=itau,
                to_account_id=foreign_account_id,
            ),
        )
        assert response.status_code == 404

        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_transfer_rejects_nonexistent_destination_account() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência para conta inexistente",
                amount=100,
                from_account_id=itau,
                to_account_id="does-not-exist",
            ),
        )
        assert response.status_code == 404
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_transfer_rejects_inactive_destination_account() -> None:
    """Atomicity/precondition check: the origin leg must never be created
    when the destination fails validation -- no partial leg left behind."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        inactive = _create_account(client, name="Conta encerrada")
        _deactivate_account(session_factory, inactive)
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência para conta inativa",
                amount=100,
                from_account_id=itau,
                to_account_id=inactive,
            ),
        )
        assert response.status_code == 404
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_transfer_audit_trail_links_both_legs() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência auditável",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 201, response.text
        body = response.json()

        with session_factory() as db:
            event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "transfer.create"))
            assert event is not None
            details = json.loads(event.details)
            assert details["transfer_group_id"] == body["transfer_group_id"]
            assert details["debit_transaction_id"] == body["from_transaction_id"]
            assert details["credit_transaction_id"] == body["to_transaction_id"]
            assert event.trace_id == body["transfer_group_id"]


def test_transfer_appears_in_ledger_with_shared_group_id() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência rastreável",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 201, response.text
        body = response.json()

        rows = client.get("/api/transactions?month=2026-08").json()
        legs = [row for row in rows if row["id"] in {body["from_transaction_id"], body["to_transaction_id"]}]
        assert len(legs) == 2
        assert {row["transfer_group_id"] for row in legs} == {body["transfer_group_id"]}
        assert {row["type"] for row in legs} == {"transfer"}
        assert sorted(row["amount"] for row in legs) == [-250.0, 250.0]


def test_deleting_one_leg_of_a_transfer_deletes_both_atomically() -> None:
    """No orphan leg: removing either transaction id of the pair removes
    the whole group in one commit, never leaving a single unlinked leg."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência a apagar",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 201, response.text
        body = response.json()

        delete_response = client.delete(f"/api/transactions/{body['from_transaction_id']}")
        assert delete_response.status_code == 200, delete_response.text
        deleted_ids = set(delete_response.json()["deleted_transaction_ids"])
        assert deleted_ids == {body["from_transaction_id"], body["to_transaction_id"]}

        with session_factory() as db:
            assert db.get(Transaction, body["from_transaction_id"]) is None
            assert db.get(Transaction, body["to_transaction_id"]) is None
            event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "transfer.delete"))
            assert event is not None
            assert json.loads(event.details)["transfer_group_id"] == body["transfer_group_id"]


def test_updating_excluded_on_a_transfer_leg_is_rejected() -> None:
    """A single leg's `excluded` cannot be flipped without its twin -- that
    would break INV-001's zero net operational effect."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência protegida",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        body = response.json()

        patched = client.patch(
            f"/api/transactions/{body['from_transaction_id']}", json={"excluded": False}
        )
        assert patched.status_code == 422


def test_updating_category_on_a_transfer_leg_is_rejected() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência protegida",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        body = response.json()
        with session_factory() as db:
            household_id = db.scalar(select(User.household_id).where(User.username == "admin-transf"))
            from app.models import Category

            other_category = Category(household_id=household_id, name="Outra categoria")
            db.add(other_category)
            db.commit()
            other_category_id = other_category.id

        patched = client.patch(
            f"/api/transactions/{body['from_transaction_id']}", json={"category_id": other_category_id}
        )
        assert patched.status_code == 422


def test_updating_owner_label_on_a_transfer_leg_still_allowed() -> None:
    """The guard is narrow: administrative fields with no operational-effect
    meaning remain editable per leg, exactly as for any other transaction."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência com titular ajustável",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        body = response.json()

        patched = client.patch(
            f"/api/transactions/{body['from_transaction_id']}", json={"owner_label": "Kelly"}
        )
        assert patched.status_code == 200, patched.text
        with session_factory() as db:
            debit = db.get(Transaction, body["from_transaction_id"])
            assert debit.owner_label == "Kelly"


def test_transfer_requires_authentication() -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Sem sessão",
                amount=100,
                from_account_id="does-not-matter",
                to_account_id="also-does-not-matter",
            ),
        )
        assert response.status_code == 401


def test_transfer_large_amount_requires_confirmation() -> None:
    """Reuses the same large-entry confirmation guard every other manual
    command already applies (`_large_entry_threshold`) -- no new policy."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")

        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência de valor elevado",
                amount=50000,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 409
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None

        confirmed = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência de valor elevado",
                amount=50000,
                from_account_id=itau,
                to_account_id=nubank,
                confirmed_large_amount=True,
            ),
        )
        assert confirmed.status_code == 201, confirmed.text


def test_application_movement_type_has_zero_operating_effect() -> None:
    """INV-003 regression for the already-existing `investment` movement
    type on `POST /transactions`, reused unchanged by this slice: applying
    to the investment pool never shows up as spending or budget cap usage."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Aplicação no Privilège DI",
                "amount": 1000,
                "movement_type": "investment",
                "account_id": itau,
            },
        )
        assert response.status_code == 201, response.text
        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["spending"] == before["spending"]
        assert after["cash_in"] == before["cash_in"]

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == response.json()["id"])
        assert row["type"] == "transfer"
        assert row["category"] == "Transferência patrimonial"
        assert row["amount"] == -1000.0


def test_redemption_movement_type_has_zero_operating_income_effect() -> None:
    """INV-004 regression for the already-existing `redemption` movement
    type on `POST /transactions`: redeeming from the investment pool never
    shows up as real income."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Resgate do Privilège DI",
                "amount": 400,
                "movement_type": "redemption",
                "account_id": itau,
            },
        )
        assert response.status_code == 201, response.text
        after = client.get("/api/dashboard?month=2026-08").json()
        assert after["cash_in"] == before["cash_in"]
        assert after["spending"] == before["spending"]

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == response.json()["id"])
        assert row["type"] == "transfer"
        assert row["category"] == "Transferência patrimonial"
        assert row["amount"] == 400.0


def test_structured_transfer_endpoint_parity_with_backend_canonical_command() -> None:
    """`docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md`: "paridade
    entre tela estruturada e comando backend canônico". There is exactly one
    code path for a transfer (`create_internal_transfer`); the structured
    screen's request body maps straight onto it with no client-side
    formula. Proof: the two persisted legs' amounts are always exactly
    the request's `amount`, split into a debit and credit of that same
    magnitude -- the frontend never computes either leg's value itself, it
    only ever displays what this endpoint returns."""
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")

        requested_amount = 733.19
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência com paridade",
                amount=requested_amount,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        assert response.status_code == 201, response.text
        body = response.json()

        rows = {row["id"]: row for row in client.get("/api/transactions?month=2026-08").json()}
        debit_row = rows[body["from_transaction_id"]]
        credit_row = rows[body["to_transaction_id"]]
        assert debit_row["amount"] == -requested_amount
        assert credit_row["amount"] == requested_amount
        # Same category/type/exclusion semantics the ledger and dashboard
        # already apply uniformly to every canonical "transfer" -- no
        # separate rendering rule for the structured screen's own rows.
        assert debit_row["category"] == credit_row["category"] == "Transferência interna"
        assert debit_row["type"] == credit_row["type"] == "transfer"


def test_transfer_household_isolation_on_delete() -> None:
    """A user in a different household must never be able to delete
    another household's transfer leg, even by guessing its id."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transfers",
            json=_transfer_payload(
                booked_at="2026-08-10",
                description="Transferência isolada",
                amount=250,
                from_account_id=itau,
                to_account_id=nubank,
            ),
        )
        body = response.json()

        _create_household_admin(
            session_factory,
            household_name="Outra Família",
            username="admin-outra-transf-delete",
            password="outra-senha-segura",
        )
        login = client.post(
            "/api/auth/login",
            json={"username": "admin-outra-transf-delete", "password": "outra-senha-segura"},
        )
        assert login.status_code == 200, login.text

        response = client.delete(f"/api/transactions/{body['from_transaction_id']}")
        assert response.status_code == 404

        with session_factory() as db:
            assert db.get(Transaction, body["from_transaction_id"]) is not None
            assert db.get(Transaction, body["to_transaction_id"]) is not None
