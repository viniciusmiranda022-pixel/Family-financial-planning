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
from datetime import UTC, date, datetime, timedelta
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
    AssistantActionProposal,
    AuditEvent,
    Category,
    Household,
    Obligation,
    Transaction,
    User,
)
from app.services.assistant_actions import (  # noqa: E402
    AssistantActionError,
    DuplicateResolution,
    build_typed_action_proposal,
    execute_typed_action,
    parse_amount_text,
    parse_date_text,
    persist_action_proposal,
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


def test_parse_amount_text_disambiguates_a_dot_with_no_comma_by_shape() -> None:
    """A lone "." with no "," is never assumed to be a decimal point by
    default -- "1.500" read that way silently becomes R$ 1,50 (Codex review
    finding on PR #108, `assistant_tools.py:444`). Disambiguate by shape
    instead of guessing: a 3-digit final group with valid thousands
    grouping throughout can only be Brazilian grouping; a 1-2 digit final
    group can only be a decimal remainder; anything else is genuinely
    ambiguous and rejected rather than guessed."""

    # Unambiguous Brazilian thousands grouping -- no comma at all.
    assert parse_amount_text("1.500") == Decimal("1500")
    assert parse_amount_text("8.500") == Decimal("8500")
    assert parse_amount_text("R$ 1.500") == Decimal("1500")
    assert parse_amount_text("12.500") == Decimal("12500")
    assert parse_amount_text("100.000") == Decimal("100000")
    assert parse_amount_text("1.234.567") == Decimal("1234567")
    # Unambiguous decimal remainder -- a final group too short to be a
    # thousands group.
    assert parse_amount_text("1.5") == Decimal("1.5")
    assert parse_amount_text("1.50") == Decimal("1.50")
    # Genuinely ambiguous shapes are rejected, never guessed.
    assert parse_amount_text("1.2345") is None
    assert parse_amount_text("1234.567") is None


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


# UX-01 (docs/WORK_ORDER_UX_01_MAIL_TEST_CHAT_WRITE.md, issue #114): a
# parcelada purchase -- "fiz uma compra de 2459 parcelada no cartão nubank"
# -- must ask for the installment count when it cannot be resolved, and
# only then propose, with `amount_text` treated as the total contracted
# price (per-installment payment derived via the same amortized formula
# `simulate_purchase`/scenario-comparison already use).
def test_create_expense_installments_text_present_but_unresolvable_asks_for_count() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="parcelada",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert "parcelas" in proposal.clarifying_question.lower()
    assert proposal.missing_fields == ("installments",)


# Engineering review of PR #115 (MERGE BLOCKED): an installment purchase
# whose count is known but whose interest was never mentioned must ask,
# never silently book rate 0 -- silence is not the same fact as an explicit
# "sem juros".
def test_create_expense_installments_without_interest_mention_asks_about_rate() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert "juros" in proposal.clarifying_question.lower()
    assert proposal.missing_fields == ("monthly_interest_rate",)


def test_create_expense_installments_explicit_zero_interest_text_resolves() -> None:
    """An explicit "sem juros" is a stated fact, not a filled-in absence --
    it must resolve exactly like a numeric "0" would."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
        monthly_interest_rate_text="sem juros",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    assert proposal.payload["amount"] == "245.90"
    assert proposal.payload["installment_current"] == 1
    assert proposal.payload["installment_total"] == 10


def test_create_expense_installments_resolves_to_equal_interest_free_split() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
        monthly_interest_rate_text="0",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    assert proposal.payload["amount"] == "245.90"
    assert proposal.payload["installment_current"] == 1
    assert proposal.payload["installment_total"] == 10


def test_create_expense_installments_invalid_rate_asks_for_correction() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
        monthly_interest_rate_text="um monte",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is False
    assert "juros" in proposal.clarifying_question.lower()
    assert proposal.missing_fields == ("monthly_interest_rate",)

    interpretation_too_high = _interpretation(
        "create_expense",
        amount_text="2459",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
        monthly_interest_rate_text="99",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(
            db, household_id=household_id, interpretation=interpretation_too_high
        )
    assert proposal.can_execute is False
    assert proposal.missing_fields == ("monthly_interest_rate",)


def test_create_expense_installments_with_interest_uses_amortized_payment() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Nubank", account_type="credit_card")
    interpretation = _interpretation(
        "create_expense",
        amount_text="1000",
        description="Compra",
        account_hint="Nubank",
        installments_text="10",
        monthly_interest_rate_text="2",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    from app.services.finance import amortized_installment_payment

    expected_payment, _ = amortized_installment_payment(Decimal("1000"), 10, Decimal("0.02"))
    assert proposal.payload["amount"] == str(expected_payment)
    assert proposal.payload["installment_total"] == 10


def test_create_income_ignores_installments_text_never_parcels_income() -> None:
    """Parcelamento só se aplica a despesas (`ManualTransactionRequest.
    validate_movement_fields`) -- `installments_text` on a `create_income`
    interpretation must never reach the payload, never block the proposal."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    _account(session_factory, household_id=household_id, name="Conta Corrente", account_type="checking")
    interpretation = _interpretation(
        "create_income",
        amount_text="1000",
        description="Salário",
        account_hint="Conta Corrente",
        installments_text="10",
    )
    with session_factory() as db:
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
    assert proposal.can_execute is True
    assert "installment_total" not in proposal.payload
    assert proposal.payload["amount"] == "1000"


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


def _propose_and_persist(
    session_factory,
    *,
    household_id: str,
    message: str,
    interpretation: StructuredInterpretation,
    trace_id: str | None = None,
    duplicate_resolution: DuplicateResolution | None = None,
):
    """Mirrors exactly what `POST /assistant/interpret` does server-side --
    `build_typed_action_proposal` then, only when it can execute,
    `persist_action_proposal` -- without needing a configured Codex sidecar
    (unavailable in this test process, exactly like Section 1's
    `_interpretation()` already bypasses it by constructing a
    `StructuredInterpretation` by hand). This lets `POST /assistant/execute`
    be exercised against its real, unmodified HTTP contract (engineering
    review of PR #92, blocker 1): the proposal id this returns is the only
    thing a test ever hands to that endpoint, exactly like a real client
    would receive it from a real `POST /assistant/interpret` call.

    Returns `(proposal_id_or_None, proposal, trace_id)` -- `proposal_id` is
    `None` when the proposal still needs disambiguation (nothing to
    execute, and nothing was persisted)."""

    trace_id = trace_id or str(uuid.uuid4())
    with session_factory() as db:
        user_id = db.scalar(select(User.id))
        proposal = build_typed_action_proposal(
            db,
            household_id=household_id,
            interpretation=interpretation,
            duplicate_resolution=duplicate_resolution,
        )
        if not proposal.can_execute:
            return None, proposal, trace_id
        proposal_id = persist_action_proposal(
            db,
            household_id=household_id,
            user_id=user_id,
            trace_id=trace_id,
            original_message=message,
            structured_interpretation=interpretation.to_dict(),
            proposal=proposal,
        )
    return proposal_id, proposal, trace_id


def _household_id(session_factory) -> str:
    with session_factory() as db:
        return db.scalar(select(Household.id))


def test_execute_create_expense_writes_exactly_once_and_is_fully_audited() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17})
        assert account.status_code == 201, account.text

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_expense", amount_text="300", description="Combustível", account_hint="Nubank",
            category_hint="Transporte",
        )
        proposal_id, proposal, trace_id = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Gastei 300 de combustível no Nubank",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
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
            # A create has no prior state to capture (engineering review of
            # PR #92, blocker 2: "para create, before_state=None é aceitável").
            assert action_event.before_state is None
            assert audit_event.trace_id == trace_id
            assert audit_event.event_type == "assistant.execute"

            proposal_row = db.get(AssistantActionProposal, proposal_id)
            assert proposal_row.consumed_at is not None
            assert proposal_row.consumed_action_event_id == action_id

        # Retry with the same, already-consumed proposal_id: idempotent
        # replay, no second write -- the proposal itself is the idempotency
        # key now (blocker 1), not a client-supplied trace_id.
        retry = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert retry.status_code == 201, retry.text
        assert retry.json()["idempotent_replay"] is True

        with session_factory() as db:
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 1


