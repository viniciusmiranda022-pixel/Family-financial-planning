"""Tests for October Go-Live Slice 4 (P0 #87) -- Assistente Financeiro
operacional: typed actions, disambiguation, audit/undo, idempotency.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md`.

Section 1 unit-tests `app.services.assistant_actions.build_typed_action_proposal`
directly against a seeded in-memory SQLite session (no Codex, no HTTP) --
a `StructuredInterpretation` is constructed by hand, exactly the shape
`app.services.assistant_interpreter` would hand back after a real Codex
call, so these tests prove the deterministic resolution/disambiguation
logic in complete isolation from the sidecar.

Section 2 is an HTTP-level functional suite for `POST /assistant/execute`
and `POST /assistant/actions/{id}/undo`, mirroring
`tests/test_obligation_lifecycle_slice3.py`'s `_client()`/`_setup_household()`
pattern -- covering the Work Order's "Testes obrigatórios" list.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-assistant-slice4-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "assistant-slice4-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AssistantActionEvent,
    AuditEvent,
    Category,
    Household,
    Obligation,
    Transaction,
)
from app.services.assistant_actions import (  # noqa: E402
    AssistantActionError,
    build_typed_action_proposal,
    execute_typed_action,
    parse_amount_text,
    parse_date_text,
)
from app.services.assistant_interpreter import StructuredInterpretation  # noqa: E402
from app.services.card_invoice_lifecycle import get_or_sync_invoice  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Deterministic parsing + proposal-building unit tests (no Codex, no HTTP).
# ---------------------------------------------------------------------------


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def _household(session_factory, name="Família Assistente Slice4") -> str:
    with session_factory() as db:
        household = Household(name=name)
        db.add(household)
        db.commit()
        return household.id


def _account(
    session_factory, *, household_id, name, account_type="checking", last_four=None
) -> str:
    with session_factory() as db:
        account = Account(
            household_id=household_id, name=name, account_type=account_type, last_four=last_four
        )
        db.add(account)
        db.commit()
        return account.id


def _interpretation(intent: str, **fields: str) -> StructuredInterpretation:
    return StructuredInterpretation(
        available=True,
        reason=None,
        intent=intent,
        extracted_fields=fields,
        missing_fields=(),
        clarifying_question=None,
        confidence=0.9,
    )


def test_parse_amount_text_handles_brazilian_and_plain_formats() -> None:
    assert parse_amount_text("300") == Decimal("300")
    assert parse_amount_text("R$ 300,50") == Decimal("300.50")
    assert parse_amount_text("8.500,00") == Decimal("8500.00")
    assert parse_amount_text("") is None
    assert parse_amount_text("abc") is None
    assert parse_amount_text("-50") is None


@given(cents=st.integers(min_value=1, max_value=999_999_999))
@settings(max_examples=100)
def test_parse_amount_text_property_round_trips_every_brazilian_formatted_value(cents: int) -> None:
    """Property, not a hand-picked example: every positive value, formatted
    the way a Brazilian user would actually type it (".", thousands; ",",
    decimal), parses back to the exact same `Decimal` -- never a guess,
    never silently truncated."""

    amount = Decimal(cents) / 100
    text = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    assert parse_amount_text(text) == amount


def test_parse_date_text_only_understands_its_closed_vocabulary() -> None:
    today = date(2026, 9, 14)
    assert parse_date_text("hoje", today=today) == today
    assert parse_date_text("ontem", today=today) == date(2026, 9, 13)
    assert parse_date_text("2026-09-01", today=today) == date(2026, 9, 1)
    assert parse_date_text("qualquer coisa", today=today) is None


def test_unavailable_interpretation_never_produces_an_executable_proposal() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    interpretation = StructuredInterpretation(available=False, reason="provider_unreachable")
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert proposal.typed_action is None


def test_create_expense_on_card_needs_no_funding_source_question() -> None:
    """A card purchase never asks Conta Corrente vs Privilège -- the card
    itself is the origin (rebaseline §4.4 only applies to cash/checking
    outflows)."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense", amount_text="300", description="Combustível", account_hint="Nubank"
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    assert proposal.typed_action == "create_expense"
    assert proposal.payload["amount"] == "300"
    assert proposal.payload["movement_type"] == "expense"


