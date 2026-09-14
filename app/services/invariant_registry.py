"""Stable executable registry for the formal financial invariants."""

from types import MappingProxyType

from app.services.financial_invariants import (
    MONEY_TOLERANCE,
    IntegritySeverity,
    InvariantContext,
    InvariantDefinition,
    InvariantResult,
    validate_advisor_consistency,
    validate_benefits_not_cash,
    validate_canonical_historical_source,
    validate_card_competence,
    validate_card_invoice_divergence_not_silently_adjusted,
    validate_card_invoice_payment_no_duplicate_expense,
    validate_card_invoice_principal_not_new_expense,
    validate_card_invoice_refund_preserves_history,
    validate_card_payment,
    validate_commission_competence,
    validate_confirmed_balance_sovereignty,
    validate_conservative_delay,
    validate_dashboard_consistency,
    validate_deficit_consumes_liquidity,
    validate_individual_receivable_tax,
    validate_internal_transfer,
    validate_investment_application,
    validate_investment_redemption,
    validate_lineage,
    validate_no_automatic_unreceived_commission_in_projection,
    validate_non_negative_liquidity,
    validate_payroll_loan,
    validate_probable_duplicate,
    validate_projection_consistency,
    validate_realized_liquidity_requires_evidence,
    validate_received_commission_excluded_from_projection,
    validate_recurring_income_no_duplicate,
    validate_refund,
    validate_report_consistency,
    validate_safety_floor_is_reference,
    validate_vacation_income,
)

