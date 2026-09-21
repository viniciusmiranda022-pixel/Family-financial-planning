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

import calendar
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.assistant_actions import (
    AssistantActionError,
    build_typed_action_proposal,
    cancel_action_proposal,
    execute_typed_action,
    parse_amount_text,
    persist_action_proposal,
    undo_assistant_action,
)
from app.services.assistant_interpreter import interpret_message
from app.services.assistant_tool_catalog import ALLOWED_TOOLS, TOOL_ARGUMENT_KEYS
from app.services.card_invoice_lifecycle import list_invoices, serialize_card_invoice
from app.services.finance import add_months, money, month_key
from app.services.financial_query import (
    DIMENSIONS as _AGGREGATE_DIMENSIONS,
)
from app.services.financial_query import (
    METRICS as _AGGREGATE_METRICS,
)
from app.services.financial_query import (
    VARIATION_METRICS as _AGGREGATE_VARIATION_METRICS,
)
from app.services.financial_query import (
    apply_metric,
    apply_top_n,
    collect_range_totals,
    filtered_expense_total,
    holder_rows,
    matches_hint,
    previous_equal_length_range,
    resolve_period,
    resolve_period_range,
    variation_rows,
)
from app.services.financial_query import (
    dimension_rows as range_dimension_rows,
)
from app.services.financial_snapshots import (
    account_cash_flow_rows,
    build_snapshot,
    category_spending_rows,
    dashboard_monetary_publication,
)
from app.services.financial_state import COMPROMETIDO, REALIZADO

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

_QUERY_FACTS_TOPICS = ("saldo", "gasto_mes", "fatura", "obrigacoes")
_DIMENSIONS = ("categoria", "conta", "cartao")
_HORIZON_DAYS = ("30", "60", "90")
_MAX_SEARCH_RESULTS = 20
_MAX_COMMITMENT_ROWS = 10

_TYPE_HINT_MAP = {
    "renda": "income",
    "receita": "income",
    "receitas": "income",
    "entrada": "income",
    "entradas": "income",
    "despesa": "expense",
    "despesas": "expense",
    "saida": "expense",
    "saidas": "expense",
    "gasto": "expense",
    "gastos": "expense",
    "transferencia": "transfer",
    "transferencias": "transfer",
    "estorno": "refund",
    "estornos": "refund",
    "reembolso": "refund",
    "reembolsos": "refund",
    "conciliacao": "reconciliation",
}

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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


def _normalize_type_hint(value: str | None) -> str | None:
    """Free-text movement-type hint -> canonical `Transaction.transaction_type`,
    or `None` when absent/unrecognized (an unrecognized hint is silently
    ignored as "no type filter" -- `search_transactions` still returns
    every other matching row instead of failing closed on a soft filter)."""

    if not value:
        return None
    return _TYPE_HINT_MAP.get(_strip_accents(value.strip().lower()))


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


def _tool_project_category_pace(db: Session, *, user: Any, arguments: dict[str, str], today: date) -> ToolOutcome:
    """WA-05 (`docs/WORK_ORDER_WA_05.md`, issue #77) "e se continuar nessa
    média até o fim do mês?" -- a deterministic linear pace estimate,
    `amount_so_far * days_in_month / elapsed_days`, for the CURRENT,
    still-in-progress month only.

    `amount_so_far` is exactly the same canonical current-month figure
    `get_expenses`/`financial_aggregate` already report for that category
    (`filtered_expense_total`/`RangeTotals.expenses`, via `collect_range_totals`
    -- no new transaction-level query, no second classification of what
    counts as operating expense). This is deliberately the household's
    already-recorded expense evidence for the month to date, not a
    `booked_at`-windowed re-query: since no transaction is ever entered for
    a future date, and a credit-card purchase's `competence` already
    determines which month's canonical total it belongs to exactly the same
    way it does for every other WA-04 tool, this is the one number that
    stays in parity with what the user already sees elsewhere for this same
    period -- never a second, competence-blind "pace-only" total that could
    silently disagree with the Dashboard/Relatórios for the same category.

    `elapsed_days` is `today.day` (never `0` -- day 1 of the month is
    elapsed day 1, not a division by zero) and `days_in_month` is the real
    length of `today`'s month (28/29/30/31 via `calendar.monthrange`).
    Explicitly refuses (clarifies, never guesses) a period that resolves to
    anything other than the month containing `today` -- there is no "pace"
    for a month that already closed or has not started yet."""

    period_text = arguments.get("period_text")
    period = resolve_period(period_text, today=today)
    current_period = month_key(today.replace(day=1))
    if period is None or period != current_period:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "Ritmo de gasto só é calculado para o mês atual, ainda em andamento "
                f"({current_period}) -- não para um mês já fechado ou futuro."
            ),
        )

    category_hint = arguments.get("category_hint")
    totals = collect_range_totals(
        db,
        household_id=user.household_id,
        generated_by=user.id,
        start_period=current_period,
        end_period=current_period,
    )
    amount_so_far = (
        filtered_expense_total(totals, category_hint=category_hint) if category_hint else totals.expenses
    )

    elapsed_days = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projected_total = money(amount_so_far * Decimal(days_in_month) / Decimal(elapsed_days))

    return ToolOutcome(
        ok=True,
        facts={
            "period": current_period,
            "category_hint": category_hint,
            "elapsed_days": elapsed_days,
            "days_in_month": days_in_month,
            "amount_so_far": amount_so_far,
            "projected_total": projected_total,
        },
    )