def test_create_expense_cash_without_funding_source_asks_and_then_resolves() -> None:
    """Work Order "Testes obrigatórios": "despesa PIX sem origem -> pergunta
    e zero escrita" followed by "resposta de desambiguação completa -> uma
    única execução"."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Conta Corrente", account_type="checking")

    ambiguous = _interpretation(
        "create_expense", amount_text="300", description="Combustível", account_hint="Conta Corrente"
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=ambiguous)
    assert proposal.can_execute is False
    assert "Privilège" in proposal.clarifying_question

    resolved = _interpretation(
        "create_expense",
        amount_text="300",
        description="Combustível",
        account_hint="Conta Corrente",
        funding_source_hint="account",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=resolved)
    assert proposal.can_execute is True
    assert proposal.payload["funding_source"] == "account"


def test_internal_transfer_proposal_never_asks_about_category_or_funding() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Privilège DI", account_type="investment")
    _account(session_factory, household_id=household_id, name="Conta Corrente", account_type="checking")
    interpretation = _interpretation(
        "create_internal_transfer",
        amount_text="500",
        account_hint="Privilège DI",
        target_hint="Conta Corrente",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    assert proposal.typed_action == "create_internal_transfer"
    assert "category_name" not in proposal.payload
    assert "funding_source" not in proposal.payload


def test_ambiguous_account_hint_returns_candidates_never_a_silent_choice() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank Vinicius", account_type="credit_card")
    _account(session_factory, household_id=household_id, name="Nubank Kelly", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense", amount_text="100", description="Compra", account_hint="Nubank"
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert len(proposal.candidates) == 2


def test_refund_ambiguous_original_purchase_asks_never_picks_silently() -> None:
    """Work Order: "ambiguidade de alvo de estorno -> pergunta, nunca
    escolha silenciosa"."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    with session_factory() as db:
        category = Category(household_id=household_id, name="Compras")
        db.add(category)
        db.flush()
        category_id = category.id
        for index in range(2):
            db.add(
                Transaction(
                    household_id=household_id,
                    account_id=account_id,
                    category_id=category_id,
                    booked_at=date(2026, 9, 1 + index),
                    occurred_at=date(2026, 9, 1 + index),
                    competence="2026-09",
                    description="Loja Tal",
                    normalized_description="LOJA TAL",
                    amount=Decimal("-100.00"),
                    transaction_type="expense",
                    fingerprint=f"refundtest{index}{uuid.uuid4().hex}".ljust(64, "0")[:64],
                    source_priority=50,
                    confidence=Decimal("1"),
                    reviewed=True,
                )
            )
        refund = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=date(2026, 9, 5),
            occurred_at=date(2026, 9, 5),
            competence="2026-09",
            description="Estorno Loja Tal",
            normalized_description="ESTORNO LOJA TAL",
            amount=Decimal("100.00"),
            transaction_type="refund",
            fingerprint=f"refundtestcredit{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
        )
        db.add(refund)
        db.commit()

    interpretation = _interpretation(
        "register_refund", amount_text="100", target_hint="Loja Tal"
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert proposal.clarifying_question is not None


# ---------------------------------------------------------------------------
# 2. HTTP functional suite for execute/undo.
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


def _setup_household(client, *, username="admin-assistant-slice4"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Assistente HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def test_execute_create_expense_writes_exactly_once_and_is_fully_audited() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17})
        assert account.status_code == 201, account.text
        account_id = account.json()["id"]

        trace_id = str(uuid.uuid4())
        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_expense",
                "payload": {
                    "booked_at": "2026-09-01",
                    "description": "Combustível",
                    "amount": "300.00",
                    "account_id": account_id,
                    "category_name": "Transporte",
                },
                "original_message": "Gastei 300 de combustível no Nubank",
                "structured_interpretation": {"intent": "create_expense", "confidence": 0.95},
                "trace_id": trace_id,
            },
        )
        assert execute.status_code == 201, execute.text
        body = execute.json()
        assert body["idempotent_replay"] is False
        transaction_id = body["response"]["id"]
        action_id = body["action"]["id"]

        with session_factory() as db:
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 1
            assert expenses[0].id == transaction_id

            action_event = db.get(AssistantActionEvent, action_id)
            audit_event = db.get(AuditEvent, action_event.audit_event_id)
            assert action_event.original_message == "Gastei 300 de combustível no Nubank"
            assert action_event.typed_action == "create_expense"
            assert action_event.target_entity_ids == [transaction_id]
            assert action_event.undoable is True
            assert audit_event.trace_id == trace_id
            assert audit_event.event_type == "assistant.execute"

        # Retry with the same trace_id: idempotent replay, no second write.
        retry = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_expense",
                "payload": {
                    "booked_at": "2026-09-01",
                    "description": "Combustível",
                    "amount": "300.00",
                    "account_id": account_id,
                    "category_name": "Transporte",
                },
                "original_message": "Gastei 300 de combustível no Nubank",
                "trace_id": trace_id,
            },
        )
        assert retry.status_code == 201, retry.text
        assert retry.json()["idempotent_replay"] is True

        with session_factory() as db:
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 1