_DEFINITIONS = (
    InvariantDefinition(
        id="INV-001",
        name="internal_transfer_no_operating_effect",
        title="Transferência interna",
        description="Transferências entre contas da família não alteram o resultado operacional.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "family_cash_effect",
        ),
        validator=validate_internal_transfer,
    ),
    InvariantDefinition(
        id="INV-002",
        name="card_payment_not_expense",
        title="Pagamento de cartão",
        description="Pagamento de fatura é conciliação e não uma nova despesa econômica.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
        ),
        validator=validate_card_payment,
    ),
    InvariantDefinition(
        id="INV-003",
        name="investment_not_operating_expense",
        title="Aplicações",
        description="Aplicação é movimento patrimonial, não despesa operacional ou consumo.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "net_worth_effect",
        ),
        validator=validate_investment_application,
    ),
    InvariantDefinition(
        id="INV-004",
        name="redemption_not_operating_income",
        title="Resgates",
        description="Resgate movimenta liquidez patrimonial e não cria receita operacional.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "net_worth_effect",
        ),
        validator=validate_investment_redemption,
    ),
    InvariantDefinition(
        id="INV-005",
        name="liquidity_balance_never_negative",
        title="Privilège não negativo (projeção)",
        description=(
            "PROJEÇÃO/cenário hipotético apenas (October Go-Live Slice 1): liquidez projetada "
            "nunca fica negativa; excedente vira déficit sem cobertura. Um período REALIZADO usa "
            "INV-023/INV-024 em vez desta regra -- ver docs/OCTOBER_GO_LIVE_REBASELINE.md §4.3."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=(
            "opening_liquidity_balance",
            "monthly_operating_result",
            "closing_liquidity_balance",
            "uncovered_deficit",
            "liquidity_used",
        ),
        validator=validate_non_negative_liquidity,
    ),
    InvariantDefinition(
        id="INV-006",
        name="deficit_consumes_liquidity",
        title="Déficit consome liquidez (projeção)",
        description=(
            "PROJEÇÃO/cenário hipotético apenas (October Go-Live Slice 1): resultado projetado "
            "negativo usa a liquidez disponível antes de virar déficit sem cobertura. Nunca se "
            "aplica a um fato REALIZADO -- ver INV-023/INV-024."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=(
            "opening_liquidity_balance",
            "monthly_operating_result",
            "closing_liquidity_balance",
            "uncovered_deficit",
            "liquidity_used",
        ),
        validator=validate_deficit_consumes_liquidity,
    ),
    InvariantDefinition(
        id="INV-007",
        name="safety_floor_is_reference",
        title="Piso não é dinheiro bloqueado (projeção)",
        description=(
            "PROJEÇÃO/cenário hipotético apenas (October Go-Live Slice 1): o piso de segurança "
            "gera alerta, mas não impede cobertura de déficit projetado."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "opening_liquidity_balance",
            "monthly_operating_result",
            "closing_liquidity_balance",
            "uncovered_deficit",
            "liquidity_used",
            "safety_floor",
        ),
        validator=validate_safety_floor_is_reference,
    ),
    InvariantDefinition(
        id="INV-008",
        name="commission_period_competence",
        title="Comissão por competência",
        description="Comissões aparecem somente no período previsto pelo cenário analisado.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "commission_period",
            "scenario_period",
            "scenario_includes_commission",
            "included_in_income",
        ),
        validator=validate_commission_competence,
    ),
    InvariantDefinition(
        id="INV-009",
        name="individual_receivable_tax",
        title="Imposto PJ individual",
        description="O imposto total é a soma do imposto arredondado em cada recebível.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("receivables", "actual_total_tax"),
        validator=validate_individual_receivable_tax,
    ),
    InvariantDefinition(
        id="INV-010",
        name="conservative_delay_never_advances",
        title="Cenário conservador",
        description="A hipótese conservadora pode adiar, mas nunca antecipar um recebimento.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("original_receipt_date", "conservative_receipt_date"),
        validator=validate_conservative_delay,
    ),
    InvariantDefinition(
        id="INV-011",
        name="net_salary_avoids_payroll_loan_double_count",
        title="Consignado",
        description="Consignado já abatido no salário líquido não é descontado novamente.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("payroll_loan_already_deducted", "projection_payroll_loan_deduction"),
        validator=validate_payroll_loan,
    ),
    InvariantDefinition(
        id="INV-012",
        name="vacation_advance_not_extra_income",
        title="Férias",
        description="Adiantamento salarial não é renda extra; apenas o adicional líquido é.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("vacation_advance", "effective_net_extra", "additional_income_effect"),
        validator=validate_vacation_income,
    ),
    InvariantDefinition(
        id="INV-013",
        name="benefits_not_free_cash",
        title="Benefícios",
        description="VA/VR não aumenta caixa livre nem capacidade de pagar dívida ou imóvel.",
        severity=IntegritySeverity.REVIEW,
        required_facts=(
            "benefit_amount",
            "cash_balance_effect",
            "debt_capacity_effect",
            "property_payment_capacity_effect",
        ),
        validator=validate_benefits_not_cash,
    ),
    InvariantDefinition(
        id="INV-014",
        name="probable_duplicates_excluded",
        title="Duplicidade",
        description="Possível duplicidade permanece auditável e fora dos totais até resolução.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("duplicate_confidence", "resolution_status", "included_in_totals"),
        validator=validate_probable_duplicate,
    ),
    InvariantDefinition(
        id="INV-015",
        name="canonical_historical_source",
        title="Fonte canônica histórica",
        description="Uma fonte canônica entra nos totais sem apagar as fontes alternativas.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "canonical_source_id",
            "counted_source_ids",
            "related_source_ids",
            "preserved_source_ids",
        ),
        validator=validate_canonical_historical_source,
    ),
    InvariantDefinition(
        id="INV-016",
        name="refund_offsets_expense",
        title="Estorno",
        description="Estorno compensa a despesa e não vira receita operacional comum.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("refund_amount", "expense_reduction", "operating_income_effect"),
        validator=validate_refund,
    ),
    InvariantDefinition(
        id="INV-017",
        name="card_statement_competence",
        title="Competência de cartão",
        description="Compras respeitam a competência canônica definida para a fatura.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("canonical_competence", "counted_competence"),
        validator=validate_card_competence,
    ),
    InvariantDefinition(
        id="INV-018",
        name="canonical_projection_formula",
        title="Projeção consistente",
        description="Motor e validador independente concordam dentro da tolerância monetária.",
        severity=IntegritySeverity.BLOCK,
        required_facts=(
            "financial_engine_values",
            "projection_validator_values",
            "monetary_tolerance",
        ),
        validator=validate_projection_consistency,
    ),
    InvariantDefinition(
        id="INV-019",
        name="dashboard_uses_canonical_snapshot",
        title="Fonte única do dashboard",
        description="O dataset do dashboard é idêntico ao Financial Engine.",
        severity=IntegritySeverity.BLOCK,
        required_facts=("financial_engine_values", "dashboard_values", "monetary_tolerance"),
        validator=validate_dashboard_consistency,
    ),
    InvariantDefinition(
        id="INV-020",
        name="reports_use_canonical_snapshot",
        title="Fonte única dos relatórios",
        description="Relatórios e exportações usam os valores canônicos sem recalcular.",
        severity=IntegritySeverity.BLOCK,
        required_facts=("financial_engine_values", "report_values", "monetary_tolerance"),
        validator=validate_report_consistency,
    ),
    InvariantDefinition(
        id="INV-021",
        name="advisor_does_not_override_engine",
        title="Advisor sem cálculo oficial independente",
        description="O Advisor explica, mas não altera os números oficiais do Financial Engine.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=("financial_engine_values", "advisor_values", "monetary_tolerance"),
        validator=validate_advisor_consistency,
    ),
    InvariantDefinition(
        id="INV-022",
        name="aggregate_lineage",
        title="Rastreabilidade",
        description="Agregados relevantes preservam fontes, lançamentos, regras e dimensões.",
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "source_count",
            "source_ids",
            "transaction_ids",
            "document_ids",
            "document_required",
            "rule_ids",
            "account_ids",
            "category_ids",
            "calculation_version",
            "lineage_period",
        ),
        validator=validate_lineage,
    ),
    InvariantDefinition(
        id="INV-023",
        name="realized_liquidity_requires_evidence",
        title="Resgate/aplicação REALIZADO exige evidência",
        description=(
            "October Go-Live Slice 1: um período REALIZADO só pode mostrar "
            "liquidity_used/liquidity_deposit do Privilège quando há evidência de movimento real "
            "(transação de 'Transferência patrimonial' importada/observada ou ação confirmada). "
            "Nunca inferido do sinal do resultado operacional; uma AccountBalanceObservation "
            "isolada nunca basta como evidência."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=("liquidity_used", "liquidity_deposit", "has_transfer_evidence"),
        validator=validate_realized_liquidity_requires_evidence,
    ),
    InvariantDefinition(
        id="INV-024",
        name="confirmed_balance_sovereignty",
        title="Saldo confirmado soberano; divergência sinalizada",
        description=(
            "October Go-Live Slice 1: quando existe observação de saldo confirmada, ela prevalece "
            "sobre a reconstrução derivada no instante observado; qualquer divergência é exposta "
            "para investigação, nunca mascarada, e o sistema nunca fabrica um ajuste sintético "
            "para zerá-la. Quando a observação for intra-período, o `closing_liquidity_balance` "
            "publicado também deve ser a âncora confirmada mais o movimento evidenciado posterior "
            "a ela -- divulgar a divergência não basta se o número canônico publicado ainda "
            "discorda dela."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "reconciliation_divergence",
            "synthetic_adjustment_created",
            "divergence_disclosed",
            "confirmed_anchor_present",
            "closing_matches_confirmed_anchor",
        ),
        validator=validate_confirmed_balance_sovereignty,
    ),
    InvariantDefinition(
        id="INV-025",
        name="card_invoice_payment_no_duplicate_expense",
        title="Pagamento de fatura nunca duplica gasto",
        description=(
            "October Go-Live Slice 2: qualquer pagamento de `CardInvoice` -- integral ou parcial, "
            "em um ou mais pagamentos ao longo do tempo -- cria apenas um lançamento de conciliação "
            "na conta pagadora; a contagem de despesas do ciclo permanece idêntica antes e depois."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=("expense_count_before", "expense_count_after"),
        validator=validate_card_invoice_payment_no_duplicate_expense,
    ),
    InvariantDefinition(
        id="INV-026",
        name="card_invoice_principal_not_new_expense",
        title="Principal carregado não é gasto novo",
        description=(
            "October Go-Live Slice 2: o saldo não pago de uma fatura fechada é transportado para o "
            "ciclo seguinte apenas como `principal_carried_in`/`principal_carried_out` -- nunca uma "
            "nova despesa -- e nunca fica negativo."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=("principal_carried_in", "new_expense_transactions_for_carry"),
        validator=validate_card_invoice_principal_not_new_expense,
    ),
    InvariantDefinition(
        id="INV-027",
        name="card_invoice_refund_preserves_history",
        title="Estorno neutraliza sem apagar histórico",
        description=(
            "October Go-Live Slice 2: um estorno vinculado explicitamente a uma compra de cartão "
            "nunca apaga a compra original, nunca neutraliza mais do que o valor efetivamente "
            "gasto, e nunca reescreve `computed_total`/`paid_total`/`status` de um ciclo já pago "
            "quando o próprio estorno é lançado em um ciclo posterior."
        ),
        severity=IntegritySeverity.BLOCK,
        required_facts=(
            "original_transaction_exists",
            "linked_refund_total",
            "original_amount",
            "original_invoice_already_paid",
            "original_invoice_state_unchanged",
        ),
        validator=validate_card_invoice_refund_preserves_history,
    ),
    InvariantDefinition(
        id="INV-028",
        name="card_invoice_divergence_not_silently_adjusted",
        title="Divergência de fatura nunca é ajustada silenciosamente",
        description=(
            "October Go-Live Slice 2: quando `declared_total` diverge de `computed_total + "
            "principal_carried_in` além da tolerância padrão, o sistema reporta um dos quatro "
            "status documentados e nunca fabrica um ajuste sintético para forçar `reconciled`."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=("divergence_status", "synthetic_adjustment_created"),
        validator=validate_card_invoice_divergence_not_silently_adjusted,
    ),
    InvariantDefinition(
        id="INV-029",
        name="received_commission_excluded_from_projection",
        title="Comissão recebida sai da projeção",
        description=(
            "October Go-Live Slice 3: uma comissão já marcada como recebida "
            "(Commission.received_date preenchido) nunca é incluída novamente "
            "como PREVISTO em nenhum cenário de projeção -- o crédito real já "
            "existe como fato; incluí-la de novo duplicaria renda."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=("received_commission_ids", "projected_commission_ids"),
        validator=validate_received_commission_excluded_from_projection,
    ),
    InvariantDefinition(
        id="INV-030",
        name="recurring_income_no_duplicate",
        title="Renda recorrente reconciliada não duplica",
        description=(
            "October Go-Live Slice 3: quando existe evidência de crédito real "
            "para o salário recorrente configurado em um período "
            "(reconcile_recurring_income), a projeção desse período usa "
            "exclusivamente o valor do crédito real -- nunca soma o valor real "
            "ao valor previsto do mesmo período."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=(
            "has_recurring_income_evidence",
            "expected_amount",
            "reconciled_amount",
            "projected_amount",
        ),
        validator=validate_recurring_income_no_duplicate,
    ),
    InvariantDefinition(
        id="INV-031",
        name="no_automatic_unreceived_commission_in_projection",
        title="Comissão não recebida nunca infla a projeção",
        description=(
            "October Go-Live Slice 3 (Round 2): rebaseline §8.3 -- 'Comissões "
            "nunca entram como receita PREVISTA automaticamente'. Mais amplo "
            "que INV-029 (que só protege uma comissão já recebida de "
            "reentrar): prova que nenhuma comissão pendente/não recebida "
            "contribui para nenhum cenário de projeção automaticamente, "
            "verificado contra a saída real de `build_projection`."
        ),
        severity=IntegritySeverity.CRITICAL,
        required_facts=("total_projected_commission", "unreceived_commission_count"),
        validator=validate_no_automatic_unreceived_commission_in_projection,
    ),
)

if len({definition.id for definition in _DEFINITIONS}) != len(_DEFINITIONS):
    raise RuntimeError("financial invariant ids must be unique")
if len({definition.name for definition in _DEFINITIONS}) != len(_DEFINITIONS):
    raise RuntimeError("financial invariant names must be unique")

INVARIANT_REGISTRY = MappingProxyType({definition.id: definition for definition in _DEFINITIONS})


def get_invariant(invariant_id: str) -> InvariantDefinition:
    try:
        return INVARIANT_REGISTRY[invariant_id]
    except KeyError as exc:
        raise KeyError(f"unknown financial invariant: {invariant_id}") from exc


def evaluate_invariant(invariant_id: str, context: InvariantContext) -> InvariantResult:
    return get_invariant(invariant_id).evaluate(context)


def context_with_default_tolerance(
    facts: dict[str, object],
    **context_fields: object,
) -> InvariantContext:
    """Convenience constructor for consumers comparing canonical datasets."""

    complete_facts = {"monetary_tolerance": MONEY_TOLERANCE, **facts}
    return InvariantContext(facts=complete_facts, **context_fields)  # type: ignore[arg-type]