_MAX_SIMULATED_INSTALLMENTS = 120
_MAX_SIMULATED_MONTHLY_RATE = Decimal("0.30")


def _parse_installments_hint(value: str | None) -> int:
    """Free-text installment-count hint -> a valid `1..120` count, or `1`
    (à vista) for anything absent/unparsable/out of range -- an optional
    field's own documented default, never a reason to block the simulation
    on a clarifying question (mirrors `_normalize_type_hint`'s "unrecognized
    soft hint is silently ignored" idiom)."""

    if not value:
        return 1
    match = re.search(r"\d{1,3}", value)
    if not match:
        return 1
    count = int(match.group(0))
    return count if 1 <= count <= _MAX_SIMULATED_INSTALLMENTS else 1


def _parse_monthly_rate_hint(value: str | None) -> Decimal:
    """Free-text monthly-interest-rate hint (percent, e.g. `"2"`/`"2%"`) ->
    a `Decimal` fraction, or `0` (interest-free) for anything absent/
    unparsable/implausibly high -- same "optional field, documented default,
    never blocks on a clarifying question" idiom as `_parse_installments_hint`.
    """

    if not value:
        return Decimal("0")
    parsed = parse_amount_text(value.replace("%", ""))
    if parsed is None:
        return Decimal("0")
    rate = parsed / Decimal("100")
    return rate if rate <= _MAX_SIMULATED_MONTHLY_RATE else Decimal("0")


