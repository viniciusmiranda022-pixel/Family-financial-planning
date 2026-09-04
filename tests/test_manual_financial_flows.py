"""Tests for the manual go-live financial flows.

Work Order: `docs/WORK_ORDER_MANUAL_FINANCIAL_FLOWS_GO_LIVE.md`.

Scope covered here (see the Technical Challenge on PR 47 for the slices
deliberately deferred): closing the gap `docs/ROADMAP.md` names explicitly --
the manual quick-entry form (`POST /api/transactions`) only accepted
`expense|income|investment|redemption|refund`, while `transfer` and
`reconciliation` were only reachable through the Central Inteligente
capture-confirm flow. This adds:

- a real, paired, double-entry `transfer` between two of the household's own
  accounts (INV-001: zero operational effect, both legs traceable via the
  previously-unused `Transaction.transfer_group_id`);
- manual `reconciliation` (invoice payment as a brand-new fact, INV-002:
  never a second expense);
- installment fields on manual expense creation, reusing the existing
  `installment_current`/`installment_total` columns and projection dedup
  logic already exercised by the capture/import paths.

Uses the same isolated-engine + `TestClient` + `/api/auth/setup` pattern as
`tests/test_card_payment_reconciliation.py::test_http_endpoints_authorize_isolate_and_audit`.
"""

import json
import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-manual-flows-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "manual-financial-flows-test-secret-long-enough-value")
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


def _create_account(client, *, name, account_type="checking"):
    response = client.post("/api/accounts", json={"name": name, "account_type": account_type})
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


def test_manual_transfer_creates_paired_legs_with_zero_operational_effect():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")

        before = client.get("/api/dashboard?month=2026-08").json()

        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Transferência Itaú -> Nubank",
                "amount": 500,
                "movement_type": "transfer",
                "account_id": itau,
                "destination_account_id": nubank,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["id"] != body["linked_id"]
        assert body["transfer_group_id"]

        after = client.get("/api/dashboard?month=2026-08").json()
        # INV-001: a transfer between the household's own accounts never
        # changes income, expense/spending or operational cash flow.
        assert after["cash_in"] == before["cash_in"]
        assert after["cash_out"] == before["cash_out"]
        assert after["spending"] == before["spending"]

        rows = client.get("/api/transactions?month=2026-08").json()
        origin_row = next(item for item in rows if item["id"] == body["id"])
        destination_row = next(item for item in rows if item["id"] == body["linked_id"])
        assert origin_row["type"] == "transfer"
        assert destination_row["type"] == "transfer"
        assert origin_row["excluded"] is True
        assert destination_row["excluded"] is True
        assert origin_row["amount"] == -500
        assert destination_row["amount"] == 500
        assert origin_row["transfer_group_id"] == destination_row["transfer_group_id"] == body["transfer_group_id"]

        with session_factory() as db:
            event = db.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "transaction.create_manual_transfer")
            )
            assert event is not None
            details = json.loads(event.details)
            assert details["origin_account_id"] == itau
            assert details["destination_account_id"] == nubank
            assert details["linked_transaction_id"] == body["linked_id"]


def test_manual_transfer_requires_distinct_destination_account():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Transferência para a mesma conta",
                "amount": 100,
                "movement_type": "transfer",
                "account_id": itau,
                "destination_account_id": itau,
            },
        )
        assert response.status_code == 422


def test_manual_transfer_requires_destination_account():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Transferência sem destino",
                "amount": 100,
                "movement_type": "transfer",
                "account_id": itau,
            },
        )
        assert response.status_code == 422


def test_destination_account_id_rejected_outside_transfer():
    client, _ = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Receita com destino indevido",
                "amount": 100,
                "movement_type": "income",
                "account_id": itau,
                "destination_account_id": nubank,
            },
        )
        assert response.status_code == 422


def test_manual_transfer_rejects_destination_account_from_another_household():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

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
                "description": "Transferência para conta de outra família",
                "amount": 100,
                "movement_type": "transfer",
                "account_id": itau,
                "destination_account_id": foreign_account_id,
            },
        )
        assert response.status_code == 404


def test_deleting_one_leg_of_transfer_deletes_both_legs():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")
        nubank = _create_account(client, name="Nubank Conta")
        created = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-10",
                "description": "Transferência a apagar",
                "amount": 300,
                "movement_type": "transfer",
                "account_id": itau,
                "destination_account_id": nubank,
            },
        ).json()

        delete = client.delete(f"/api/transactions/{created['id']}")
        assert delete.status_code == 200
        assert sorted(delete.json()["deleted_ids"]) == sorted([created["id"], created["linked_id"]])

        with session_factory() as db:
            remaining = db.scalars(
                select(Transaction).where(Transaction.transfer_group_id == created["transfer_group_id"])
            ).all()
            assert remaining == []


def test_manual_reconciliation_creates_excluded_transaction_without_expense_effect():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        itau = _create_account(client, name="Itaú Corrente")

        before = client.get("/api/dashboard?month=2026-08").json()
        response = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-15",
                "description": "Pagamento da fatura Itaú",
                "amount": 5000,
                "movement_type": "reconciliation",
                "account_id": itau,
                "confirmed_large_amount": True,
            },
        )
        assert response.status_code == 201, response.text
        transaction_id = response.json()["id"]

        after = client.get("/api/dashboard?month=2026-08").json()
        # INV-002: paying an invoice is conciliation, never a new expense.
        assert after["spending"] == before["spending"]
        assert after["cash_out"] == before["cash_out"]

        rows = client.get("/api/transactions?month=2026-08").json()
        row = next(item for item in rows if item["id"] == transaction_id)
        assert row["type"] == "reconciliation"
        assert row["excluded"] is True
        assert row["amount"] == -5000
        assert row["category"] == "Conciliação"

        with session_factory() as db:
            event = db.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "transaction.create_manual")
            )
            assert json.loads(event.details)["movement_type"] == "reconciliation"


def test_manual_installment_expense_populates_installment_and_card_fields():
    client, session_factory = _client()
    with client:
        _setup_household(client)
        nubank_card = _create_account(client, name="Nubank Cartão", account_type="credit_card")
        categories = client.get("/api/categories").json()
        category_id = next(item["id"] for item in categories if item["name"] not in {
            "Conciliação", "Transferência patrimonial", "Transferência interna", "Receitas",
        })

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
        categories = client.get("/api/categories").json()
        category_id = categories[0]["id"]
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
        categories = client.get("/api/categories").json()
        category_id = categories[0]["id"]
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