def test_execute_create_expense_installment_persists_current_and_total() -> None:
    """UX-01: the executed `Transaction` must carry `installment_current`/
    `installment_total` exactly like the manual-entry form's own
    `ManualTransactionRequest` -- the canonical "parcelas futuras"
    projection (`app.api._installment_remaining_schedule`) reads these same
    two columns, so a chat-drafted installment purchase must feed it
    identically to a manually-entered one."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post(
            "/api/accounts",
            json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
        )
        assert account.status_code == 201, account.text

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_expense",
            amount_text="2459",
            description="Televisão",
            account_hint="Nubank",
            category_hint="Casa",
            installments_text="10",
            monthly_interest_rate_text="sem juros",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="fiz uma compra de 2459 parcelada no cartão nubank",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert execute.status_code == 201, execute.text
        transaction_id = execute.json()["response"]["id"]

        with session_factory() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.installment_current == 1
            assert transaction.installment_total == 10
            assert transaction.amount == Decimal("-245.90")


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

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_internal_transfer",
            amount_text="500",
            account_hint="Privilège DI",
            target_hint="Conta Corrente",
            description="Resgate para pagamento",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Resgatei 500 do Privilège para a conta corrente",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
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

        created = client.post(
            "/api/obligations",
            json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"},
        )
        assert created.status_code == 201, created.text
        obligation_id = created.json()["id"]

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "pay_obligation",
            target_hint="Parcela chácara",
            funding_source_hint="account",
            account_hint="Conta Corrente",
            date_text="2026-08-09",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Paguei a parcela da chácara",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True
        assert proposal.path_params == {"obligation_id": obligation_id}

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert execute.status_code == 201, execute.text
        action_id = execute.json()["action"]["id"]

        with session_factory() as db:
            obligation = db.get(Obligation, obligation_id)
            assert obligation.status == "paid"
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 1

            # Blocker 2: a mutation on an *existing* entity (unlike a
            # create) must capture the canonical before/after the domain
            # endpoint itself already computed -- copied verbatim, not
            # recomputed.
            action_event = db.get(AssistantActionEvent, action_id)
            assert action_event.before_state == {
                "status": "pending",
                "paid_at": None,
                "paid_transaction_id": None,
            }
            assert action_event.after_state["status"] == "paid"
            assert action_event.after_state["paid_transaction_id"] == expenses[0].id

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
            # The undo itself is a real domain write (`_unpay_obligation_impl`)
            # -- deleted the transaction the payment created -- so it must
            # not have vanished from `transactions` either.
            assert db.scalar(select(Transaction).where(Transaction.id == expenses[0].id)) is None


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
        card_id = card.json()["id"]

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
            get_or_sync_invoice(
                db, household_id=household_id, account=account, competence="2026-09", as_of=date(2026, 9, 11)
            )
            db.commit()

        interpretation = _interpretation(
            "pay_card_invoice",
            account_hint="Cartão",
            target_hint="Conta Corrente",
            amount_text="1000",
            date_text="2026-09-15",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Paguei a fatura do cartão",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert execute.status_code == 201, execute.text
        action_id = execute.json()["action"]["id"]
        assert execute.json()["action"]["undoable"] is False
        # Blocker 2, mutation-on-existing-entity: the invoice's own
        # before/after must be captured too, not just left `None`.
        assert execute.json()["action"]["before_state"]["status"] == "closed"
        assert execute.json()["action"]["after_state"]["status"] == "paid"

        undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Quero desfazer"}
        )
        assert undo.status_code == 409
        assert "reversão" in undo.json()["detail"] or "revers" in undo.json()["detail"]


def test_incomplete_interpretation_never_persists_a_proposal_and_writes_nothing() -> None:
    """Work Order invariant: "Nenhuma ação ambígua é executada
    automaticamente" -- an incomplete message never becomes an executable,
    persisted proposal, so there is never a `proposal_id` an HTTP call
    could even reference to write anything."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_expense", amount_text="300", description="Combustível"
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory, household_id=household_id, message="Gastei 300 de combustível", interpretation=interpretation
        )
        assert proposal.can_execute is False
        assert proposal_id is None

        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None
            assert db.scalar(select(AssistantActionProposal)) is None


