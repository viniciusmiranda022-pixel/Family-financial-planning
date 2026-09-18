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
    ToolSpec(
        name="cancel_typed_action",
        description=(
            "Cancela uma proposta de ação pendente ainda não confirmada -- nunca executa nada, "
            "apenas descarta o rascunho."
        ),
        arguments=(("reference_text", "trecho/identificador da proposta a cancelar, se houver"),),
    ),
    # WA-04 (docs/WORK_ORDER_WA_04.md, issue #76) -- generic, composable
    # read/query tools, additive to the WA-02 set above (nothing here
    # replaces query_facts/aggregate_spending/compare_periods).
    ToolSpec(
        name="financial_aggregate",
        description=(
            "Agrega o gasto operacional de um período por categoria, conta, cartão, mês ou titular, com "
            "métrica (total, média, contagem, mínimo, máximo, participação percentual, variação absoluta "
            "ou percentual) e, opcionalmente, apenas os top N resultados. category_hint/account_hint/"
            "holder_hint combinam-se simultaneamente quando mais de um for informado."
        ),
        arguments=(
            ("dimension", "um de: categoria, conta, cartao, mes, titular"),
            ("period_text", "período em texto livre (ex.: 'este mês', 'últimos 3 meses', 'últimos 90 dias')"),
            (
                "metric",
                "um de: total, media, contagem, minimo, maximo, participacao, variacao_absoluta, "
                "variacao_percentual (padrão: total)",
            ),
            ("compare_period_text", "período de comparação, apenas para as métricas de variação"),
            ("category_hint", "nome (ou nomes, separados por vírgula) de categoria para filtrar"),
            ("account_hint", "nome de conta/cartão para filtrar"),
            ("holder_hint", "titular/responsável para filtrar"),
            ("top_n", "quantidade máxima de resultados a devolver, já ordenados"),
        ),
    ),
    ToolSpec(
        name="get_income",
        description="Renda operacional total (e por mês) de um período, nunca incluindo transferência/resgate/aplicação.",
        arguments=(("period_text", "período em texto livre (ex.: 'este mês', 'último ano', 'ano passado')"),),
    ),
    ToolSpec(
        name="get_expenses",
        description=(
            "Gasto operacional total (por categoria, conta/cartão e por mês) de um período, opcionalmente "
            "filtrado por categoria, conta/cartão e/ou titular -- os filtros informados combinam-se "
            "simultaneamente (nunca um sobrepõe o outro)."
        ),
        arguments=(
            ("period_text", "período em texto livre (ex.: 'este mês', 'últimos 3 meses')"),
            ("category_hint", "nome (ou nomes, separados por vírgula) de categoria para filtrar"),
            ("account_hint", "nome de conta/cartão para filtrar"),
            ("holder_hint", "titular/responsável para filtrar"),
        ),
    ),
    ToolSpec(
        name="get_commitments",
        description=(
            "Obrigações REALIZADAS (já pagas) e COMPROMETIDAS (pendentes), opcionalmente filtradas por "
            "período de vencimento, categoria ou status."
        ),
        arguments=(
            ("period_text", "mês de vencimento em texto livre, opcional"),
            ("status_hint", "um de: realizado, comprometido -- opcional, mostra ambos se ausente"),
            ("category_hint", "nome da categoria da obrigação, opcional"),
        ),
    ),
    ToolSpec(
        name="get_installments",
        description=(
            "Detalhe de uma compra parcelada: valor contratado, impacto no mês atual e saldo de parcelas "
            "futuras ainda não realizadas."
        ),
        arguments=(("merchant_hint", "nome/descrição da compra parcelada (obrigatório)"),),
    ),
    ToolSpec(
        name="search_transactions",
        description=(
            "Lista transações já lançadas de um período, filtráveis por categoria, conta/cartão, tipo, "
            "titular ou trecho da descrição -- nunca soma nada, apenas lista os fatos já lançados."
        ),
        arguments=(
            ("period_text", "período em texto livre (ex.: 'este mês', 'agosto')"),
            ("category_hint", "nome da categoria para filtrar"),
            ("account_hint", "nome de conta/cartão para filtrar"),
            ("type_hint", "um de: renda, despesa, transferencia, estorno, conciliacao"),
            ("holder_hint", "titular/responsável para filtrar"),
            ("merchant_hint", "trecho da descrição/estabelecimento para filtrar"),
        ),
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