def test_execute_internal_transfer_creates_zero_income_or_expense() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        privilege = client.post(
            "/api/accounts", json={"name": "Privilège DI", "account_type": "investment"}
        )
        checking = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        assert privilege.status_code == 201
        assert checking.status_code == 201

        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_internal_transfer",
                "payload": {
                    "booked_at": "2026-09-01",
                    "description": "Resgate para pagamento",
                    "amount": "500.00",
                    "from_account_id": privilege.json()["id"],
                    "to_account_id": checking.json()["id"],
                },
                "original_message": "Resgatei 500 do Privilège para a conta corrente",
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 201, execute.text

        with session_factory() as db:
            movements = list(db.scalars(select(Transaction)))
            assert len(movements) == 2
            assert all(item.transaction_type == "transfer" for item in movements)
            assert not any(item.transaction_type in ("expense", "income") for item in movements)


def test_execute_pay_obligation_via_assistant_matches_manual_contract_no_double_count() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        assert account.status_code == 201
        account_id = account.json()["id"]

        created = client.post(
            "/api/obligations",
            json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"},
        )
        assert created.status_code == 201, created.text
        obligation_id = created.json()["id"]

        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "pay_obligation",
                "payload": {
                    "account_id": account_id,
                    "paid_at": "2026-08-09",
                    "funding_source": "account",
                },
                "path_params": {"obligation_id": obligation_id},
                "original_message": "Paguei a parcela da chácara",
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 201, execute.text
        action_id = execute.json()["action"]["id"]

        with session_factory() as db:
            obligation = db.get(Obligation, obligation_id)
            assert obligation.status == "paid"
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 1

        undo = client.post(
            f"/api/assistant/actions/{action_id}/undo",
            json={"reason": "Pagamento lançado por engano pelo Assistente"},
        )
        assert undo.status_code == 200, undo.text

        with session_factory() as db:
            obligation = db.get(Obligation, obligation_id)
            assert obligation.status == "pending"
            action_event = db.get(AssistantActionEvent, action_id)
            assert action_event.undone_at is not None
            assert action_event.undone_by is not None