def test_persist_action_proposal_refuses_a_non_executable_proposal() -> None:
    """Defensive guard on `persist_action_proposal` itself -- even if a
    caller mistakenly tried to persist a still-ambiguous proposal, it must
    refuse rather than silently store something `execute_typed_action`
    could later dispatch without ever having been resolved."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        interpretation = _interpretation("create_expense", amount_text="300")
        proposal = build_typed_action_proposal(db, household_id=household_id, interpretation=interpretation)
        assert proposal.can_execute is False
        try:
            persist_action_proposal(
                db,
                household_id=household_id,
                user_id="does-not-matter",
                trace_id=str(uuid.uuid4()),
                original_message="Gastei 300",
                structured_interpretation=interpretation.to_dict(),
                proposal=proposal,
            )
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_execute_rejects_unknown_proposal_id() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        execute = client.post("/api/assistant/execute", json={"proposal_id": str(uuid.uuid4())})
        assert execute.status_code == 404


def test_execute_rejects_the_old_client_supplied_typed_action_contract() -> None:
    """Regression test for the exact vulnerability engineering review of PR
    #92 (blocker 1) flagged: a client can no longer skip `POST
    /assistant/interpret` and hand-craft `typed_action`/`payload` directly
    -- `AssistantExecuteRequest` no longer has those fields at all, so
    Pydantic itself rejects the old-shaped request before any code in
    `execute_typed_action` ever runs."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post(
            "/api/accounts",
            json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
        )
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
                "original_message": "mensagem forjada sem passar por /assistant/interpret",
                "structured_interpretation": {"intent": "create_expense", "confidence": 0.99},
                "trace_id": str(uuid.uuid4()),
            },
        )
        assert execute.status_code == 422
        with session_factory() as db:
            assert db.scalar(select(Transaction)) is None


def test_execute_rejects_a_proposal_from_another_household() -> None:
    """`execute_typed_action` scopes its `AssistantActionProposal` lookup by
    `household_id`, exactly like every other Slice 1-4 query -- a proposal
    id leaked or guessed from another household's session must not be
    executable here."""

    client_a, session_factory_a = _client()
    with client_a:
        _setup_household(client_a, username="assistant-proposal-household-a")
        assert (
            client_a.post(
                "/api/accounts",
                json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
            ).status_code
            == 201
        )
        household_a_id = _household_id(session_factory_a)
        interpretation = _interpretation(
            "create_expense", amount_text="300", description="Combustível", account_hint="Nubank",
            category_hint="Transporte",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory_a,
            household_id=household_a_id,
            message="Gastei 300 de combustível",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

    client_b, _ = _client()
    with client_b:
        _setup_household(client_b, username="assistant-proposal-household-b")
        execute = client_b.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        assert execute.status_code == 404


def test_execute_typed_action_function_rejects_unknown_action_directly() -> None:
    """Defense in depth: even a hand-corrupted `AssistantActionProposal` row
    (a value `build_typed_action_proposal` itself could never produce) is
    rejected by `execute_typed_action`'s own `TYPED_ACTIONS` check, not
    trusted just because it made it into the table."""

    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        household = db.get(Household, household_id)
        user = User(household_id=household.id, name="Corrupt", username="corrupt-proposal-user", password_hash="x")
        db.add(user)
        db.flush()
        row = AssistantActionProposal(
            household_id=household_id,
            user_id=user.id,
            trace_id=str(uuid.uuid4()),
            original_message="x",
            typed_action="not_a_real_action",
            payload={},
            path_params={},
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        db.add(row)
        db.commit()
        proposal_id = row.id

        try:
            execute_typed_action(db, user=user, proposal_id=proposal_id)
            raised = False
        except AssistantActionError:
            raised = True
        assert raised


def test_execute_rejects_an_expired_proposal() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        household = db.get(Household, household_id)
        user = User(household_id=household.id, name="Expired", username="expired-proposal-user", password_hash="x")
        db.add(user)
        db.flush()
        row = AssistantActionProposal(
            household_id=household_id,
            user_id=user.id,
            trace_id=str(uuid.uuid4()),
            original_message="x",
            typed_action="create_expense",
            payload={},
            path_params={},
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.add(row)
        db.commit()
        proposal_id = row.id

        try:
            execute_typed_action(db, user=user, proposal_id=proposal_id)
            raised = False
        except AssistantActionError as exc:
            raised = "expirou" in str(exc)
        assert raised


def test_execute_typed_action_is_atomic_on_a_failure_after_the_domain_write() -> None:
    """Engineering review of PR #92, blocker 5: if anything fails between
    the dispatched `_impl`'s own domain write and this module's own
    `AssistantActionEvent`/proposal-consumption write, the whole thing must
    roll back together -- never a financial fact persisted without its
    Assistant audit trail. Forces that exact failure window by making the
    *first* `db.flush()` after dispatch (the one building the
    Assistant-specific `AuditEvent`) raise, then proves in a *fresh*
    session that the obligation's payment was fully undone, not just left
    uncommitted in the original session's identity map."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
        assert account.status_code == 201

        created = client.post(
            "/api/obligations",
            json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"},
        )
        assert created.status_code == 201, created.text
        obligation_id = created.json()["id"]

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "pay_obligation",
            target_hint="Parcela chácara",
            funding_source_hint="account",
            account_hint="Conta Corrente",
            date_text="2026-08-09",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Paguei a parcela da chácara",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True

        with session_factory() as db:
            user = db.scalar(select(User))
            real_flush = db.flush
            calls = {"n": 0}

            def _boom_flush(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    # The domain write (`_pay_obligation_impl`, `commit=False`)
                    # has already happened in this same session by the time
                    # `execute_typed_action` reaches its own first flush.
                    raise RuntimeError("simulated failure between domain write and assistant audit")
                return real_flush(*args, **kwargs)

            db.flush = _boom_flush
            try:
                execute_typed_action(db, user=user, proposal_id=proposal_id)
                raised = False
            except RuntimeError:
                raised = True
            assert raised

        # A completely fresh session/connection -- not the one the failure
        # happened in -- must show the obligation still pending and no
        # payment transaction, proving a real rollback, not merely an
        # uncommitted change in one session's identity map.
        with session_factory() as db:
            obligation = db.get(Obligation, obligation_id)
            assert obligation.status == "pending"
            assert obligation.paid_transaction_id is None
            assert db.scalar(select(Transaction)) is None
            proposal_row = db.get(AssistantActionProposal, proposal_id)
            assert proposal_row.consumed_at is None

        # Concurrency review requirement (engineering review of PR #92,
        # second round, point 4 of the required regression): a transaction
        # that fails before its commit must release the proposal for a
        # legitimate retry, not leave it permanently stuck -- the rollback
        # above must have undone the `SELECT ... FOR UPDATE` claim as well
        # as the domain write. Retrying with the *same* `proposal_id` here
        # must now succeed exactly once.
        with session_factory() as db:
            user = db.scalar(select(User))
            retry_result = execute_typed_action(db, user=user, proposal_id=proposal_id)
        assert retry_result["idempotent_replay"] is False

        with session_factory() as db:
            obligation = db.get(Obligation, obligation_id)
            assert obligation.status == "paid"
            assert obligation.paid_transaction_id is not None
            proposal_row = db.get(AssistantActionProposal, proposal_id)
            assert proposal_row.consumed_at is not None


def test_possible_duplicate_offers_three_human_choices_never_auto_resolves() -> None:
    """Work Order "deduplicação provável com as três escolhas humanas"
    (engineering review of PR #92, blocker 4): a probable duplicate must be
    presented with the existing lançamento and never silently merged,
    skipped or imported without an explicit human decision."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        account = client.post("/api/accounts", json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17})
        assert account.status_code == 201

        first = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-09-05",
                "description": "Posto Ipiranga",
                "amount": "300.00",
                "movement_type": "expense",
                "account_id": account.json()["id"],
                "category_name": "Combustível",
            },
        )
        assert first.status_code == 201, first.text
        existing_transaction_id = first.json()["id"]

        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_expense",
            amount_text="300",
            description="Posto Ipiranga",
            account_hint="Nubank",
            date_text="2026-09-05",
            category_hint="Combustível",
        )

        # Step 1: no decision yet -> warning, zero writes, three choices
        # implied by the question text.
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory, household_id=household_id, message="Gastei 300 no posto de novo", interpretation=interpretation
        )
        assert proposal_id is None
        assert proposal.can_execute is False
        assert proposal.candidate_kind == "possible_duplicate"
        assert proposal.candidates[0]["id"] == existing_transaction_id
        assert "pular" in proposal.clarifying_question.lower()
        assert "importar" in proposal.clarifying_question.lower()

        with session_factory() as db:
            assert (
                db.scalar(select(Transaction).where(Transaction.id != existing_transaction_id)) is None
            )

        # Step 2: "skip" -> still zero writes, no proposal persisted.
        skip_resolution = DuplicateResolution(decision="skip", transaction_id=existing_transaction_id)
        skip_proposal_id, skip_proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Gastei 300 no posto de novo",
            interpretation=interpretation,
            duplicate_resolution=skip_resolution,
        )
        assert skip_proposal_id is None
        assert skip_proposal.can_execute is False
        with session_factory() as db:
            assert (
                db.scalar(select(Transaction).where(Transaction.id != existing_transaction_id)) is None
            )

        # Step 3: "import_anyway" -> now resolves to a normal executable
        # proposal; the post-create pass (`register_transaction_duplicates`)
        # still flags it for human review, exactly like manual entry does.
        import_resolution = DuplicateResolution(decision="import_anyway", transaction_id=existing_transaction_id)
        import_proposal_id, import_proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Gastei 300 no posto de novo",
            interpretation=interpretation,
            duplicate_resolution=import_resolution,
        )
        assert import_proposal.can_execute is True

        execute = client.post("/api/assistant/execute", json={"proposal_id": import_proposal_id})
        assert execute.status_code == 201, execute.text

        with session_factory() as db:
            expenses = list(
                db.scalars(select(Transaction).where(Transaction.transaction_type == "expense"))
            )
            assert len(expenses) == 2
            new_transaction = next(item for item in expenses if item.id != existing_transaction_id)
            assert new_transaction.reviewed is False


def test_assistant_actions_are_isolated_between_households() -> None:
    client_a, session_factory_a = _client()
    with client_a:
        _setup_household(client_a, username="assistant-household-a")
        assert (
            client_a.post(
                "/api/accounts",
                json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
            ).status_code
            == 201
        )
        household_id = _household_id(session_factory_a)
        interpretation = _interpretation(
            "create_expense", amount_text="300", description="Combustível", account_hint="Nubank",
            category_hint="Transporte",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory_a,
            household_id=household_id,
            message="Gastei 300 de combustível",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True
        execute = client_a.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        action_id = execute.json()["action"]["id"]

    client_b, _ = _client()
    with client_b:
        _setup_household(client_b, username="assistant-household-b")
        undo = client_b.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Tentativa de outro household"}
        )
        assert undo.status_code == 404
        assert client_b.get("/api/assistant/actions").json() == []


def test_undo_already_undone_action_is_refused() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        assert (
            client.post(
                "/api/accounts",
                json={"name": "Nubank", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
            ).status_code
            == 201
        )
        household_id = _household_id(session_factory)
        interpretation = _interpretation(
            "create_expense", amount_text="300", description="Combustível", account_hint="Nubank",
            category_hint="Transporte",
        )
        proposal_id, proposal, _ = _propose_and_persist(
            session_factory,
            household_id=household_id,
            message="Gastei 300 de combustível",
            interpretation=interpretation,
        )
        assert proposal.can_execute is True
        execute = client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        action_id = execute.json()["action"]["id"]

        first_undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Lançamento incorreto"}
        )
        assert first_undo.status_code == 200

        second_undo = client.post(
            f"/api/assistant/actions/{action_id}/undo", json={"reason": "Tentativa de novo"}
        )
        assert second_undo.status_code == 409
