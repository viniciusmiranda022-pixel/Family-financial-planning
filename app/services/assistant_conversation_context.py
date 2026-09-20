"""WA-05 (`docs/WORK_ORDER_WA_05.md`, issue #77): short, structured,
server-side conversational continuity for the Tool Layer.

This module is the only reader/writer of `app.models.AssistantConversationState`
-- a single row per `(household_id, user_id, conversation_id)`, holding at
most `MAX_TURNS` bounded plain-text turns (the same shape/length discipline
`app.services.assistant_tool_sanitizer.build_plan_payload` already applies
to a client-supplied `history`) and at most `MAX_CONTEXT_STEPS` bounded,
already-allowlisted read-only tool calls (`tool` drawn from
`app.services.assistant_tool_catalog.ALLOWED_TOOLS`, restricted further to
`CONTEXT_ELIGIBLE_TOOLS`; `arguments` filtered through that tool's own
`TOOL_ARGUMENT_KEYS` -- the same independent re-validation idiom
`app.services.assistant_tools.run_tool` already applies, so a row this
module itself wrote can never smuggle anything past that allowlist even if
the row were somehow corrupted).

`CONTEXT_ELIGIBLE_TOOLS` deliberately excludes `draft_typed_action`/
`confirm_typed_action`/`cancel_typed_action`/`undo_typed_action` -- Work
Order prohibition "No authority/household/write-confirmation derived from
conversation memory". The write flow's own idempotency/authorization
(`app.services.assistant_actions`, `AssistantActionProposal`/
`AssistantActionEvent`) is always resolved fresh against the database, and
never reads this table.

TTL is a sliding window (`CONVERSATION_STATE_TTL_MINUTES`), refreshed on
every `record_turn` call. An expired row is never resurrected: `load_context`
treats it as if it did not exist, and the next `record_turn`/`load_context`
touch for that key deletes it -- deterministic, lazy cleanup, no scheduled
sweep or external cache (single-family deployment; see
`app.services.notification_scheduler`/`app.services.capture_worker` for the
same "no Redis" constraint already documented elsewhere in this codebase).
`cleanup_expired_conversation_states` is additionally exposed as a plain,
independently testable maintenance function for an operator/cron to invoke
if desired -- WA-05 does not wire it into any existing worker loop, to avoid
broadening this slice's scope into unrelated infrastructure.

Row-lock idiom (`SELECT ... FOR UPDATE` + `begin_nested()`/`IntegrityError`
insert-on-race) mirrors `app.services.whatsapp_gateway.check_rate_limit`
exactly, for the same reason: `FOR UPDATE` cannot lock a row that does not
exist yet, so a conversation's very first turn is its own race between two
concurrent messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services.assistant_tool_catalog import ALLOWED_TOOLS, TOOL_ARGUMENT_KEYS

CONVERSATION_STATE_TTL_MINUTES = 30
MAX_TURNS = 8
MAX_TURN_CONTENT_LENGTH = 1000
MAX_CONTEXT_STEPS = 3
MAX_ARGUMENT_VALUE_LENGTH = 300

# Read-only Tool Layer calls only -- see module docstring. Never the typed-
# action write flow (`draft_typed_action`/`confirm_typed_action`/
# `cancel_typed_action`/`undo_typed_action`), which must always resolve its
# own authority from the database, never from conversational memory.
CONTEXT_ELIGIBLE_TOOLS = frozenset(
    {
        "query_facts",
        "aggregate_spending",
        "compare_periods",
        "project_horizon",
        "financial_aggregate",
        "get_income",
        "get_expenses",
        "get_commitments",
        "get_installments",
        "search_transactions",
    }
) & ALLOWED_TOOLS


def _clean_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:max_length]


def _as_aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sanitize_step(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """Re-validate a resolved step against the exact same allowlist
    `app.services.assistant_tools.run_tool` already checked before this
    step ever executed -- defense in depth, not the primary gate."""

    if tool not in CONTEXT_ELIGIBLE_TOOLS:
        return None
    allowed_keys = TOOL_ARGUMENT_KEYS.get(tool, frozenset())
    raw_arguments = arguments if isinstance(arguments, dict) else {}
    clean_arguments = {
        key: _clean_text(value, max_length=MAX_ARGUMENT_VALUE_LENGTH)
        for key, value in raw_arguments.items()
        if key in allowed_keys and isinstance(value, str) and value.strip()
    }
    return {"tool": tool, "arguments": clean_arguments}


@dataclass(frozen=True, slots=True)
class ConversationContext:
    turns: tuple[dict[str, str], ...] = field(default_factory=tuple)
    last_steps: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def empty(cls) -> ConversationContext:
        return cls()


def _load_row(
    db: Session, *, household_id: str, user_id: str, conversation_id: str, for_update: bool
):
    from app.models import AssistantConversationState  # deferred: keep this module DB-schema-lazy

    query = select(AssistantConversationState).where(
        AssistantConversationState.household_id == household_id,
        AssistantConversationState.user_id == user_id,
        AssistantConversationState.conversation_id == conversation_id,
    )
    if for_update:
        query = query.with_for_update()
    return db.scalar(query)


def load_context(
    db: Session,
    *,
    household_id: str,
    user_id: str,
    conversation_id: str,
    now: datetime | None = None,
) -> ConversationContext:
    """Read-only lookup for building the next `/v1/plan` payload -- never
    locks the row (nothing here is mutated), and never resurrects a stale
    row: an expired conversation is treated exactly like no conversation at
    all, ambiguity-fail-safe by construction (Work Order "On material
    ambiguity or stale/insufficient context, return clarification rather
    than silently assuming")."""

    moment = now or datetime.now(UTC)
    row = _load_row(
        db, household_id=household_id, user_id=user_id, conversation_id=conversation_id, for_update=False
    )
    if row is None or _as_aware_utc(row.expires_at) < moment:
        return ConversationContext.empty()
    return ConversationContext(
        turns=tuple(dict(item) for item in (row.turns or [])),
        last_steps=tuple(dict(item) for item in (row.last_steps or [])),
    )


def record_turn(
    db: Session,
    *,
    household_id: str,
    user_id: str,
    conversation_id: str,
    channel: str,
    user_message: str,
    assistant_message: str | None,
    resolved_step: tuple[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> None:
    """Append one turn (and, when eligible, one structured step hint) to
    this conversation's bounded, TTL-refreshed state. Never raises for a
    concurrent first-touch race on the same key (see module docstring) --
    the caller's own request must never fail because conversational memory
    could not be updated; a lost race here still leaves this turn's plain
    answer already computed and returned to the user, only its *memory* of
    this turn is not persisted.
    """

    from app.models import AssistantConversationState  # deferred, same reason as above

    moment = now or datetime.now(UTC)
    row = _load_row(
        db, household_id=household_id, user_id=user_id, conversation_id=conversation_id, for_update=True
    )
    if row is not None and _as_aware_utc(row.expires_at) < moment:
        # Stale conversation: never carry its turns/steps forward into a
        # "new" conversation under the same key -- delete and fall through
        # to the same first-touch creation path a brand-new key would take.
        db.delete(row)
        db.flush()
        row = None

    if row is None:
        try:
            with db.begin_nested():
                row = AssistantConversationState(
                    household_id=household_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    channel=channel,
                    turns=[],
                    last_steps=[],
                    expires_at=moment + timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES),
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            # Lost the race to a concurrent first turn for the exact same
            # (household_id, user_id, conversation_id) -- its row is now
            # committed-within-the-transaction and lockable; join the
            # normal accounting path below instead of losing this turn's
            # memory update unaccounted.
            row = _load_row(
                db,
                household_id=household_id,
                user_id=user_id,
                conversation_id=conversation_id,
                for_update=True,
            )
            if row is None:  # pragma: no cover - defensive, should be unreachable
                raise RuntimeError(
                    "assistant_conversation_context: state row missing after IntegrityError "
                    "on its own unique constraint"
                ) from None

    new_turns = list(row.turns or [])
    new_turns.append({"role": "user", "content": _clean_text(user_message, max_length=MAX_TURN_CONTENT_LENGTH)})
    if assistant_message:
        new_turns.append(
            {"role": "assistant", "content": _clean_text(assistant_message, max_length=MAX_TURN_CONTENT_LENGTH)}
        )
    row.turns = new_turns[-MAX_TURNS:]

    if resolved_step is not None:
        sanitized = _sanitize_step(resolved_step[0], resolved_step[1])
        if sanitized is not None:
            new_steps = list(row.last_steps or [])
            new_steps.append(sanitized)
            row.last_steps = new_steps[-MAX_CONTEXT_STEPS:]

    row.channel = channel
    row.expires_at = moment + timedelta(minutes=CONVERSATION_STATE_TTL_MINUTES)
    db.add(row)


def cleanup_expired_conversation_states(db: Session, *, now: datetime | None = None) -> int:
    """Bulk maintenance sweep -- deletes every conversation state whose
    sliding TTL has already lapsed, regardless of whether its key has been
    touched since. Not wired into any scheduled worker by this slice (see
    module docstring); an operator may invoke it directly, or a future
    slice may schedule it, without changing this function's contract."""

    from app.models import AssistantConversationState  # deferred, same reason as above

    moment = now or datetime.now(UTC)
    result = db.execute(delete(AssistantConversationState).where(AssistantConversationState.expires_at < moment))
    return result.rowcount or 0