def test_undo_pay_card_invoice_is_refused_as_non_reversible_with_explicit_reason() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        card = client.post(
            "/api/accounts",
            json={"name": "Cartão", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
        )
        checking = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        assert card.status_code == 201
        assert checking.status_code == 201
        card_id, checking_id = card.json()["id"], checking.json()["id"]

        with session_factory() as db:
            household_id = db.scalar(select(Household)).id
            category = Category(household_id=household_id, name="Compras")
            db.add(category)
            db.flush()
            db.add(
                Transaction(
                    household_id=household_id,
                    account_id=card_id,
                    category_id=category.id,
                    booked_at=date(2026, 9, 3),
                    occurred_at=date(2026, 9, 3),
                    competence="2026-09",
                    description="Compra",
                    normalized_description="COMPRA",
                    amount=Decimal("-1000.00"),
                    transaction_type="expense",
                    fingerprint=f"assistantinvoice{uuid.uuid4().hex}".ljust(64, "0")[:64],
                    source_priority=50,
                    confidence=Decimal("1"),
                    reviewed=True,
                )
            )
            db.commit()

        with session_factory() as db:
            account = db.get(Account, card_id)
            invoice = get_or_sync_invoice(
                db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 11)
            )
            db.commit()
            invoice_id = invoice.id

        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "pay_card_invoice",
                "payload": {
                    "paying_account_id": checking_id,
                    "amount": "1000.00",
                    "booked_at": "2026-09-15",
                    "description": "Pagamento fatura",
                    "confirmed": True,
                },
                "path_params": {"invoice_id": invoice_id},
                "original_message": "Paguei a fatura do cartão",
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 201, execute.text
        action_id = execute.json()["action"]["id"]
        assert execute.json()["action"]["undoable"] is False

        undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Quero desfazer"}
        )
        assert undo.status_code == 409
        assert "reversão" in undo.json()["detail"] or "revers" in undo.json()["detail"]


def test_execute_with_missing_required_field_never_writes() -> None:
    """Work Order invariant: "Nenhuma ação ambígua é executada
    automaticamente" -- an incomplete payload is rejected before any row is
    created, not silently completed with a guess."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_expense",
                "payload": {"booked_at": "2026-09-01", "description": "Combustível", "amount": "300.00"},
                "original_message": "Gastei 300 de combustível",
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 422
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_assistant_actions_are_isolated_between_households() -> None:
    client_a, _ = _client()
    with client_a:
        _setup_household(client_a, username="assistant-household-a")
        account = client_a.post("/api/accounts", json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17})
        execute = client_a.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_expense",
                "payload": {
                    "booked_at": "2026-09-01",
                    "description": "Combustível",
                    "amount": "300.00",
                    "account_id": account.json()["id"],
                    "category_name": "Transporte",
                },
                "original_message": "Gastei 300 de combustível",
                "trace_id": str(uuid.uuid4()),
            },
        )
        action_id = execute.json()["action"]["id"]

    client_b, _ = _client()
    with client_b:
        _setup_household(client_b, username="assistant-household-b")
        undo = client_b.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Tentativa de outro household"}
        )
        assert undo.status_code == 404
        assert client_b.get("/api/assistant/actions").json() == []


def test_execute_rejects_unknown_typed_action() -> None:
    """`AssistantExecuteRequest.typed_action` is a closed, pattern-validated
    vocabulary -- FastAPI/Pydantic itself rejects anything outside
    `app.services.assistant_actions.TYPED_ACTIONS` before this ever reaches
    `execute_typed_action`."""

    client, _ = _client()
    with client:
        _setup_household(client)
        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "delete_everything",
                "payload": {},
                "original_message": "tente algo indevido",
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 422


def test_execute_typed_action_function_rejects_unknown_action_directly() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        try:
            execute_typed_action(
                db,
                user=None,
                typed_action="not_a_real_action",
                payload={},
                original_message="x",
                structured_interpretation=None,
                disambiguation_qa=None,
                trace_id=str(uuid.uuid4()),
            )
            raised = False
        except AssistantActionError:
            raised = True
        assert raised


def test_undo_already_undone_action_is_refused() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17})
        execute = client.post(
            "/api/assistant/execute",
            json={
                "typed_action": "create_expense",
                "payload": {
                    "booked_at": "2026-09-01",
                    "description": "Combustível",
                    "amount": "300.00",
                    "account_id": account.json()["id"],
                    "category_name": "Transporte",
                },
                "original_message": "Gastei 300 de combustível",
                "trace_id": str(uuid.uuid4()),
            },
        )
        action_id = execute.json()["action"]["id"]

        first_undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Lançamento incorreto"}
        )
        assert first_undo.status_code == 200

        second_undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Tentativa de novo"}
        )
        assert second_undo.status_code == 409