def _tool_simulate_purchase(db: Session, *, user: Any, arguments: dict[str, str], today: date) -> ToolOutcome:
    """WA-06 (`docs/WORK_ORDER_WA_06.md`, issue #78) "posso gastar R$ X?" --
    a purely hypothetical, read-only purchase/cash-impact scenario. Never
    persists anything and never drafts a typed action: this tool answers
    "what would happen if", `draft_typed_action` is the only path that can
    ever turn a purchase into a real pending proposal.

    Reuses, rather than reimplements: `app.services.assistant_actions.
    parse_amount_text` for the amount (the exact "hint, never a guess"
    parser the typed-action WRITE pipeline already uses for the same kind of
    free-text amount), `app.schemas.PurchaseScenarioAlternativeRequest` +
    `app.api._purchase_scenario_candidate_schedule` for the amortized
    monthly payment/total cost and installment schedule (the same amortized
    Price/Gauss formula `_advisor_payment` and `POST /purchases/scenario-
    comparison` already use -- see `app.services.finance.
    amortized_installment_payment`'s own docstring), and `app.api.
    _run_purchase_projection_scenario` -- the exact canonical Projection
    Engine/Validator path `POST /purchases/scenario-comparison` runs,
    factored out of that endpoint specifically so this tool can share it
    instead of becoming a second projection engine.

    Deliberately does NOT net the simulated monthly payment against the
    CURRENT month's remaining cap: the canonical projection only ever
    starts from next month onward (the current month's snapshot is already
    closed -- see `_purchase_scenario_candidate_schedule`'s own docstring on
    why it refuses to invent a placement convention), so blending "this
    month's already-closed cap" with "a schedule that can only begin next
    month" would itself be exactly the kind of invented calendar semantics
    that function's docstring documents an engineering review rejecting.
    The current month's cap/spending are reported side by side, as
    unrelated context, never netted against this hypothetical.
    """

    amount = parse_amount_text(arguments.get("amount_text"))
    if amount is None:
        return ToolOutcome(
            ok=True, clarifying_question="Qual o valor da compra que você quer simular?"
        )

    installments = _parse_installments_hint(arguments.get("installments_text"))
    monthly_rate = _parse_monthly_rate_hint(arguments.get("monthly_interest_rate_text"))

    from app.api import (  # deferred: avoids app.api <-> services circular import
        _purchase_scenario_candidate_schedule,
        _run_purchase_projection_scenario,
        profile_for,
    )
    from app.schemas import PurchaseScenarioAlternativeRequest

    try:
        alternative = PurchaseScenarioAlternativeRequest(
            label="Simulação do Assistente",
            price=amount,
            installment_count=installments,
            monthly_interest_rate=monthly_rate,
        )
    except ValueError:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "Não consegui simular essa compra com esses valores. Pode confirmar o valor, a "
                "quantidade de parcelas e a taxa de juros?"
            ),
        )

    profile = profile_for(db, user.household_id)
    current_period = month_key(today.replace(day=1))
    snapshot = build_snapshot(db, household_id=user.household_id, period=current_period, generated_by=user.id)
    publication = dashboard_monetary_publication(snapshot, profile=profile)
    balance_evidence_trusted = bool(snapshot.payload.get("balance_evidence_trusted", False))

    start_month = add_months(today.replace(day=1), 1)
    end_month = profile.projection_end or date(today.year + 1, 12, 1)
    schedule, monthly_payment, total_cost = _purchase_scenario_candidate_schedule(
        alternative, purchase_month=start_month
    )

    common_kwargs = dict(
        household_id=user.household_id,
        profile=profile,
        snapshot=snapshot,
        start_month=start_month,
        end_month=end_month,
        current_period=current_period,
        balance_evidence_trusted=balance_evidence_trusted,
    )
    baseline = _run_purchase_projection_scenario(
        db, extra_installments=None, entity_suffix="assistant_simulate_baseline", **common_kwargs
    )
    with_purchase = _run_purchase_projection_scenario(
        db, extra_installments=schedule, entity_suffix="assistant_simulate_purchase", **common_kwargs
    )
    baseline_delayed = baseline["scenarios"]["delayed"]
    with_purchase_delayed = with_purchase["scenarios"]["delayed"]

    return ToolOutcome(
        ok=True,
        facts={
            "amount": amount,
            "installments": installments,
            "monthly_interest_rate": monthly_rate,
            "monthly_payment": monthly_payment,
            "total_cost": total_cost,
            "current_period": current_period,
            "cash_cap": money(publication["cash_cap"]),
            "spending_so_far": money(publication["spending"]),
            "remaining_cap": money(publication["remaining_cap"]),
            "projection_start_month": month_key(start_month),
            "projection_end_month": month_key(end_month),
            "emergency_floor": money(profile.emergency_floor),
            "baseline_minimum_balance": baseline_delayed["minimum_balance"],
            "baseline_crosses_safety_floor": baseline_delayed["crosses_safety_floor"],
            "with_purchase_minimum_balance": with_purchase_delayed["minimum_balance"],
            "with_purchase_final_balance": with_purchase_delayed["final_balance"],
            "with_purchase_crosses_safety_floor": with_purchase_delayed["crosses_safety_floor"],
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


def _tool_cancel_typed_action(db: Session, *, user: Any, arguments: dict[str, str]) -> ToolOutcome:
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
                "Não encontrei nenhuma proposta pendente para cancelar. Já foi confirmada ou expirou?"
            ),
        )
    try:
        cancel_action_proposal(db, user=user, proposal_id=proposal.id)
    except AssistantActionError as exc:
        return ToolOutcome(ok=True, clarifying_question=str(exc))
    return ToolOutcome(ok=True, facts={"cancelled": True})


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


