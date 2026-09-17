"""WA-02 (`docs/WORK_ORDER_WA_02.md`, issue #74) Tool Layer -- the strict,
allowlisted set of generic tools a validated `/v1/plan` step may execute.

Architecture (mirrors `app.services.assistant_actions`'s authority
boundary, extended to read-only queries): every tool here either
(a) reads facts already computed by an existing deterministic service
(`app.services.financial_snapshots`, `app.services.card_invoice_lifecycle`,
`app.api.forecast`/`app.api._obligation_rows`) with no new aggregation
logic of its own, or (b) is a thin wrapper over the existing typed-action
pipeline (`app.services.assistant_interpreter`/`app.services.assistant_actions`),
unchanged. No tool ever accepts SQL, a table/column name, or a raw id from
the model -- `arguments` is always a flat bag of free-text hints the
functions below resolve deterministically against real household data,
the same "hint, never a guess" idiom `assistant_actions` already uses for
`account_hint`/`category_hint`. `run_tool` re-validates `tool`/argument
keys against `app.services.assistant_tool_catalog` regardless of what the
`/v1/plan` schema already accepted -- widening that schema alone can never
widen what actually executes here.

Every tool call either succeeds with `facts` (untouched native types --
`Decimal`/`date` included; formatting for a human or for a sanitized trace
is `app.services.assistant_orchestrator`'s job, not this module's) or asks
for clarification -- never guesses a missing account, period, dimension or
proposal/action reference. `ToolOutcome.ok=False` is reserved for a
genuine unexpected failure (never used to signal "needs more info").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.assistant_actions import (
    AssistantActionError,
    build_typed_action_proposal,
    execute_typed_action,
    persist_action_proposal,
    undo_assistant_action,
)
from app.services.assistant_interpreter import interpret_message
from app.services.assistant_tool_catalog import ALLOWED_TOOLS, TOOL_ARGUMENT_KEYS
from app.services.card_invoice_lifecycle import list_invoices, serialize_card_invoice
from app.services.financial_snapshots import (
    account_cash_flow_rows,
    build_snapshot,
    category_spending_rows,
    dashboard_monetary_publication,
)

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

_QUERY_FACTS_TOPICS = ("saldo", "gasto_mes", "fatura", "obrigacoes")
_DIMENSIONS = ("categoria", "conta", "cartao")
_HORIZON_DAYS = ("30", "60", "90")

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_MONTH_NAMES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

_EXPLICIT_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SLASH_PERIOD_RE = re.compile(r"^(0[1-9]|1[0-2])/(\d{4})$")


def sao_paulo_today(now: datetime | None = None) -> date:
    """The caller's "today" for resolving relative date expressions,
    anchored to `America/Sao_Paulo` (Work Order item 5) rather than the
    server's own local/UTC clock -- a request processed just after midnight
    UTC must still resolve "hoje"/"este mês" the way a person in Brazil
    means it."""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(SAO_PAULO).date()


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _add_months(base: date, delta: int) -> date:
    month_index = base.month - 1 + delta
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _period_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def resolve_period(text: str | None, *, today: date) -> str | None:
    """Deterministically resolve a free-text period hint into a `"YYYY-MM"`
    snapshot period key, or `None` when it cannot be resolved with
    confidence -- callers must treat `None` as "ask the user", never as
    "assume this month" (an absent/empty hint is the one case that legitimately
    defaults to the current month, the same default `GET /reports`'s own
    `end_month=None` already uses). Supports: empty/"hoje"/"este mês", "mês
    passado"/"mês retrasado", an explicit month name with an optional year
    ("setembro", "setembro de 2026"), `"YYYY-MM"`, and `"MM/YYYY"`. Anything
    else (a range, "últimos N meses", an unrecognized word) is intentionally
    left unresolved rather than guessed.
    """

    if text is None or not text.strip():
        return _period_key(today.replace(day=1))

    cleaned = _strip_accents(text.strip().lower())
    cleaned = " ".join(cleaned.split())

    if cleaned in ("hoje", "este mes", "esse mes", "mes atual", "mes corrente", "atual"):
        return _period_key(today.replace(day=1))
    if cleaned in ("mes passado", "mes anterior", "ultimo mes"):
        return _period_key(_add_months(today.replace(day=1), -1))
    if cleaned == "mes retrasado":
        return _period_key(_add_months(today.replace(day=1), -2))

    compact = cleaned.replace(" ", "")
    match = _EXPLICIT_PERIOD_RE.match(compact)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = _SLASH_PERIOD_RE.match(compact)
    if match:
        return f"{match.group(2)}-{match.group(1)}"

    tokens = [token for token in re.split(r"[\s/]+", cleaned) if token and token != "de"]
    if tokens and tokens[0] in _MONTH_NAMES:
        month = _MONTH_NAMES[tokens[0]]
        year = today.year
        if len(tokens) >= 2 and tokens[1].isdigit() and len(tokens[1]) == 4:
            year = int(tokens[1])
        elif month > today.month:
            # A bare month name later than the current month means "last
            # year's occurrence" ("em outubro" said in September means
            # October last year, never a future month the household has no
            # data for yet) -- never the current year's not-yet-happened month.
            year -= 1
        return f"{year:04d}-{month:02d}"

    return None


def _extract_reference_id(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        match = _UUID_RE.search(candidate)
        if match:
            return match.group(0)
    return None


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    ok: bool
    facts: dict[str, Any] = field(default_factory=dict)
    clarifying_question: str | None = None


def _dimension_rows(snapshot: Any, dimension: str) -> list[dict[str, Any]]:
    if dimension == "categoria":
        return [
            {"label": str(row["category"]), "amount": row["amount"]}
            for row in category_spending_rows(snapshot)
        ]
    account_rows = account_cash_flow_rows(snapshot)
    wants_card = dimension == "cartao"
    filtered = [
        row for row in account_rows if (row.get("account_type") == "credit_card") == wants_card
    ]
    return [{"label": str(row["account"]), "amount": row["cash_out"]} for row in filtered]


def _tool_query_facts(db: Session, *, user: Any, arguments: dict[str, str], today: date) -> ToolOutcome:
    topic = arguments.get("topic")
    if topic not in _QUERY_FACTS_TOPICS:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "Você quer saber o saldo, o gasto do mês, uma fatura de cartão ou as obrigações "
                "pendentes?"
            ),
        )

    if topic in ("saldo", "gasto_mes"):
        period = resolve_period(arguments.get("period_text"), today=today)
        if period is None:
            return ToolOutcome(
                ok=True,
                clarifying_question="De qual período você quer saber? (por exemplo: este mês, agosto)",
            )
        from app.api import profile_for  # deferred: avoids app.api <-> services circular import

        profile = profile_for(db, user.household_id)
        snapshot = build_snapshot(db, household_id=user.household_id, period=period, generated_by=user.id)
        publication = dashboard_monetary_publication(snapshot, profile=profile)
        return ToolOutcome(
            ok=True,
            facts={
                "topic": topic,
                "period": period,
                "liquidity_balance": publication["liquidity_balance"],
                "spending": publication["spending"],
                "remaining_cap": publication["remaining_cap"],
                "integrity_status": snapshot.integrity_status,
            },
        )

    if topic == "fatura":
        invoices = [
            invoice
            for invoice in list_invoices(db, household_id=user.household_id)
            if invoice.status != "paid"
        ]
        invoices.sort(key=lambda invoice: invoice.due_date)
        serialized = [serialize_card_invoice(invoice) for invoice in invoices[:5]]
        return ToolOutcome(
            ok=True, facts={"topic": "fatura", "invoices": serialized, "total_open": len(invoices)}
        )

    # topic == "obrigacoes"
    from app.api import _obligation_rows  # deferred: same circular-import avoidance as above

    rows = _obligation_rows(db, user.household_id, include_paid=False)
    return ToolOutcome(
        ok=True,
        facts={"topic": "obrigacoes", "obligations": rows[:5], "total_pending": len(rows)},
    )


def _tool_aggregate_spending(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    dimension = arguments.get("dimension")
    if dimension not in _DIMENSIONS:
        return ToolOutcome(
            ok=True, clarifying_question="Você quer o gasto por categoria, por conta ou por cartão?"
        )
    period = resolve_period(arguments.get("period_text"), today=today)
    if period is None:
        return ToolOutcome(
            ok=True,
            clarifying_question="De qual período você quer o gasto? (por exemplo: este mês, setembro)",
        )
    snapshot = build_snapshot(db, household_id=user.household_id, period=period, generated_by=user.id)
    rows = _dimension_rows(snapshot, dimension)
    return ToolOutcome(ok=True, facts={"dimension": dimension, "period": period, "rows": rows})


def _tool_compare_periods(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    dimension = arguments.get("dimension")
    if dimension not in _DIMENSIONS:
        return ToolOutcome(
            ok=True, clarifying_question="Você quer comparar por categoria, por conta ou por cartão?"
        )
    period_a = resolve_period(arguments.get("period_a_text"), today=today)
    period_b = resolve_period(arguments.get("period_b_text"), today=today)
    if period_a is None or period_b is None:
        return ToolOutcome(
            ok=True,
            clarifying_question="Quais dois períodos você quer comparar? (por exemplo: agosto e setembro)",
        )
    snapshot_a = build_snapshot(db, household_id=user.household_id, period=period_a, generated_by=user.id)
    snapshot_b = build_snapshot(db, household_id=user.household_id, period=period_b, generated_by=user.id)
    return ToolOutcome(
        ok=True,
        facts={
            "dimension": dimension,
            "period_a": period_a,
            "period_b": period_b,
            "rows_a": _dimension_rows(snapshot_a, dimension),
            "rows_b": _dimension_rows(snapshot_b, dimension),
        },
    )


def _tool_project_horizon(db: Session, *, user: Any, arguments: dict[str, str]) -> ToolOutcome:
    horizon_raw = arguments.get("horizon_days")
    if horizon_raw not in _HORIZON_DAYS:
        return ToolOutcome(ok=True, clarifying_question="Você quer a projeção para 30, 60 ou 90 dias?")
    horizon_days = int(horizon_raw)
    months = -(-horizon_days // 30)  # ceil(horizon_days / 30): the engine's projection is monthly

    from app.api import forecast as forecast_endpoint  # deferred: same circular-import avoidance

    result = forecast_endpoint(user=user, db=db)
    rows = list(result.get("rows") or [])[:months]
    return ToolOutcome(
        ok=True,
        facts={
            "horizon_days": horizon_days,
            "rows": rows,
            "summary": result.get("summary") or {},
        },
    )


def _tool_draft_typed_action(
    db: Session,
    *,
    user: Any,
    message: str,
    history: Sequence[Any],
    trace_id: str | None,
    interpret_client: Any = None,
) -> ToolOutcome:
    interpretation = interpret_message(
        message=message, history=history, trace_id=trace_id, client=interpret_client
    )
    proposal = build_typed_action_proposal(
        db, household_id=user.household_id, interpretation=interpretation
    )
    if not proposal.can_execute:
        question = (
            proposal.clarifying_question
            or "Não identifiquei uma ação para executar nessa mensagem."
        )
        return ToolOutcome(ok=True, clarifying_question=question, facts={"can_execute": False})

    proposal_id = persist_action_proposal(
        db,
        household_id=user.household_id,
        user_id=user.id,
        trace_id=trace_id or "",
        original_message=message,
        structured_interpretation=interpretation.to_dict(),
        proposal=proposal,
    )
    return ToolOutcome(
        ok=True,
        facts={
            "can_execute": True,
            "proposal_id": proposal_id,
            "typed_action": proposal.typed_action,
            "payload": dict(proposal.payload or {}),
        },
    )


def _not_expired(proposal: Any, *, now: datetime) -> bool:
    expires_at = proposal.expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at >= now


def _find_pending_proposal(
    db: Session, *, household_id: str, user_id: str, reference_text: str | None
) -> Any | None:
    from app.models import AssistantActionProposal  # deferred: keep this module DB-schema-lazy

    now = datetime.now(UTC)
    base_query = select(AssistantActionProposal).where(
        AssistantActionProposal.household_id == household_id,
        AssistantActionProposal.consumed_at.is_(None),
    )
    reference_id = _extract_reference_id(reference_text)
    if reference_id:
        # An explicit reference that does not resolve is never silently
        # replaced by "the most recent one instead" -- that would risk
        # confirming a different proposal than the one the user pointed at.
        row = db.scalar(base_query.where(AssistantActionProposal.id == reference_id))
        return row if row is not None and _not_expired(row, now=now) else None
    row = db.scalar(
        base_query.where(AssistantActionProposal.user_id == user_id)
        .order_by(AssistantActionProposal.created_at.desc())
        .limit(1)
    )
    return row if row is not None and _not_expired(row, now=now) else None


def _tool_confirm_typed_action(db: Session, *, user: Any, arguments: dict[str, str]) -> ToolOutcome:
    proposal = _find_pending_proposal(
        db,
        household_id=user.household_id,
        user_id=user.id,
        reference_text=arguments.get("reference_text"),
    )
    if proposal is None:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "Não encontrei nenhuma proposta pendente para confirmar. Pode repetir o pedido?"
            ),
        )
    try:
        result = execute_typed_action(db, user=user, proposal_id=proposal.id)
    except AssistantActionError as exc:
        return ToolOutcome(ok=True, clarifying_question=str(exc))
    except HTTPException as exc:
        return ToolOutcome(ok=True, clarifying_question=str(exc.detail))
    return ToolOutcome(ok=True, facts={"executed": True, "action": result.get("action")})


def _find_undoable_action(
    db: Session, *, household_id: str, user_id: str, reference_text: str | None
) -> Any | None:
    from app.models import AssistantActionEvent, AuditEvent  # deferred, same reason as above

    base_query = (
        select(AssistantActionEvent)
        .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
        .where(
            AuditEvent.household_id == household_id,
            AssistantActionEvent.undoable.is_(True),
            AssistantActionEvent.undone_at.is_(None),
        )
    )
    reference_id = _extract_reference_id(reference_text)
    if reference_id:
        return db.scalar(base_query.where(AssistantActionEvent.id == reference_id))
    return db.scalar(
        base_query.where(AuditEvent.user_id == user_id)
        .order_by(AssistantActionEvent.created_at.desc())
        .limit(1)
    )


def _tool_undo_typed_action(db: Session, *, user: Any, arguments: dict[str, str]) -> ToolOutcome:
    action_event = _find_undoable_action(
        db,
        household_id=user.household_id,
        user_id=user.id,
        reference_text=arguments.get("reference_text"),
    )
    if action_event is None:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "Não encontrei nenhuma ação recente para desfazer. Qual ação você quer desfazer?"
            ),
        )
    try:
        result = undo_assistant_action(
            db,
            user=user,
            action_id=action_event.id,
            reason="Desfeito via Assistente Financeiro (linguagem natural)",
        )
    except AssistantActionError as exc:
        return ToolOutcome(ok=True, clarifying_question=str(exc))
    return ToolOutcome(ok=True, facts={"undone": True, "result": result})


def run_tool(
    db: Session,
    *,
    user: Any,
    tool: str,
    arguments: Mapping[str, Any] | None,
    message: str,
    history: Sequence[Any] = (),
    trace_id: str | None = None,
    today: date | None = None,
    interpret_client: Any = None,
) -> ToolOutcome:
    """Execute exactly one already-allowlisted plan step.

    `interpret_client` is forwarded only to `draft_typed_action`
    (`app.services.assistant_interpreter.interpret_message`'s own
    injectable client, the same dependency-injection seam
    `tests/test_assistant_interpreter.py` already exercises) -- it lets
    tests substitute a deterministic fake Codex response without touching
    the network, exactly like every other Codex call site in this codebase.

    `tool` and every key of `arguments` are re-checked against
    `app.services.assistant_tool_catalog` here, independent of whatever
    `app.services.assistant_orchestrator` already checked against the raw
    `/v1/plan` response -- two independent gates, neither of which trusts
    the other to have been correct.
    """

    if tool not in ALLOWED_TOOLS:
        return ToolOutcome(ok=False)

    allowed_keys = TOOL_ARGUMENT_KEYS.get(tool, frozenset())
    raw_arguments = arguments if isinstance(arguments, Mapping) else {}
    clean_arguments = {
        str(key): value
        for key, value in raw_arguments.items()
        if key in allowed_keys and isinstance(value, str)
    }
    resolved_today = today or sao_paulo_today()

    if tool == "query_facts":
        return _tool_query_facts(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "aggregate_spending":
        return _tool_aggregate_spending(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "compare_periods":
        return _tool_compare_periods(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "project_horizon":
        return _tool_project_horizon(db, user=user, arguments=clean_arguments)
    if tool == "draft_typed_action":
        return _tool_draft_typed_action(
            db,
            user=user,
            message=message,
            history=history,
            trace_id=trace_id,
            interpret_client=interpret_client,
        )
    if tool == "confirm_typed_action":
        return _tool_confirm_typed_action(db, user=user, arguments=clean_arguments)
    if tool == "undo_typed_action":
        return _tool_undo_typed_action(db, user=user, arguments=clean_arguments)
    return ToolOutcome(ok=False)  # pragma: no cover - unreachable, ALLOWED_TOOLS already checked above
