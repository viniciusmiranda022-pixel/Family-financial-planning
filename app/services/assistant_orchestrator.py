"""WA-02 (`docs/WORK_ORDER_WA_02.md`, issue #74) orchestrator -- the
Python-side authority boundary for the Assistente Financeiro's multi-tool
natural-language planning step, channel-agnostic (the web Assistant is the
only caller wired in this slice; a future WA-03 WhatsApp reply path calls
the exact same `plan_and_execute`, never a second orchestration).

Same discipline as `app.services.assistant_interpreter`: `Plan`/`PlanStep`
carry no field that could itself authorize a mutation or run a query --
`tool` is drawn from the closed `app.services.assistant_tool_catalog`
vocabulary and `arguments` are free-text hints only. `_coerce_plan` is the
only function that reads a raw `/v1/plan` response, and it re-validates
every field regardless of what the sidecar's own schema already checked
(the same "widening the schema alone cannot widen what is trusted" idiom
`assistant_interpreter`/`assistant_sanitizer` already establish).

`plan_and_execute` never computes a financial fact itself: it calls the
LLM exactly once to get a plan, then dispatches every step to
`app.services.assistant_tools.run_tool`, which calls only existing
deterministic services. The natural-language answer is built entirely
from the values those tools returned (`_format_step`), never phrased or
recomputed by the LLM a second time (INV-021 "Advisor sem cálculo oficial
independente", extended here to the Tool Layer). Tool-call depth is
bounded to exactly one planning round (no re-planning loop that feeds
tool results back into another LLM call) and step count is bounded by
`app.services.assistant_tool_catalog.MAX_PLAN_STEPS` -- both are structural
prompt-injection/runaway-loop defenses, not merely a performance choice.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.assistant_conversation_context import (
    CONTEXT_ELIGIBLE_TOOLS as _CONTEXT_ELIGIBLE_TOOLS,
)
from app.services.assistant_conversation_context import (
    ConversationContext,
    load_context,
    record_turn,
)
from app.services.assistant_tool_catalog import ALLOWED_TOOLS, MAX_PLAN_STEPS, TOOL_ARGUMENT_KEYS
from app.services.assistant_tool_sanitizer import build_plan_payload
from app.services.assistant_tools import ToolOutcome, run_tool, sao_paulo_today
from app.services.codex_client import CodexAdvisorClient
from app.services.notification_templates import format_brl


@dataclass(frozen=True, slots=True)
class PlanStep:
    tool: str
    arguments: dict[str, str]


@dataclass(frozen=True, slots=True)
class Plan:
    available: bool
    reason: str | None
    needs_clarification: bool = False
    clarifying_question: str | None = None
    steps: tuple[PlanStep, ...] = ()
    model: str | None = None


def _unavailable(reason: str) -> Plan:
    return Plan(available=False, reason=reason)


def _coerce_step(raw: Any) -> PlanStep | None:
    if not isinstance(raw, Mapping):
        return None
    tool = raw.get("tool")
    if not isinstance(tool, str) or tool not in ALLOWED_TOOLS:
        return None
    allowed_keys = TOOL_ARGUMENT_KEYS.get(tool, frozenset())
    arguments_raw = raw.get("arguments")
    arguments: dict[str, str] = {}
    if isinstance(arguments_raw, Mapping):
        for key, value in arguments_raw.items():
            if key in allowed_keys and isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    arguments[key] = cleaned[:300]
    return PlanStep(tool=tool, arguments=arguments)


def _coerce_plan(raw: Any, *, model: str | None) -> Plan:
    """Turn a raw `/v1/plan` sidecar response into a `Plan`.

    Reads exactly three keys (`needs_clarification`/`clarifying_question`/
    `steps`) and, inside each step, exactly `tool`/`arguments` -- nothing
    else the response might carry is ever looked at, by construction. Any
    shape violation (wrong type, too many steps, an unknown tool name, a
    step that is not a mapping) fails the whole plan closed
    (`available=False`) rather than executing a partially-trusted subset.
    """

    if not isinstance(raw, Mapping):
        return _unavailable("malformed_response")

    needs_clarification = raw.get("needs_clarification")
    if not isinstance(needs_clarification, bool):
        return _unavailable("invalid_schema")

    clarifying_question_raw = raw.get("clarifying_question")
    clarifying_question = (
        clarifying_question_raw.strip()[:300]
        if isinstance(clarifying_question_raw, str) and clarifying_question_raw.strip()
        else None
    )

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        return _unavailable("invalid_schema")
    if len(steps_raw) > MAX_PLAN_STEPS:
        return _unavailable("too_many_steps")

    steps: list[PlanStep] = []
    for item in steps_raw:
        step = _coerce_step(item)
        if step is None:
            return _unavailable("invalid_step")
        steps.append(step)

    if needs_clarification and not clarifying_question:
        clarifying_question = "Pode dar mais detalhes sobre o que você quer saber ou fazer?"

    return Plan(
        available=True,
        reason=None,
        needs_clarification=needs_clarification,
        clarifying_question=clarifying_question,
        steps=tuple(steps),
        model=str(model) if isinstance(model, str) and model else None,
    )


def get_plan(
    *,
    message: str,
    history: Sequence[Any] = (),
    context_hints: Sequence[Any] = (),
    today: date,
    trace_id: str | None = None,
    client: CodexAdvisorClient | None = None,
) -> Plan:
    """Request one Codex plan and return a fail-safe `Plan`. Never raises --
    an unconfigured/unreachable/misbehaving sidecar becomes
    `available=False`, exactly like `assistant_interpreter.interpret_message`,
    never a fabricated empty-but-successful plan.

    `context_hints` (WA-05) is the short, structured list of this same
    conversation's most recent read-only tool calls -- see
    `app.services.assistant_tool_sanitizer.build_plan_payload` for the
    re-validation/shaping this function never duplicates itself."""

    plan_client = client or CodexAdvisorClient()
    if not plan_client.configured:
        return _unavailable("codex_disabled")

    payload = build_plan_payload(
        message=message, history=history, context_hints=context_hints, today=today, trace_id=trace_id
    )

    try:
        result = plan_client.plan(payload)
    except Exception:
        return _unavailable("provider_unreachable")
    if result.payload is None:
        return _unavailable("provider_unreachable")

    model = result.payload.get("model") if isinstance(result.payload, Mapping) else None
    return _coerce_plan(result.payload, model=model)


def _format_money(value: Any) -> str:
    if isinstance(value, Decimal):
        return format_brl(value)
    try:
        return format_brl(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _format_query_facts(facts: dict[str, Any]) -> str:
    topic = facts.get("topic")
    if topic == "saldo":
        return (
            f"Seu saldo de liquidez em {facts.get('period')} é "
            f"{_format_money(facts.get('liquidity_balance'))}."
        )
    if topic == "gasto_mes":
        return (
            f"O gasto considerado em {facts.get('period')} foi {_format_money(facts.get('spending'))}, "
            f"com {_format_money(facts.get('remaining_cap'))} de saldo restante no teto."
        )
    if topic == "fatura":
        invoices = facts.get("invoices") or []
        if not invoices:
            return "Não encontrei nenhuma fatura em aberto."
        parts = [
            f"{invoice.get('competence')}: {_format_money(invoice.get('outstanding_balance'))} "
            f"({invoice.get('financial_state')})"
            for invoice in invoices
        ]
        return "Faturas em aberto: " + "; ".join(parts) + "."
    if topic == "obrigacoes":
        obligations = facts.get("obligations") or []
        total = facts.get("total_pending", 0)
        if not obligations:
            return "Não há obrigações pendentes."
        parts = [
            f"{item.get('name')}: {_format_money(item.get('amount'))} (vence {item.get('due_date')})"
            for item in obligations
        ]
        return f"{total} obrigação(ões) pendente(s): " + "; ".join(parts) + "."
    return "Não consegui identificar o que você quer consultar."


def _format_rows(rows: Sequence[Mapping[str, Any]], *, limit: int = 8) -> str:
    shown = list(rows)[:limit]
    parts = [f"{row.get('label')}: {_format_money(row.get('amount'))}" for row in shown]
    suffix = " e outros" if len(rows) > limit else ""
    return "; ".join(parts) + suffix


def _format_aggregate_spending(facts: dict[str, Any]) -> str:
    rows = facts.get("rows") or []
    if not rows:
        return f"Não encontrei movimentação em {facts.get('period')} para essa dimensão."
    return f"Gasto por {facts.get('dimension')} em {facts.get('period')}: " + _format_rows(rows) + "."


def _sum_amounts(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        try:
            total += Decimal(str(row.get("amount", 0)))
        except (InvalidOperation, TypeError):
            continue
    return total


def _format_compare_periods(facts: dict[str, Any]) -> str:
    rows_a = facts.get("rows_a") or []
    rows_b = facts.get("rows_b") or []
    total_a = _sum_amounts(rows_a)
    total_b = _sum_amounts(rows_b)
    return (
        f"Gasto total por {facts.get('dimension')} em {facts.get('period_a')}: {_format_money(total_a)}; "
        f"em {facts.get('period_b')}: {_format_money(total_b)}."
    )


def _format_project_horizon(facts: dict[str, Any]) -> str:
    summary = facts.get("summary") or {}
    horizon_days = facts.get("horizon_days")
    final_delayed = summary.get("final_delayed")
    viable = summary.get("viable")
    if final_delayed is None:
        return f"Não consegui montar a projeção para {horizon_days} dias agora."
    situation = "permanecendo acima do piso de segurança" if viable else "podendo ficar abaixo do piso de segurança"
    return (
        f"Na projeção para os próximos {horizon_days} dias (cenário conservador com atraso), o saldo "
        f"estimado seria {_format_money(final_delayed)}, {situation}. Esta é uma estimativa "
        f"(PREVISTO), não um fato realizado."
    )


def _format_project_category_pace(facts: dict[str, Any]) -> str:
    amount_so_far = facts.get("amount_so_far")
    projected_total = facts.get("projected_total")
    if amount_so_far is None or projected_total is None:
        return "Não consegui calcular o ritmo de gasto agora."
    period = facts.get("period")
    category_hint = facts.get("category_hint")
    elapsed_days = facts.get("elapsed_days")
    days_in_month = facts.get("days_in_month")
    scope = f"em {category_hint}" if category_hint else "no total"
    return (
        f"Ritmo de gasto {scope} em {period}: {_format_money(amount_so_far)} já gastos em "
        f"{elapsed_days} de {days_in_month} dias. Mantendo esse ritmo, a projeção até o fim do mês é "
        f"{_format_money(projected_total)}. Esta é uma estimativa (PREVISTO), não um fato realizado."
    )


def _format_dimension_label(dimension: str | None) -> str:
    return {
        "categoria": "categoria",
        "conta": "conta",
        "cartao": "cartão",
        "mes": "mês",
        "titular": "titular",
    }.get(dimension or "", str(dimension))


def _format_financial_aggregate(facts: dict[str, Any]) -> str:
    rows = facts.get("rows") or []
    dimension_label = _format_dimension_label(facts.get("dimension"))
    period = facts.get("start_period")
    end_period = facts.get("end_period")
    period_label = period if period == end_period else f"{period} a {end_period}"
    if not rows:
        return f"Não encontrei movimentação em {period_label} para essa consulta."
    metric = facts.get("metric")
    if metric == "participacao":
        parts = [
            f"{row.get('label')}: {_format_money(row.get('amount'))} "
            f"({Decimal(str(row.get('share', 0))) * 100:.1f}%)"
            for row in rows[:8]
        ]
        return f"Participação por {dimension_label} em {period_label}: " + "; ".join(parts) + "."
    if metric == "variacao_percentual":
        parts = []
        for row in rows[:8]:
            variation = row.get("variation_percent")
            variation_text = f"{Decimal(str(variation)):.1f}%" if variation is not None else "sem base de comparação"
            parts.append(f"{row.get('label')}: {_format_money(row.get('amount'))} ({variation_text})")
        return (
            f"Variação por {dimension_label} entre {facts.get('compare_start_period')} e "
            f"{facts.get('compare_end_period')} vs {period_label}: " + "; ".join(parts) + "."
        )
    if metric == "variacao_absoluta":
        parts = [
            f"{row.get('label')}: {_format_money(row.get('amount'))} "
            f"(variação de {_format_money(row.get('variation_absolute'))})"
            for row in rows[:8]
        ]
        return f"Variação por {dimension_label} em {period_label}: " + "; ".join(parts) + "."
    if metric in ("media", "contagem", "minimo", "maximo"):
        return f"{metric.capitalize()} por {dimension_label} em {period_label}: {_format_money(rows[0].get('amount'))}."
    if metric == "total":
        total = facts.get("total")
        detail = f"Gasto por {dimension_label} em {period_label}: " + _format_rows(rows) + "."
        if total is None:
            return detail
        return f"{detail} Total: {_format_money(total)}."
    return f"Gasto por {dimension_label} em {period_label}: " + _format_rows(rows) + "."


def _format_get_income(facts: dict[str, Any]) -> str:
    period = facts.get("start_period")
    end_period = facts.get("end_period")
    period_label = period if period == end_period else f"{period} a {end_period}"
    return f"Renda operacional em {period_label}: {_format_money(facts.get('income'))}."


def _format_get_expenses(facts: dict[str, Any]) -> str:
    period = facts.get("start_period")
    end_period = facts.get("end_period")
    period_label = period if period == end_period else f"{period} a {end_period}"
    return f"Gasto operacional em {period_label}: {_format_money(facts.get('expenses'))}."


def _format_get_commitments(facts: dict[str, Any]) -> str:
    realized = facts.get("realized") or []
    committed = facts.get("committed") or []
    if not realized and not committed:
        return "Não encontrei obrigações realizadas nem pendentes para esse filtro."
    parts = []
    if committed:
        parts.append(
            f"COMPROMETIDO (pendente): {_format_money(facts.get('committed_total'))} em "
            f"{len(committed)} obrigação(ões)"
        )
    if realized:
        parts.append(
            f"REALIZADO (já pago): {_format_money(facts.get('realized_total'))} em {len(realized)} obrigação(ões)"
        )
    return "; ".join(parts) + "."


def _format_get_installments(facts: dict[str, Any]) -> str:
    purchases = facts.get("purchases") or []
    if not purchases:
        return "Não encontrei nenhuma compra parcelada para esse filtro."
    parts = [
        f"{item.get('description')}: contratado {_format_money(item.get('contracted_total'))} "
        f"({item.get('installment_current')}/{item.get('installment_total')}x), impacto neste mês "
        f"{_format_money(item.get('impact_this_month'))}, parcelas futuras "
        f"{_format_money(item.get('future_remaining'))}"
        for item in purchases[:5]
    ]
    return "; ".join(parts) + "."


def _format_search_transactions(facts: dict[str, Any]) -> str:
    transactions = facts.get("transactions") or []
    total_matches = facts.get("total_matches", 0)
    if not transactions:
        return f"Não encontrei transações em {facts.get('period')} para esse filtro."
    parts = [
        f"{row.get('date')}: {row.get('description')} ({_format_money(row.get('amount'))})"
        for row in transactions[:8]
    ]
    suffix = " e outras" if total_matches > len(transactions) else ""
    return f"{total_matches} transação(ões) encontrada(s): " + "; ".join(parts) + suffix + "."


_TYPED_ACTION_LABELS = {
    "create_expense": "uma saída",
    "create_income": "uma entrada",
    "create_internal_transfer": "uma transferência interna",
    "pay_obligation": "o pagamento de uma obrigação",
    "pay_card_invoice": "o pagamento de uma fatura",
    "register_refund": "um estorno",
    "update_asset_value": "a atualização de um ativo",
    "register_asset_contribution": "um aporte em um ativo",
}


def _format_draft_typed_action(facts: dict[str, Any]) -> str:
    typed_action = facts.get("typed_action")
    label = _TYPED_ACTION_LABELS.get(typed_action, "uma ação")
    payload = facts.get("payload") or {}
    details = []
    amount = payload.get("amount")
    if amount is not None:
        details.append(_format_money(amount))
    description = payload.get("description") or payload.get("category_name")
    if description:
        details.append(str(description))
    detail_text = f" ({', '.join(details)})" if details else ""
    return f"Entendi {label}{detail_text}. Confirma?"


def _format_step(tool: str, facts: dict[str, Any]) -> str:
    if tool == "query_facts":
        return _format_query_facts(facts)
    if tool == "aggregate_spending":
        return _format_aggregate_spending(facts)
    if tool == "compare_periods":
        return _format_compare_periods(facts)
    if tool == "project_horizon":
        return _format_project_horizon(facts)
    if tool == "project_category_pace":
        return _format_project_category_pace(facts)
    if tool == "draft_typed_action":
        return _format_draft_typed_action(facts)
    if tool == "confirm_typed_action":
        return "Ação confirmada e executada."
    if tool == "undo_typed_action":
        return "Ação desfeita -- o histórico original foi preservado."
    if tool == "cancel_typed_action":
        return "Combinado, cancelei essa proposta. Nada foi lançado."
    if tool == "financial_aggregate":
        return _format_financial_aggregate(facts)
    if tool == "get_income":
        return _format_get_income(facts)
    if tool == "get_expenses":
        return _format_get_expenses(facts)
    if tool == "get_commitments":
        return _format_get_commitments(facts)
    if tool == "get_installments":
        return _format_get_installments(facts)
    if tool == "search_transactions":
        return _format_search_transactions(facts)
    return ""  # pragma: no cover - unreachable, ALLOWED_TOOLS already checked in run_tool


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    answer: str
    needs_clarification: bool
    clarifying_question: str | None
    proposal_id: str | None
    trace_id: str
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "needs_clarification": self.needs_clarification,
            "clarifying_question": self.clarifying_question,
            "proposal_id": self.proposal_id,
            "trace_id": self.trace_id,
            "trace": list(self.trace),
        }


def _record_orchestration_audit(
    db: Any, *, user: Any, trace_id: str, needs_clarification: bool, trace: tuple[dict[str, Any], ...]
) -> None:
    """Sanitized diagnostic trail only -- tool names, ok/failure/needs-
    clarification flags and argument *keys* (never argument values, tool
    facts, or the user's raw message). `app.services.assistant_tools`'s own
    `draft_typed_action`/`confirm_typed_action`/`undo_typed_action` calls
    already produce the full `AssistantActionEvent`/`AuditEvent` pair
    INV-032 requires for any actual mutation; this is a separate,
    read-scoped record of the orchestration round itself, so a purely
    read-only question ("quanto gastei em setembro?") still leaves an
    auditable trace of what the Assistant did."""

    import app.api as api  # deferred: app.api imports this module's siblings at load time

    api.audit(
        db,
        user,
        "assistant.orchestrate",
        "assistant_orchestration",
        None,
        {"needs_clarification": needs_clarification, "steps": list(trace)},
        trace_id=trace_id,
        source="assistant",
    )
    db.commit()


def _finish(
    db: Any,
    *,
    user: Any,
    message: str,
    trace_id: str,
    answer: str,
    needs_clarification: bool,
    clarifying_question: str | None,
    proposal_id: str | None,
    trace: tuple[dict[str, Any], ...],
    conversation_id: str | None = None,
    channel: str = "web",
    resolved_step: tuple[str, dict[str, Any]] | None = None,
) -> OrchestratorResult:
    """WA-05: every return path of `plan_and_execute` funnels through here,
    so conversational memory is updated exactly once per round, atomically
    with the same commit `_record_orchestration_audit` already performs --
    a lost race on `record_turn`'s own row lock (see
    `app.services.assistant_conversation_context`) never fails this
    request; it only means this turn is not remembered for the next one.
    `conversation_id=None` (no channel wired a conversation key through)
    skips persistence entirely -- unchanged behavior for any caller that
    does not opt in."""

    if conversation_id:
        record_turn(
            db,
            household_id=user.household_id,
            user_id=user.id,
            conversation_id=conversation_id,
            channel=channel,
            user_message=message,
            assistant_message=answer,
            resolved_step=resolved_step,
        )
    _record_orchestration_audit(
        db, user=user, trace_id=trace_id, needs_clarification=needs_clarification, trace=trace
    )
    return OrchestratorResult(
        answer=answer,
        needs_clarification=needs_clarification,
        clarifying_question=clarifying_question,
        proposal_id=proposal_id,
        trace_id=trace_id,
        trace=trace,
    )


def plan_and_execute(
    db: Any,
    *,
    user: Any,
    message: str,
    history: Sequence[Any] = (),
    conversation_id: str | None = None,
    channel: str = "web",
    trace_id: str | None = None,
    client: CodexAdvisorClient | None = None,
    interpret_client: Any = None,
) -> OrchestratorResult:
    """Plan, then deterministically execute, one round of generic tool
    calls for `message` -- the WA-02 Tool Layer's single entry point.

    `user`/`db` fix `household_id`/authorization for every tool call this
    plan can trigger; neither is ever taken from the plan itself (Work
    Order item 10, "preservar autorização de household/usuário"). Never
    raises for a planning/tool failure -- every path returns a safe
    `OrchestratorResult`, falling back to a clarifying question or an
    apology rather than guessing. `client` fakes the `/v1/plan` call;
    `interpret_client` fakes the `/v1/interpret` call a `draft_typed_action`
    step makes -- both are test-only seams, unused in production.

    `conversation_id` (WA-05, `docs/WORK_ORDER_WA_05.md`, issue #77) opts
    this round into short, structured, server-side conversational
    continuity, keyed by `household_id`+`user.id`+`conversation_id` (never
    by household alone -- Vinicius/Kelly always get separate state even
    inside the same household). `None` (the default) preserves the exact
    WA-02/WA-04 behavior: no context loaded, nothing persisted. When set,
    an explicitly caller-supplied `history` is still honored as-is (the web
    Assistant's own client-managed history is never silently overridden);
    only an *empty* `history` falls back to this conversation's own
    persisted turns, and the persisted, structured `last_steps` hints are
    always layered in regardless, since a client has no way to reconstruct
    those itself.
    """

    resolved_trace_id = trace_id or str(uuid.uuid4())
    today = sao_paulo_today()
    context = (
        load_context(db, household_id=user.household_id, user_id=user.id, conversation_id=conversation_id)
        if conversation_id
        else ConversationContext.empty()
    )
    effective_history = history if history else context.turns
    plan = get_plan(
        message=message,
        history=effective_history,
        context_hints=context.last_steps,
        today=today,
        trace_id=resolved_trace_id,
        client=client,
    )

    if not plan.available:
        return _finish(
            db,
            user=user,
            message=message,
            trace_id=resolved_trace_id,
            answer=(
                "Não consegui planejar uma resposta agora. Pode tentar novamente em instantes ou "
                "reformular a pergunta?"
            ),
            needs_clarification=False,
            clarifying_question=None,
            proposal_id=None,
            trace=({"stage": "plan", "ok": False, "reason": plan.reason},),
            conversation_id=conversation_id,
            channel=channel,
        )

    if plan.needs_clarification or not plan.steps:
        question = plan.clarifying_question or "Pode dar mais detalhes sobre o que você quer saber ou fazer?"
        return _finish(
            db,
            user=user,
            message=message,
            trace_id=resolved_trace_id,
            answer=question,
            needs_clarification=True,
            clarifying_question=question,
            proposal_id=None,
            trace=({"stage": "plan", "ok": True, "needs_clarification": True, "model": plan.model},),
            conversation_id=conversation_id,
            channel=channel,
        )

    trace: list[dict[str, Any]] = [
        {"stage": "plan", "ok": True, "step_count": len(plan.steps), "model": plan.model}
    ]
    answers: list[str] = []
    proposal_id: str | None = None
    resolved_step: tuple[str, dict[str, Any]] | None = None

    for index, step in enumerate(plan.steps):
        outcome: ToolOutcome = run_tool(
            db,
            user=user,
            tool=step.tool,
            arguments=step.arguments,
            message=message,
            history=effective_history,
            trace_id=resolved_trace_id,
            today=today,
            interpret_client=interpret_client,
        )
        step_trace: dict[str, Any] = {
            "stage": "tool",
            "index": index,
            "tool": step.tool,
            "argument_keys": sorted(step.arguments.keys()),
            "ok": outcome.ok,
        }
        if outcome.clarifying_question:
            step_trace["needs_clarification"] = True
            trace.append(step_trace)
            return _finish(
                db,
                user=user,
                message=message,
                trace_id=resolved_trace_id,
                answer=outcome.clarifying_question,
                needs_clarification=True,
                clarifying_question=outcome.clarifying_question,
                proposal_id=proposal_id,
                trace=tuple(trace),
                conversation_id=conversation_id,
                channel=channel,
                resolved_step=resolved_step,
            )
        if not outcome.ok:
            step_trace["failed"] = True
            trace.append(step_trace)
            return _finish(
                db,
                user=user,
                message=message,
                trace_id=resolved_trace_id,
                answer="Não consegui concluir essa solicitação agora. Tente novamente em instantes.",
                needs_clarification=False,
                clarifying_question=None,
                proposal_id=proposal_id,
                trace=tuple(trace),
                conversation_id=conversation_id,
                channel=channel,
                resolved_step=resolved_step,
            )

        step_trace["fact_keys"] = sorted(outcome.facts.keys())
        trace.append(step_trace)
        if step.tool == "draft_typed_action" and outcome.facts.get("can_execute"):
            proposal_id = outcome.facts.get("proposal_id")
        if step.tool in _CONTEXT_ELIGIBLE_TOOLS:
            resolved_step = (step.tool, dict(step.arguments))
        answers.append(_format_step(step.tool, outcome.facts))

    final_answer = " ".join(part for part in answers if part) or "Não encontrei nada para responder."
    return _finish(
        db,
        user=user,
        message=message,
        trace_id=resolved_trace_id,
        answer=final_answer,
        needs_clarification=False,
        clarifying_question=None,
        proposal_id=proposal_id,
        trace=tuple(trace),
        conversation_id=conversation_id,
        channel=channel,
        resolved_step=resolved_step,
    )