def _tool_financial_aggregate(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    """WA-04 (`docs/WORK_ORDER_WA_04.md`, issue #76) generic composable
    aggregation: group operational expense by `categoria`/`conta`/`cartao`/
    `mes` over a resolved period range, apply a `metric`, and optionally
    keep only the top `top_n` rows -- all backend-computed and already
    sorted, so the model never sums, sorts or picks a "top N" out of raw
    numbers itself."""

    dimension = arguments.get("dimension")
    if dimension not in _AGGREGATE_DIMENSIONS:
        return ToolOutcome(
            ok=True, clarifying_question="Você quer agrupar por categoria, conta, cartão, mês ou titular?"
        )
    metric = arguments.get("metric") or "total"
    if metric not in _AGGREGATE_METRICS:
        metric = "total"
    period_range = resolve_period_range(arguments.get("period_text"), today=today)
    if period_range is None:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "De qual período? (por exemplo: este mês, últimos 3 meses, últimos 90 dias, ano passado)"
            ),
        )
    start_period, end_period = period_range
    totals = collect_range_totals(
        db,
        household_id=user.household_id,
        generated_by=user.id,
        start_period=start_period,
        end_period=end_period,
    )
    category_hint = arguments.get("category_hint")
    account_hint = arguments.get("account_hint")
    holder_hint = arguments.get("holder_hint")
    # WA-05 (docs/WORK_ORDER_WA_05.md, issue #77) "e se tirar mercado?":
    # symmetric to `category_hint`, but removes a matching row instead of
    # narrowing to it -- composes with every filter above, never overrides
    # them (see `app.services.financial_query.dimension_rows`/`holder_rows`).
    exclude_category_hint = arguments.get("exclude_category_hint")

    def _rows_for(range_totals: Any) -> list[dict[str, Any]]:
        # WA-04 PR #106 review round 1: `titular` is a real grouping
        # dimension, not a `search_transactions`-only filter -- it reads
        # `RangeTotals.detail_rows` (`holder_rows`) so `category_hint` and
        # `account_hint` compose simultaneously with it instead of one
        # silently overriding the other. `categoria`/`conta`/`cartao`/`mes`
        # keep their original single-`label_hint` behavior unchanged (no
        # canonical per-month snapshot output exposes those four dimensions
        # pre-filtered by more than one hint at once without re-deriving
        # what counts as expense -- see `dimension_rows`'s own docstring).
        if dimension == "titular":
            return holder_rows(
                range_totals,
                category_hint=category_hint,
                account_hint=account_hint,
                holder_hint=holder_hint,
                exclude_category_hint=exclude_category_hint,
            )
        label_hint = category_hint or account_hint
        return range_dimension_rows(
            range_totals, dimension, label_hint=label_hint, exclude_category_hint=exclude_category_hint
        )

    rows = _rows_for(totals)

    if metric in _AGGREGATE_VARIATION_METRICS:
        compare_text = arguments.get("compare_period_text")
        if compare_text:
            compare_range = resolve_period_range(compare_text, today=today)
            if compare_range is None:
                return ToolOutcome(
                    ok=True, clarifying_question="Com qual período você quer comparar?"
                )
        else:
            # No explicit comparison period named -- deterministic default:
            # the immediately preceding range of the same length (never a
            # fabricated/guessed period), see
            # `app.services.financial_query.previous_equal_length_range`.
            compare_range = previous_equal_length_range(start_period, end_period)
        compare_start, compare_end = compare_range
        previous_totals = collect_range_totals(
            db,
            household_id=user.household_id,
            generated_by=user.id,
            start_period=compare_start,
            end_period=compare_end,
        )
        previous_rows = _rows_for(previous_totals)
        percentage = metric == "variacao_percentual"
        result_rows = variation_rows(rows, previous_rows, percentage=percentage)
        sort_key = "variation_percent" if percentage else "variation_absolute"
        result_rows = apply_top_n(result_rows, arguments.get("top_n"), sort_key=sort_key)
        return ToolOutcome(
            ok=True,
            facts={
                "dimension": dimension,
                "metric": metric,
                "start_period": start_period,
                "end_period": end_period,
                "compare_start_period": compare_start,
                "compare_end_period": compare_end,
                "rows": result_rows,
            },
        )

    result_rows = apply_metric(rows, metric)
    if metric in ("total", "participacao"):
        sort_key = "share" if metric == "participacao" else "amount"
        result_rows = apply_top_n(result_rows, arguments.get("top_n"), sort_key=sort_key)
    facts: dict[str, Any] = {
        "dimension": dimension,
        "metric": metric,
        "start_period": start_period,
        "end_period": end_period,
        "rows": result_rows,
    }
    if metric == "total":
        # WA-05 (docs/WORK_ORDER_WA_05.md, issue #77) "e se tirar mercado?":
        # a single grand-total fact, plain decimal addition over every
        # matching (pre-`top_n`) row -- already-canonical amounts, no second
        # classification -- so "quanto sobra sem mercado?" never requires the
        # caller to sum `rows` itself (INV-021, no arithmetic outside the
        # deterministic engine).
        facts["total"] = money(sum((row["amount"] for row in rows), Decimal("0")))
    return ToolOutcome(ok=True, facts=facts)


