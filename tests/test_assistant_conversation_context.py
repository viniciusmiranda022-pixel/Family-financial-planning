"""Tests for WA-05 (`docs/WORK_ORDER_WA_05.md`, issue #77) --
`app.services.assistant_conversation_context`, the short, structured,
server-side conversational continuity store.

Mirrors the structure of `tests/test_assistant_orchestrator.py` (seeded
in-memory SQLite session, no network). Covers the Work Order's mandatory
tests for this module in isolation: TTL expiry, size/history bounds,
household/user/conversation isolation (including colliding conversation
ids across users/households), and defense-in-depth sanitization of a
resolved step against the read-only tool allowlist.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-assistant-wa05-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "assistant-wa05-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base  # noqa: E402
from app.models import AssistantConversationState, Household, User  # noqa: E402
from app.services.assistant_conversation_context import (  # noqa: E402
    CONVERSATION_STATE_TTL_MINUTES,
    MAX_CONTEXT_STEPS,
    MAX_TURNS,
    ConversationContext,
    cleanup_expired_conversation_states,
    load_context,
    record_turn,
)


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def _seed_user(session_factory, *, username: str) -> tuple[str, str]:
    with session_factory() as db:
        household = Household(name=f"Família {username}")
        db.add(household)
        db.flush()
        household_id = household.id
        user = User(
            household_id=household_id,
            name="Usuário",
            username=username,
            password_hash="x",
            is_admin=True,
        )
        db.add(user)
        db.flush()
        user_id = user.id
        db.commit()
    return household_id, user_id


# ---------------------------------------------------------------------------
# 1. Basic round trip -- empty context, then turns/steps accumulate.
# ---------------------------------------------------------------------------


def test_load_context_returns_empty_when_no_row_exists() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="empty-ctx")
    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context == ConversationContext.empty()


def test_record_turn_then_load_context_round_trips_plain_text_turns() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="round-trip")
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="quanto gastei de combustível esse mês?",
            assistant_message="Você gastou R$ 300,00 em Combustível em setembro/2026.",
        )
        db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context.turns == (
        {"role": "user", "content": "quanto gastei de combustível esse mês?"},
        {"role": "assistant", "content": "Você gastou R$ 300,00 em Combustível em setembro/2026."},
    )
    assert context.last_steps == ()


def test_record_turn_persists_an_eligible_read_only_step_hint() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="step-hint")
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="quanto gastei de combustível esse mês?",
            assistant_message="R$ 300,00.",
            resolved_step=(
                "financial_aggregate",
                {"dimension": "categoria", "category_hint": "combustível", "period_text": "este mês"},
            ),
        )
        db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context.last_steps == (
        {
            "tool": "financial_aggregate",
            "arguments": {
                "dimension": "categoria",
                "category_hint": "combustível",
                "period_text": "este mês",
            },
        },
    )


# ---------------------------------------------------------------------------
# 2. Prohibition -- typed-action write-flow tools never become a step hint.
# ---------------------------------------------------------------------------


def test_record_turn_never_persists_typed_action_write_tools_as_step_hints() -> None:
    """Work Order prohibition 'No authority/household/write-confirmation
    derived from conversation memory' -- `draft_typed_action`/
    `confirm_typed_action`/`cancel_typed_action`/`undo_typed_action` must
    never become a remembered `last_steps` hint, even though the plain-text
    turn (the assistant's own confirmation prompt) is still recorded."""

    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="no-write-hint")
    for tool in ("draft_typed_action", "confirm_typed_action", "cancel_typed_action", "undo_typed_action"):
        with session_factory() as db:
            record_turn(
                db,
                household_id=household_id,
                user_id=user_id,
                conversation_id=f"web:{tool}",
                channel="web",
                user_message="gastei 50 no mercado",
                assistant_message="Entendi uma saída. Confirma?",
                resolved_step=(tool, {"reference_text": "a proposta"}),
            )
            db.commit()
        with session_factory() as db:
            context = load_context(
                db, household_id=household_id, user_id=user_id, conversation_id=f"web:{tool}"
            )
        assert context.last_steps == (), f"{tool} must never be persisted as a context step hint"
        assert context.turns  # plain text continuity is still preserved


