"""WA-02 (`docs/WORK_ORDER_WA_02.md`, issue #74) generic tool catalog --
the single, shared, allowlisted vocabulary of tools the Codex `/v1/plan`
sidecar may choose from and `app.services.assistant_tools` may execute.

Deliberately pure data, zero SQLAlchemy/FastAPI/HTTP imports -- same
isolation discipline as `app.services.assistant_sanitizer`
(`app.services.assistant_tool_sanitizer` imports this module to build the
`/v1/plan` prompt payload; `app.services.assistant_tools` imports it again
to decide what it is willing to execute). Widening `TOOL_CATALOG`'s
description text alone can never widen what a plan is allowed to do: both
consumers re-check every `tool`/argument key against the frozensets below,
not against whatever the model was merely told.
"""

from __future__ import annotations

from typing import NamedTuple


class ToolSpec(NamedTuple):
    name: str
    description: str
    arguments: tuple[tuple[str, str], ...]  # (argument key, human description)


TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="query_facts",
        description=(
            "Consulta um fato pontual já calculado: saldo de liquidez, gasto do mês, "
            "fatura de cartão em aberto ou obrigações pendentes."
        ),
        arguments=(
            ("topic", "um de: saldo, gasto_mes, fatura, obrigacoes"),
            ("period_text", "período em texto livre, opcional (ex.: 'este mês', 'agosto')"),
        ),
    ),
    ToolSpec(
        name="aggregate_spending",
        description="Soma o gasto de um período por categoria, conta ou cartão.",
        arguments=(
            ("dimension", "um de: categoria, conta, cartao"),
            ("period_text", "período em texto livre (ex.: 'setembro', 'mês passado')"),
        ),
    ),
    ToolSpec(
        name="compare_periods",
        description="Compara o gasto por categoria/conta/cartão entre dois períodos.",
        arguments=(
            ("dimension", "um de: categoria, conta, cartao"),
            ("period_a_text", "primeiro período em texto livre"),
            ("period_b_text", "segundo período em texto livre"),
        ),
    ),
    ToolSpec(
        name="project_horizon",
        description="Projeção determinística de liquidez para os próximos 30, 60 ou 90 dias.",
        arguments=(("horizon_days", "um de: 30, 60, 90"),),
    ),
    ToolSpec(
        name="draft_typed_action",
        description=(
            "Interpreta a mensagem original como um lançamento (entrada, saída, transferência, "
            "pagamento de obrigação/fatura, estorno, aporte/atualização de ativo) e devolve uma "
            "proposta para confirmação -- nunca executa nada sozinha."
        ),
        arguments=(),
    ),
    ToolSpec(
        name="confirm_typed_action",
        description="Confirma e executa uma proposta de ação pendente já apresentada ao usuário.",
        arguments=(
            ("reference_text", "trecho/identificador da proposta a confirmar, se houver"),
        ),
    ),
    ToolSpec(
        name="undo_typed_action",
        description="Desfaz uma ação do Assistente já executada anteriormente.",
        arguments=(("reference_text", "trecho/identificador da ação a desfazer, se houver"),),
    ),
)

ALLOWED_TOOLS: frozenset[str] = frozenset(spec.name for spec in TOOL_CATALOG)

TOOL_ARGUMENT_KEYS: dict[str, frozenset[str]] = {
    spec.name: frozenset(key for key, _description in spec.arguments) for spec in TOOL_CATALOG
}

MAX_PLAN_STEPS = 5


def catalog_for_prompt() -> list[dict[str, object]]:
    """Serializable tool catalog sent to `/v1/plan` -- description text only,
    never a callable/table/column, so the sidecar has nothing here it could
    use to reach outside the allowlisted tool names below."""

    return [
        {
            "tool": spec.name,
            "description": spec.description,
            "arguments": [
                {"key": key, "description": description} for key, description in spec.arguments
            ],
        }
        for spec in TOOL_CATALOG
    ]