def _tool_get_income(db: Session, *, user: Any, arguments: dict[str, str], today: date) -> ToolOutcome:
    period_range = resolve_period_range(arguments.get("period_text"), today=today)
    if period_range is None:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "De qual período você quer a renda? (por exemplo: este mês, últimos 3 meses, ano passado)"
            ),
        )
    start_period, end_period = period_range
    totals = collect_range_totals(
        db,
        household_id=user.household_id,
        generated_by=user.id,
        start_period=start_period,
        end_period=end_period,
    )
    return ToolOutcome(
        ok=True,
        facts={
            "start_period": start_period,
            "end_period": end_period,
            "income": totals.income,
            "by_month": [
                {"period": period, "amount": amount}
                for period, amount in sorted(totals.month_income_rows.items())
            ],
        },
    )


def _tool_get_expenses(db: Session, *, user: Any, arguments: dict[str, str], today: date) -> ToolOutcome:
    period_range = resolve_period_range(arguments.get("period_text"), today=today)
    if period_range is None:
        return ToolOutcome(
            ok=True,
            clarifying_question=(
                "De qual período você quer o gasto? (por exemplo: este mês, últimos 3 meses, ano passado)"
            ),
        )
    start_period, end_period = period_range
    totals = collect_range_totals(
        db,
        household_id=user.household_id,
        generated_by=user.id,
        start_period=start_period,
        end_period=end_period,
    )
    category_hint = arguments.get("category_hint")
    account_hint = arguments.get("account_hint")
    holder_hint = arguments.get("holder_hint")
    # WA-05 (docs/WORK_ORDER_WA_05.md, issue #77) "e se tirar mercado?":
    # symmetric to `category_hint`, removes a matching category from the
    # total instead of narrowing to it -- see `financial_query.dimension_rows`/
    # `filtered_expense_total`.
    exclude_category_hint = arguments.get("exclude_category_hint")
    category_rows = range_dimension_rows(
        totals, "categoria", label_hint=category_hint, exclude_category_hint=exclude_category_hint
    )
    account_rows = range_dimension_rows(
        totals, "conta", label_hint=account_hint, exclude_category_hint=exclude_category_hint
    ) + range_dimension_rows(totals, "cartao", label_hint=account_hint, exclude_category_hint=exclude_category_hint)
    # WA-04 PR #106 review round 1: `category_hint`/`account_hint`/
    # `holder_hint` compose simultaneously (AND) via `filtered_expense_total`
    # -- the same per-transaction `expense_detail_rows` contributions
    # `category_rows`/`account_rows` above are already built from, just not
    # collapsed to one dimension first -- instead of the previous "category
    # wins over account, no combination possible" priority order. With no
    # hint at all this is exactly `totals.expenses` (every row, unfiltered).
    filtered_total = (
        filtered_expense_total(
            totals,
            category_hint=category_hint,
            account_hint=account_hint,
            holder_hint=holder_hint,
            exclude_category_hint=exclude_category_hint,
        )
        if (category_hint or account_hint or holder_hint or exclude_category_hint)
        else totals.expenses
    )
    return ToolOutcome(
        ok=True,
        facts={
            "start_period": start_period,
            "end_period": end_period,
            "expenses": filtered_total,
            "by_category": category_rows,
            "by_account": account_rows,
            "by_month": [
                {"period": period, "amount": amount}
                for period, amount in sorted(totals.month_expense_rows.items())
            ],
        },
    )


