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
    validate_card_payment,
    validate_commission_competence,
    validate_conservative_delay,
    validate_dashboard_consistency,
    validate_deficit_consumes_liquidity,
    validate_individual_receivable_tax,
    validate_internal_transfer,
    validate_investment_application,
    validate_investment_redemption,
    validate_lineage,
    validate_non_negative_liquidity,
    validate_payroll_loan,
    validate_probable_duplicate,
    validate_projection_consistency,
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
        title="Privilège não negativo",
        description="Liquidez final nunca fica negativa; excedente vira déficit sem cobertura.",
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
        title="Déficit consome liquidez",
        description="Resultado negativo usa a liquidez disponível antes de virar déficit sem cobertura.",
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
        title="Piso não é dinheiro bloqueado",
        description="O piso de segurança gera alerta, mas não impede cobertura de déficit real.",
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