def test_sanitize_step_strips_argument_keys_outside_that_tools_own_allowlist() -> None:
    """Defense in depth: even if a caller mistakenly forwarded an
    out-of-schema argument key for an otherwise-eligible tool, it never
    reaches persisted context -- the same 'never trust a single layer'
    idiom `app.services.assistant_tools.run_tool` already applies."""

    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="strip-args")
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="x",
            assistant_message="y",
            resolved_step=(
                "financial_aggregate",
                {"dimension": "categoria", "sql": "DROP TABLE transactions", "account_id": "123"},
            ),
        )
        db.commit()
    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context.last_steps == ({"tool": "financial_aggregate", "arguments": {"dimension": "categoria"}},)


def test_sanitize_step_rejects_a_tool_name_outside_the_allowlist_entirely() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="unknown-tool")
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="x",
            assistant_message="y",
            resolved_step=("run_sql", {"query": "DROP TABLE transactions"}),
        )
        db.commit()
    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context.last_steps == ()


# ---------------------------------------------------------------------------
# 3. Bounds -- turns/last_steps are capped, oldest entries drop off first.
# ---------------------------------------------------------------------------


def test_turns_are_bounded_to_max_turns_dropping_the_oldest_first() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="bounded-turns")
    for i in range(MAX_TURNS):  # each call appends 2 turns (user+assistant)
        with session_factory() as db:
            record_turn(
                db,
                household_id=household_id,
                user_id=user_id,
                conversation_id="web:1",
                channel="web",
                user_message=f"pergunta {i}",
                assistant_message=f"resposta {i}",
            )
            db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert len(context.turns) == MAX_TURNS
    # Only the most recent turns survive -- the oldest ones fell off first.
    assert context.turns[0]["content"] == f"pergunta {MAX_TURNS // 2}"
    assert context.turns[-1]["content"] == f"resposta {MAX_TURNS - 1}"


def test_last_steps_are_bounded_to_max_context_steps() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="bounded-steps")
    for i in range(MAX_CONTEXT_STEPS + 2):
        with session_factory() as db:
            record_turn(
                db,
                household_id=household_id,
                user_id=user_id,
                conversation_id="web:1",
                channel="web",
                user_message=f"pergunta {i}",
                assistant_message=f"resposta {i}",
                resolved_step=("financial_aggregate", {"period_text": f"periodo-{i}"}),
            )
            db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert len(context.last_steps) == MAX_CONTEXT_STEPS
    assert context.last_steps[-1]["arguments"]["period_text"] == f"periodo-{MAX_CONTEXT_STEPS + 1}"


# ---------------------------------------------------------------------------
# 4. TTL -- sliding expiry, never resurrected once stale.
# ---------------------------------------------------------------------------


def test_expired_context_is_treated_as_absent_never_resurrected() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="ttl-expiry")
    long_ago = datetime.now(UTC) - timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES + 5)
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="pergunta antiga",
            assistant_message="resposta antiga",
            resolved_step=("financial_aggregate", {"period_text": "agosto"}),
            now=long_ago,
        )
        db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
    assert context == ConversationContext.empty()


def test_record_turn_after_expiry_starts_a_fresh_conversation_not_a_merge() -> None:
    """A stale conversation's old turns/steps must never leak into what is,
    for continuity purposes, a brand-new conversation under the same key."""

    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="ttl-fresh-start")
    long_ago = datetime.now(UTC) - timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES + 5)
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="pergunta antiga",
            assistant_message="resposta antiga",
            resolved_step=("financial_aggregate", {"period_text": "agosto"}),
            now=long_ago,
        )
        db.commit()

    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="pergunta nova",
            assistant_message="resposta nova",
        )
        db.commit()

    with session_factory() as db:
        context = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:1")
        rows = db.scalars(select(AssistantConversationState)).all()
    assert context.turns == (
        {"role": "user", "content": "pergunta nova"},
        {"role": "assistant", "content": "resposta nova"},
    )
    assert context.last_steps == ()
    assert len(rows) == 1, "the stale row must be replaced, never accumulated alongside a new one"


def test_record_turn_refreshes_the_sliding_ttl_on_every_call() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="ttl-refresh")
    t0 = datetime.now(UTC)
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="a",
            assistant_message="b",
            now=t0,
        )
        db.commit()

    # A little less than the TTL later -- still alive -- refresh again.
    t1 = t0 + timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES - 5)
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:1",
            channel="web",
            user_message="c",
            assistant_message="d",
            now=t1,
        )
        db.commit()

    # Now past the *original* TTL window, but well inside the refreshed one.
    t2 = t0 + timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES + 2)
    with session_factory() as db:
        context = load_context(
            db, household_id=household_id, user_id=user_id, conversation_id="web:1", now=t2
        )
    assert context.turns[-2:] == (
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    )