def _tool_get_commitments(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    period_text = arguments.get("period_text")
    period_label: str | None = None
    if period_text:
        period = resolve_period(period_text, today=today)
        if period is None:
            return ToolOutcome(
                ok=True,
                clarifying_question="De qual período você quer as obrigações? (por exemplo: este mês, outubro)",
            )
        period_label = period

    from app.api import _obligation_rows  # deferred: avoids app.api <-> services circular import

    rows = _obligation_rows(db, user.household_id, include_paid=True)
    if period_label is not None:
        start = datetime.strptime(period_label, "%Y-%m").date()
        end = add_months(start, 1)
        rows = [row for row in rows if start <= row["due_date"] < end]

    category_hint = arguments.get("category_hint")
    if category_hint:
        rows = [row for row in rows if matches_hint(category_hint, row.get("category"))]

    status_hint = _strip_accents((arguments.get("status_hint") or "").strip().lower())
    realized = [row for row in rows if row["financial_state"] == REALIZADO]
    committed = [row for row in rows if row["financial_state"] == COMPROMETIDO]
    if status_hint in ("realizado", "pago", "pagas", "pagos"):
        committed = []
    elif status_hint in ("comprometido", "pendente", "pendentes"):
        realized = []

    # `_obligation_rows`' own `amount` is a `float` (`decimal_value`, its
    # established HTTP-response contract) -- round-tripped through `str()`
    # back into `Decimal` here before any arithmetic, the same idiom
    # `app.services.financial_snapshots` already uses whenever it sums a
    # dict/JSON-shaped row (`Decimal(str(row.get("amount", 0)))`), never
    # binary float addition on a monetary total.
    return ToolOutcome(
        ok=True,
        facts={
            "period": period_label,
            "realized": realized[:_MAX_COMMITMENT_ROWS],
            "realized_total": sum((money(Decimal(str(row["amount"]))) for row in realized), Decimal("0")),
            "committed": committed[:_MAX_COMMITMENT_ROWS],
            "committed_total": sum(
                (money(Decimal(str(row["amount"]))) for row in committed), Decimal("0")
            ),
        },
    )


def _tool_get_installments(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    merchant_hint = arguments.get("merchant_hint")
    if not merchant_hint or not merchant_hint.strip():
        return ToolOutcome(
            ok=True,
            clarifying_question="Qual compra parcelada você quer consultar? (ex.: nome da loja/descrição)",
        )

    from app.api import (  # deferred: avoids app.api <-> services circular import
        _installment_anchor_month,
        _installment_remaining_schedule,
        _installment_series_key,
    )
    from app.models import Transaction as TransactionModel

    candidates = db.scalars(
        select(TransactionModel).where(
            TransactionModel.household_id == user.household_id,
            TransactionModel.excluded.is_(False),
            TransactionModel.installment_current.is_not(None),
            TransactionModel.installment_total.is_not(None),
        )
    ).all()
    matched = [row for row in candidates if matches_hint(merchant_hint, row.description)]
    if not matched:
        return ToolOutcome(
            ok=True,
            clarifying_question=f'Não encontrei nenhuma compra parcelada com "{merchant_hint}" na descrição.',
        )

    # Same series-identity/latest-observation policy as
    # `app.api._project_installments` (Work Order: never a second
    # installment engine) -- only replicated here because that function
    # returns one combined household-wide month->amount total, not the
    # per-purchase contracted/impact/future breakdown this tool answers.
    latest_by_series: dict[tuple, TransactionModel] = {}
    for row in matched:
        current = row.installment_current or 0
        total = row.installment_total or 0
        anchor = _installment_anchor_month(competence=row.competence, booked_at=row.booked_at)
        origin = add_months(anchor, -(max(1, current) - 1))
        series = _installment_series_key(
            account_id=row.account_id,
            card_last_four=row.card_last_four,
            description=row.description,
            amount=row.amount,
            installment_total=total,
            origin_month=origin,
        )
        previous = latest_by_series.get(series)
        if previous is None or (row.booked_at, current) > (
            previous.booked_at,
            previous.installment_current or 0,
        ):
            latest_by_series[series] = row

    target_month = today.replace(day=1)
    target_key = f"{target_month.year:04d}-{target_month.month:02d}"
    purchases = []
    for row in latest_by_series.values():
        current = row.installment_current or 0
        total = row.installment_total or 0
        anchor = _installment_anchor_month(competence=row.competence, booked_at=row.booked_at)
        per_installment = money(abs(row.amount))
        contracted_total = money(per_installment * total)
        already_paid = money(per_installment * min(current, total))
        future_remaining = money(contracted_total - already_paid)
        anchor_key = f"{anchor.year:04d}-{anchor.month:02d}"
        if target_key == anchor_key:
            impact_this_month = per_installment
        else:
            schedule = dict(
                _installment_remaining_schedule(
                    amount=row.amount,
                    installment_current=current,
                    installment_total=total,
                    anchor_month=anchor,
                )
            )
            impact_this_month = schedule.get(target_key, Decimal("0"))
        purchases.append(
            {
                "description": row.description,
                "installment_amount": per_installment,
                "installment_current": current,
                "installment_total": total,
                "contracted_total": contracted_total,
                "already_paid": already_paid,
                "future_remaining": future_remaining,
                "impact_this_month": impact_this_month,
            }
        )
    purchases.sort(key=lambda item: item["description"])
    return ToolOutcome(ok=True, facts={"merchant_hint": merchant_hint, "purchases": purchases})


def _tool_search_transactions(
    db: Session, *, user: Any, arguments: dict[str, str], today: date
) -> ToolOutcome:
    period = resolve_period(arguments.get("period_text"), today=today)
    if period is None:
        return ToolOutcome(
            ok=True,
            clarifying_question="De qual período você quer ver as transações? (por exemplo: este mês, agosto)",
        )

    from app.models import Transaction as TransactionModel  # deferred, same reason as above

    start = datetime.strptime(period, "%Y-%m").date()
    end = add_months(start, 1)
    query = (
        select(TransactionModel)
        .where(
            TransactionModel.household_id == user.household_id,
            TransactionModel.booked_at >= start,
            TransactionModel.booked_at < end,
        )
        .order_by(TransactionModel.booked_at.desc())
    )
    type_hint = _normalize_type_hint(arguments.get("type_hint"))
    if type_hint:
        query = query.where(TransactionModel.transaction_type == type_hint)
    rows = list(db.scalars(query))

    category_hint = arguments.get("category_hint")
    account_hint = arguments.get("account_hint")
    holder_hint = arguments.get("holder_hint")
    merchant_hint = arguments.get("merchant_hint")

    filtered = []
    for row in rows:
        category_name = row.category.name if row.category else None
        account_name = row.account.name if row.account else None
        if not matches_hint(category_hint, category_name):
            continue
        if not matches_hint(account_hint, account_name):
            continue
        if not matches_hint(holder_hint, row.owner_label):
            continue
        if not matches_hint(merchant_hint, row.description):
            continue
        filtered.append(row)

    serialized = [
        {
            "date": row.booked_at,
            "description": row.description,
            "amount": money(row.amount),
            "type": row.transaction_type,
            "category": row.category.name if row.category else None,
            "account": row.account.name if row.account else None,
            "holder": row.owner_label,
        }
        for row in filtered[:_MAX_SEARCH_RESULTS]
    ]
    return ToolOutcome(
        ok=True,
        facts={"period": period, "total_matches": len(filtered), "transactions": serialized},
    )


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
    if tool == "project_category_pace":
        return _tool_project_category_pace(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "simulate_purchase":
        return _tool_simulate_purchase(db, user=user, arguments=clean_arguments, today=resolved_today)
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
    if tool == "cancel_typed_action":
        return _tool_cancel_typed_action(db, user=user, arguments=clean_arguments)
    if tool == "financial_aggregate":
        return _tool_financial_aggregate(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "get_income":
        return _tool_get_income(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "get_expenses":
        return _tool_get_expenses(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "get_commitments":
        return _tool_get_commitments(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "get_installments":
        return _tool_get_installments(db, user=user, arguments=clean_arguments, today=resolved_today)
    if tool == "search_transactions":
        return _tool_search_transactions(db, user=user, arguments=clean_arguments, today=resolved_today)
    return ToolOutcome(ok=False)  # pragma: no cover - unreachable, ALLOWED_TOOLS already checked above