def test_cleanup_expired_conversation_states_deletes_only_stale_rows() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="cleanup")
    now = datetime.now(UTC)
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:stale",
            channel="web",
            user_message="a",
            assistant_message="b",
            now=now - timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES + 5),
        )
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:fresh",
            channel="web",
            user_message="a",
            assistant_message="b",
            now=now,
        )
        db.commit()

    with session_factory() as db:
        deleted = cleanup_expired_conversation_states(db, now=now)
        db.commit()
        remaining = db.scalars(select(AssistantConversationState.conversation_id)).all()
    assert deleted == 1
    assert remaining == ["web:fresh"]


# ---------------------------------------------------------------------------
# 5. Isolation -- household/user/conversation, including colliding ids.
# ---------------------------------------------------------------------------


def test_two_users_in_the_same_household_get_separate_state_for_the_same_conversation_id() -> None:
    """Work Order acceptance criterion: 'Vinicius/Kelly may have separate
    conversation state inside the same household' -- proven here with the
    exact same `conversation_id` string reused by both, colliding on
    everything except `user_id`."""

    session_factory = _session_factory()
    with session_factory() as db:
        household = Household(name="Família Compartilhada")
        db.add(household)
        db.flush()
        household_id = household.id
        vinicius = User(
            household_id=household_id, name="Vinicius", username="vinicius", password_hash="x", is_admin=True
        )
        kelly = User(
            household_id=household_id, name="Kelly", username="kelly", password_hash="x", is_admin=True
        )
        db.add_all([vinicius, kelly])
        db.flush()
        vinicius_id, kelly_id = vinicius.id, kelly.id
        db.commit()

    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=vinicius_id,
            conversation_id="whatsapp:shared-key",
            channel="whatsapp",
            user_message="pergunta do Vinicius",
            assistant_message="resposta para Vinicius",
        )
        record_turn(
            db,
            household_id=household_id,
            user_id=kelly_id,
            conversation_id="whatsapp:shared-key",
            channel="whatsapp",
            user_message="pergunta da Kelly",
            assistant_message="resposta para Kelly",
        )
        db.commit()

    with session_factory() as db:
        vinicius_context = load_context(
            db, household_id=household_id, user_id=vinicius_id, conversation_id="whatsapp:shared-key"
        )
        kelly_context = load_context(
            db, household_id=household_id, user_id=kelly_id, conversation_id="whatsapp:shared-key"
        )
    assert vinicius_context.turns[0]["content"] == "pergunta do Vinicius"
    assert kelly_context.turns[0]["content"] == "pergunta da Kelly"


def test_two_households_never_cross_reference_the_same_conversation_id() -> None:
    session_factory = _session_factory()
    household_a, user_a = _seed_user(session_factory, username="household-a")
    household_b, user_b = _seed_user(session_factory, username="household-b")

    with session_factory() as db:
        record_turn(
            db,
            household_id=household_a,
            user_id=user_a,
            conversation_id="web:same-user-id-collision",
            channel="web",
            user_message="segredo da família A",
            assistant_message="resposta A",
        )
        db.commit()

    with session_factory() as db:
        context_b = load_context(
            db, household_id=household_b, user_id=user_b, conversation_id="web:same-user-id-collision"
        )
    assert context_b == ConversationContext.empty()


def test_two_conversation_ids_for_the_same_user_never_share_state() -> None:
    session_factory = _session_factory()
    household_id, user_id = _seed_user(session_factory, username="two-conversations")
    with session_factory() as db:
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:tab-1",
            channel="web",
            user_message="pergunta na aba 1",
            assistant_message="resposta 1",
        )
        record_turn(
            db,
            household_id=household_id,
            user_id=user_id,
            conversation_id="web:tab-2",
            channel="web",
            user_message="pergunta na aba 2",
            assistant_message="resposta 2",
        )
        db.commit()

    with session_factory() as db:
        tab1 = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:tab-1")
        tab2 = load_context(db, household_id=household_id, user_id=user_id, conversation_id="web:tab-2")
    assert tab1.turns[0]["content"] == "pergunta na aba 1"
    assert tab2.turns[0]["content"] == "pergunta na aba 2"
