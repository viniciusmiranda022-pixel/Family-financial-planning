"""Deterministic contracts used by the Financial Integrity Engine.

This module is deliberately independent from SQLAlchemy, FastAPI and the Advisor.
It contains only pure financial rules and serializable evaluation results, so the
same contract can be reused by imports, projections, reports and tests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

# October Go-Live Rebaseline, Slice 1 (P0 #87): INV-005/INV-006/INV-007 are
# rescoped to the projection engine's hypothetical liquidity formula only,
# and INV-023/INV-024 are added for a REALIZADO period's evidence-based
# liquidity reconciliation. See docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md §5.
# October Go-Live Rebaseline, Slice 3 (P0 #87): INV-029/INV-030 are added so
# a received Commission and a reconciled recurring income never re-enter the
# forward projection as a second, duplicated income fact. See
# docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_3.md.
# October Go-Live Rebaseline, Slice 6 (P0 #87): INV-034 is added so an
# investment/asset's (including the Studio's) contribution to net worth is
# always exactly its current value -- never the sum of cost basis, current
# value and future projection. See
# docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md.
# Issue #85: INV-035 is added so a CVM-quota-derived fund valuation never
# contributes to income, expense, budget usage, operating result or net
# worth -- appreciation is not cash-flow, and this slice deliberately never
# feeds household_patrimony_summary a second time. See
# docs/WORK_ORDER_PRIVILEGE_DI_CVM.md.
FINANCIAL_RULES_VERSION = "2026.10.6"
MONEY_TOLERANCE = Decimal("0.01")
PROBABLE_DUPLICATE_THRESHOLD = Decimal("0.60")
STRONG_DUPLICATE_THRESHOLD = Decimal("0.85")


class InvariantStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    UNKNOWN = "unknown"


class IntegritySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    REVIEW = "review"
    CRITICAL = "critical"
    BLOCK = "block"


class InvariantScope(StrEnum):
    ENTITY = "entity"
    TRANSACTION = "transaction"
    DOCUMENT = "document"
    PERIOD = "period"
    PROJECTION = "projection"
    REPORT = "report"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class InvariantContext:
    """Sanitized facts supplied to one deterministic invariant."""

    facts: Mapping[str, object]
    scope: InvariantScope | str = InvariantScope.SYSTEM
    entity_type: str = "system"
    entity_id: str | int | None = None
    period: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class InvariantResult:
    invariant_id: str
    financial_rules_version: str
    status: InvariantStatus
    severity: IntegritySeverity
    entity_type: str
    entity_id: str | int | None
    message: str
    expected: object | None = None
    actual: object | None = None
    difference: Decimal | None = None
    scope: InvariantScope | str = InvariantScope.ENTITY
    period: str | None = None
    trace_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "invariant_id": self.invariant_id,
            "financial_rules_version": self.financial_rules_version,
            "status": self.status.value,
            "severity": self.severity.value,
            "scope": _json_safe(self.scope),
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "period": self.period,
            "trace_id": self.trace_id,
            "message": self.message,
            "expected": _json_safe(self.expected),
            "actual": _json_safe(self.actual),
            "difference": _json_safe(self.difference),
            "metadata": _json_safe(self.metadata),
        }


class MissingFactsError(ValueError):
    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(f"missing invariant facts: {', '.join(self.missing)}")


class InvalidFactError(ValueError):
    pass


InvariantValidator = Callable[["InvariantDefinition", InvariantContext], InvariantResult]


@dataclass(frozen=True, slots=True)
class InvariantDefinition:
    id: str
    name: str
    title: str
    description: str
    severity: IntegritySeverity
    required_facts: tuple[str, ...]
    validator: InvariantValidator = field(repr=False, compare=False)

    def evaluate(self, context: InvariantContext) -> InvariantResult:
        try:
            _require(context, *self.required_facts)
            return self.validator(self, context)
        except MissingFactsError as exc:
            return _result(
                self,
                context,
                InvariantStatus.UNKNOWN,
                "Dados insuficientes para avaliar a regra sem presumir valores.",
                expected={"required_facts": list(self.required_facts)},
                actual={"available_facts": sorted(str(key) for key in context.facts)},
                metadata={"missing_facts": list(exc.missing)},
            )
        except InvalidFactError as exc:
            return _result(
                self,
                context,
                InvariantStatus.UNKNOWN,
                "Um ou mais dados da regra possuem formato inválido.",
                expected={"required_facts": list(self.required_facts)},
                actual={"error": str(exc)},
            )


@dataclass(frozen=True, slots=True)
class LiquidityTransition:
    opening_balance: Decimal
    monthly_result: Decimal
    liquidity_used: Decimal
    closing_balance: Decimal
    uncovered_deficit: Decimal


def calculate_liquidity_transition(
    opening_balance: Decimal | int | float | str,
    monthly_result: Decimal | int | float | str,
) -> LiquidityTransition:
    """Apply the canonical Privilège/central-liquidity closing rule.

    A negative result consumes all available liquidity before producing uncovered
    deficit. The safety floor is intentionally absent: it is an alert, not locked
    money. A positive result increases central liquidity.
    """

    opening = _money(opening_balance)
    result = _money(monthly_result)
    if opening < 0:
        raise ValueError("opening liquidity balance cannot be negative")

    if result >= 0:
        return LiquidityTransition(
            opening_balance=opening,
            monthly_result=result,
            liquidity_used=Decimal("0.00"),
            closing_balance=_money(opening + result),
            uncovered_deficit=Decimal("0.00"),
        )

    deficit = -result
    used = min(opening, deficit)
    return LiquidityTransition(
        opening_balance=opening,
        monthly_result=result,
        liquidity_used=_money(used),
        closing_balance=_money(max(Decimal("0"), opening - deficit)),
        uncovered_deficit=_money(max(Decimal("0"), deficit - opening)),
    )


def validate_internal_transfer(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    return _validate_zero_effects(
        definition,
        context,
        fields=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "family_cash_effect",
        ),
        pass_message="A transferência interna não alterou renda, despesa, consumo ou caixa familiar.",
        fail_message="A transferência interna alterou indevidamente um total financeiro da família.",
    )


def validate_card_payment(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    return _validate_zero_effects(
        definition,
        context,
        fields=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
        ),
        pass_message="O pagamento da fatura foi tratado somente como conciliação.",
        fail_message="O pagamento da fatura foi contado novamente no resultado operacional.",
    )


def validate_investment_application(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_zero_effects(
        definition,
        context,
        fields=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "net_worth_effect",
        ),
        pass_message="A aplicação foi mantida como movimento patrimonial.",
        fail_message="A aplicação contaminou renda, despesa, consumo ou patrimônio líquido.",
    )


def validate_investment_redemption(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_zero_effects(
        definition,
        context,
        fields=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "net_worth_effect",
        ),
        pass_message="O resgate foi mantido como movimento patrimonial de liquidez.",
        fail_message="O resgate foi contado como renda, despesa, consumo ou ganho patrimonial fictício.",
    )


def validate_fund_valuation_is_not_cash_flow(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-035 (issue #85, `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`): a
    CVM-quota-derived fund valuation (`app.services.privilege_valuation
    .run_valuation_for_household`, `app.models.FundValuation`) must never
    contribute to operating income, operating expense, budget usage,
    operating result, or net worth -- appreciation/depreciation from a
    quota movement is neither income/cash-flow nor a patrimony change in
    this slice (the Work Order's "Financial architecture": "appreciation is
    not income/cash-flow"; this codebase's own scoping decision, see
    `app.models.FundValuation`'s docstring, to keep this slice additive/
    display-only rather than feeding `household_patrimony_summary` a second
    time). Same `_validate_zero_effects` shape as
    `validate_investment_application`/`validate_investment_redemption`."""

    return _validate_zero_effects(
        definition,
        context,
        fields=(
            "operating_income_effect",
            "operating_expense_effect",
            "budget_usage_effect",
            "operating_result_effect",
            "net_worth_effect",
        ),
        pass_message="A valorização do fundo não afetou renda, despesa, consumo, resultado ou patrimônio.",
        fail_message="A valorização do fundo contaminou renda, despesa, consumo, resultado ou patrimônio.",
    )


def validate_net_worth_current_value_only(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-034 (October Go-Live Slice 6): an asset's contribution to net
    worth must equal exactly its `current_value` -- never `historical_cost`
    plus `current_value` plus `expected_receivable_value`, and never any
    other combination of the three (rebaseline §16.2). `historical_cost`/
    `expected_receivable_value` are still required facts so a caller cannot
    satisfy this check by omitting them; they are read here only to be
    reported alongside a failure, never added into the comparison."""

    historical_cost = _decimal(context, "historical_cost")
    current_value = _decimal(context, "current_value")
    expected_receivable_value = _decimal(context, "expected_receivable_value")
    net_worth_contribution = _decimal(context, "net_worth_contribution")
    difference = _money(net_worth_contribution - current_value)
    matches = difference == Decimal("0.00")
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O patrimônio deste ativo considerou somente o valor de hoje."
            if matches
            else "O patrimônio deste ativo somou custo histórico e/ou valor previsto ao valor de hoje."
        ),
        expected={"net_worth_contribution": current_value},
        actual={
            "net_worth_contribution": net_worth_contribution,
            "historical_cost": historical_cost,
            "expected_receivable_value": expected_receivable_value,
        },
        difference=difference,
    )


def validate_non_negative_liquidity(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    transition, actual = _liquidity_values(context)
    expected = {
        "closing_liquidity_balance": transition.closing_balance,
        "uncovered_deficit": transition.uncovered_deficit,
    }
    actual_subset = {
        "closing_liquidity_balance": actual["closing_liquidity_balance"],
        "uncovered_deficit": actual["uncovered_deficit"],
    }
    matches = expected == actual_subset and actual["closing_liquidity_balance"] >= 0
    largest_difference = max(
        abs(actual["closing_liquidity_balance"] - transition.closing_balance),
        abs(actual["uncovered_deficit"] - transition.uncovered_deficit),
    )
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O saldo final da liquidez permaneceu não negativo e o déficit sem cobertura foi separado."
            if matches
            else "O fechamento criou saldo negativo ou calculou incorretamente o déficit sem cobertura."
        ),
        expected=expected,
        actual=actual_subset,
        difference=_money(largest_difference),
    )


def validate_deficit_consumes_liquidity(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    transition, actual = _liquidity_values(context)
    used = actual["liquidity_used"]
    matches = used == transition.liquidity_used
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O resultado negativo consumiu a liquidez disponível antes de gerar déficit sem cobertura."
            if matches
            else "O resultado negativo não consumiu corretamente a liquidez disponível."
        ),
        expected=transition.liquidity_used,
        actual=used,
        difference=_money(used - transition.liquidity_used),
    )


def validate_safety_floor_is_reference(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    _require(context, "safety_floor")
    transition, actual = _liquidity_values(context)
    floor = _decimal(context, "safety_floor")
    used = actual["liquidity_used"]
    matches = used == transition.liquidity_used
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O piso foi usado como referência de alerta, sem bloquear a cobertura do déficit."
            if matches
            else "O piso de segurança bloqueou dinheiro necessário para cobrir um déficit real."
        ),
        expected={"liquidity_used": transition.liquidity_used, "safety_floor_role": "alert"},
        actual={"liquidity_used": used, "safety_floor": floor},
        difference=_money(used - transition.liquidity_used),
    )


def validate_realized_liquidity_requires_evidence(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    used = _decimal(context, "liquidity_used")
    deposit = _decimal(context, "liquidity_deposit")
    has_evidence = _boolean(context, "has_transfer_evidence")
    matches = has_evidence or (used == 0 and deposit == 0)
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "Nenhum resgate/aplicação do Privilège foi registrado como fato sem evidência de "
            "movimento real."
            if matches
            else "O fechamento registrou resgate/aplicação do Privilège como fato realizado sem "
            "nenhuma evidência de movimento real (importado/observado ou ação confirmada)."
        ),
        expected={"liquidity_used": Decimal("0.00"), "liquidity_deposit": Decimal("0.00")}
        if not has_evidence
        else None,
        actual={"liquidity_used": used, "liquidity_deposit": deposit, "has_transfer_evidence": has_evidence},
        difference=_money(used + deposit) if not has_evidence else Decimal("0.00"),
    )


def validate_confirmed_balance_sovereignty(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    divergence = _decimal(context, "reconciliation_divergence")
    synthetic_adjustment = _boolean(context, "synthetic_adjustment_created")
    disclosed = _boolean(context, "divergence_disclosed")
    anchor_present = _boolean(context, "confirmed_anchor_present")
    anchor_matches = _boolean(context, "closing_matches_confirmed_anchor")
    anchor_gap = _decimal(context, "closing_vs_confirmed_anchor_gap")
    has_divergence = divergence != 0
    # Disclosure alone is not enough: when an intra-period confirmed balance
    # exists, the *published* closing position (`closing_liquidity_balance`)
    # must itself be the confirmed anchor plus evidenced movement after it --
    # never the from-opening reconstruction left standing. Otherwise a
    # snapshot could disclose the correct divergence while still publishing
    # the wrong canonical number (rebaseline §5.1/§5.2).
    matches = (
        not synthetic_adjustment
        and (not has_divergence or disclosed)
        and (not anchor_present or anchor_matches)
    )
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O saldo confirmado permaneceu soberano; nenhuma divergência foi mascarada e "
            "nenhum ajuste sintético foi criado."
            if matches
            else "Uma divergência de reconciliação foi mascarada, um ajuste sintético foi "
            "criado, ou o saldo corrente publicado diverge da âncora confirmada mais os "
            "movimentos posteriores a ela."
        ),
        expected={
            "synthetic_adjustment_created": False,
            "divergence_disclosed": True,
            "closing_matches_confirmed_anchor": True,
        },
        actual={
            "synthetic_adjustment_created": synthetic_adjustment,
            "divergence_disclosed": disclosed,
            "reconciliation_divergence": divergence,
            "confirmed_anchor_present": anchor_present,
            "closing_vs_confirmed_anchor_gap": anchor_gap,
        },
        difference=(
            Decimal("0.00")
            if matches
            else abs(anchor_gap)
            if anchor_present and not anchor_matches
            else abs(divergence)
        ),
    )


def validate_commission_competence(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    commission_period = _text(context, "commission_period")
    scenario_period = _text(context, "scenario_period")
    scenario_includes_commission = _boolean(context, "scenario_includes_commission")
    included = _boolean(context, "included_in_income")
    expected = scenario_includes_commission and commission_period == scenario_period
    matches = included is expected
    return _boolean_result(
        definition,
        context,
        matches,
        expected,
        included,
        "A comissão foi incluída somente no período de competência do cenário.",
        "A comissão foi incluída fora do período de competência ou omitida no período correto.",
    )


def validate_individual_receivable_tax(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    items = _mapping_sequence(context, "receivables")
    actual_tax = _decimal(context, "actual_total_tax")
    expected_tax = Decimal("0.00")
    for index, item in enumerate(items):
        try:
            gross = _money(item["gross_amount"])
            rate = Decimal(str(item["tax_rate"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise InvalidFactError(f"receivables[{index}] is invalid") from exc
        expected_tax += _money(gross * rate)
    expected_tax = _money(expected_tax)
    matches = actual_tax == expected_tax
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O imposto total corresponde à soma dos impostos arredondados por recebível."
            if matches
            else "O imposto foi agregado de forma diferente do cálculo individual por recebível."
        ),
        expected=expected_tax,
        actual=actual_tax,
        difference=_money(actual_tax - expected_tax),
    )


def validate_conservative_delay(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    original = _date(context, "original_receipt_date")
    conservative = _date(context, "conservative_receipt_date")
    matches = conservative >= original
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O cenário conservador manteve ou adiou o recebimento."
            if matches
            else "O cenário conservador antecipou indevidamente um recebimento."
        ),
        expected={"minimum_date": original.isoformat()},
        actual={"conservative_date": conservative.isoformat()},
    )


def validate_payroll_loan(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    already_deducted = _boolean(context, "payroll_loan_already_deducted")
    deduction = _decimal(context, "projection_payroll_loan_deduction")
    expected = Decimal("0.00") if already_deducted else deduction
    matches = deduction == expected
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O consignado já refletido no salário líquido não foi descontado novamente."
            if matches
            else "O consignado já descontado no holerite foi abatido novamente na projeção."
        ),
        expected=expected,
        actual=deduction,
        difference=_money(deduction - expected),
    )


def validate_vacation_income(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    _decimal(context, "vacation_advance")
    effective_extra = _decimal(context, "effective_net_extra")
    actual = _decimal(context, "additional_income_effect")
    matches = actual == effective_extra
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "Somente o adicional líquido efetivo de férias foi tratado como renda adicional."
            if matches
            else "O adiantamento salarial de férias foi contado como renda extra."
        ),
        expected=effective_extra,
        actual=actual,
        difference=_money(actual - effective_extra),
    )


def validate_benefits_not_cash(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    _decimal(context, "benefit_amount")
    return _validate_zero_effects(
        definition,
        context,
        fields=("cash_balance_effect", "debt_capacity_effect", "property_payment_capacity_effect"),
        pass_message="Os benefícios permaneceram separados de caixa livre e capacidade de pagar dívida.",
        fail_message="VA/VR foi usado indevidamente como caixa livre ou capacidade de pagar dívida.",
    )


def validate_probable_duplicate(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    confidence = _decimal(context, "duplicate_confidence", quantize=False)
    status = _text(context, "resolution_status")
    included = _boolean(context, "included_in_totals")
    unresolved = status in {"open", "pending", "review", "acknowledged"}
    must_exclude = confidence >= PROBABLE_DUPLICATE_THRESHOLD and unresolved
    expected = False if must_exclude else included
    matches = not included if must_exclude else True
    try:
        confidence_band = duplicate_confidence_band(confidence)
    except ValueError as exc:
        raise InvalidFactError(str(exc)) from exc
    return _boolean_result(
        definition,
        context,
        matches,
        expected,
        included,
        "A possível duplicidade permaneceu auditável e fora dos totais enquanto pendente.",
        "Uma possível duplicidade pendente foi incluída nos totais financeiros.",
        metadata={"confidence_band": confidence_band},
    )


def validate_canonical_historical_source(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    canonical = _text(context, "canonical_source_id")
    counted = _text_sequence(context, "counted_source_ids")
    related = _text_sequence(context, "related_source_ids")
    preserved = _text_sequence(context, "preserved_source_ids")
    related_sources = set(related)
    preserved_sources = set(preserved)
    matches = (
        counted == (canonical,)
        and canonical in related_sources
        and related_sources.issubset(preserved_sources)
        and len(preserved) == len(preserved_sources)
    )
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "A fonte canônica prevaleceu nos totais e as demais fontes foram preservadas."
            if matches
            else "As fontes históricas foram contadas mais de uma vez ou a rastreabilidade foi perdida."
        ),
        expected={
            "counted_source_ids": [canonical],
            "preserved_source_ids": list(related),
        },
        actual={"counted_source_ids": list(counted), "preserved_source_ids": list(preserved)},
    )


def validate_refund(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    refund = _decimal(context, "refund_amount")
    reduction = _decimal(context, "expense_reduction")
    income = _decimal(context, "operating_income_effect")
    matches = reduction == refund and income == 0
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O estorno reduziu a despesa correspondente sem criar receita operacional."
            if matches
            else "O estorno não compensou corretamente a despesa ou foi tratado como receita comum."
        ),
        expected={"expense_reduction": refund, "operating_income_effect": Decimal("0.00")},
        actual={"expense_reduction": reduction, "operating_income_effect": income},
        difference=_money(reduction - refund),
    )


def validate_card_competence(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    expected = _text(context, "canonical_competence")
    actual = _text(context, "counted_competence")
    matches = actual == expected
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "A compra foi contabilizada na competência canônica da fatura."
            if matches
            else "A compra foi contabilizada em competência diferente da regra da fatura."
        ),
        expected=expected,
        actual=actual,
    )


def validate_projection_consistency(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_financial_mappings(
        definition,
        context,
        expected_key="financial_engine_values",
        actual_key="projection_validator_values",
        pass_message="Projection Engine e Projection Validator produziram os mesmos valores canônicos.",
        fail_message="Projection Engine e Projection Validator divergiram acima da tolerância monetária.",
    )


def validate_dashboard_consistency(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_financial_mappings(
        definition,
        context,
        expected_key="financial_engine_values",
        actual_key="dashboard_values",
        pass_message="O dashboard consumiu os valores canônicos do Financial Engine.",
        fail_message="O dashboard divergiu dos valores canônicos do Financial Engine.",
    )


def validate_report_consistency(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_financial_mappings(
        definition,
        context,
        expected_key="financial_engine_values",
        actual_key="report_values",
        pass_message="O relatório consumiu os valores canônicos do Financial Engine.",
        fail_message="O relatório divergiu dos valores canônicos do Financial Engine.",
    )


def validate_advisor_consistency(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    return _validate_financial_mappings(
        definition,
        context,
        expected_key="financial_engine_values",
        actual_key="advisor_values",
        pass_message="O Advisor preservou os números oficiais recebidos do Financial Engine.",
        fail_message="O Advisor substituiu ou alterou números oficiais do Financial Engine.",
    )


def validate_lineage(definition: InvariantDefinition, context: InvariantContext) -> InvariantResult:
    source_count = _integer(context, "source_count")
    source_ids = _text_sequence(context, "source_ids")
    transaction_ids = _text_sequence(context, "transaction_ids")
    rule_ids = _text_sequence(context, "rule_ids")
    account_ids = _text_sequence(context, "account_ids")
    category_ids = _text_sequence(context, "category_ids")
    calculation_version = _text(context, "calculation_version")
    period = _text(context, "lineage_period")
    document_required = _boolean(context, "document_required")
    document_ids = _text_sequence(context, "document_ids")

    missing_dimensions: list[str] = []
    for key, values in (
        ("source_ids", source_ids),
        ("transaction_ids", transaction_ids),
        ("rule_ids", rule_ids),
        ("account_ids", account_ids),
        ("category_ids", category_ids),
    ):
        if not values:
            missing_dimensions.append(key)
    if document_required and not document_ids:
        missing_dimensions.append("document_ids")
    if not calculation_version:
        missing_dimensions.append("calculation_version")
    if not period:
        missing_dimensions.append("lineage_period")

    unique_sources = set(source_ids)
    matches = (
        source_count == len(unique_sources)
        and len(source_ids) == len(unique_sources)
        and source_count > 0
        and not missing_dimensions
    )
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O agregado possui linhagem suficiente para reprodução e auditoria."
            if matches
            else "O agregado não pode ser rastreado integralmente até suas fontes e regras."
        ),
        expected={"source_count": len(unique_sources), "missing_dimensions": []},
        actual={"source_count": source_count, "missing_dimensions": missing_dimensions},
        difference=Decimal(source_count - len(unique_sources)),
    )


def validate_card_invoice_payment_no_duplicate_expense(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-025 (October Go-Live Slice 2): a `CardInvoice` payment -- partial
    or full, in one or more calls over time -- never creates or duplicates
    an `expense` `Transaction`. The cycle's own `expense` count must be
    identical immediately before and immediately after the payment call
    that produced this context (`app.services.card_invoice_lifecycle.
    pay_invoice` always creates a `reconciliation` leg, never a new
    `expense`)."""

    before = _integer(context, "expense_count_before")
    after = _integer(context, "expense_count_after")
    matches = before == after
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O pagamento da fatura não alterou a contagem de despesas do ciclo."
            if matches
            else "O pagamento da fatura alterou a contagem de despesas do ciclo -- possível gasto duplicado."
        ),
        expected={"expense_count": before},
        actual={"expense_count": after},
        difference=Decimal(after - before),
    )


def validate_card_invoice_principal_not_new_expense(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-026: the unpaid balance a closed `CardInvoice` carries into the
    next cycle (`principal_carried_in`) is only ever the cache field itself
    -- never materialized as a new `expense`/purchase `Transaction` in the
    cycle that inherits it. `principal_carried_in` is also never negative
    (this slice never models a card credit balance -- see
    `card_invoice_lifecycle.outstanding_balance`)."""

    principal_carried_in = _decimal(context, "principal_carried_in")
    new_expense_transactions_for_carry = _integer(context, "new_expense_transactions_for_carry")
    matches = principal_carried_in >= 0 and new_expense_transactions_for_carry == 0
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O saldo transportado permanece apenas como campo de fatura, sem nova despesa."
            if matches
            else "O saldo transportado é negativo ou foi materializado como uma nova despesa."
        ),
        expected={"new_expense_transactions_for_carry": 0, "principal_carried_in_negative": False},
        actual={
            "new_expense_transactions_for_carry": new_expense_transactions_for_carry,
            "principal_carried_in_negative": principal_carried_in < 0,
        },
        metadata={"principal_carried_in": str(principal_carried_in)},
    )


def validate_card_invoice_refund_preserves_history(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-027: linking a refund to the card purchase it neutralizes
    (`card_invoice_lifecycle.link_refund`) never deletes the original
    purchase, never lets the sum of linked refunds exceed what was actually
    spent, and -- when the refund lands in a later cycle than an original
    invoice that has *already been paid* -- never rewrites that already-
    settled invoice's `computed_total`/`paid_total`/`status`."""

    original_exists = _boolean(context, "original_transaction_exists")
    linked_refund_total = _decimal(context, "linked_refund_total")
    original_amount = _decimal(context, "original_amount")
    original_invoice_already_paid = _boolean(context, "original_invoice_already_paid")
    original_invoice_state_unchanged = _boolean(context, "original_invoice_state_unchanged")

    within_original_amount = linked_refund_total <= original_amount + MONEY_TOLERANCE
    settled_invoice_untouched = (not original_invoice_already_paid) or original_invoice_state_unchanged
    matches = original_exists and within_original_amount and settled_invoice_untouched
    failures = {
        "original_transaction_deleted": not original_exists,
        "linked_refund_exceeds_original_amount": not within_original_amount,
        "already_paid_invoice_rewritten": original_invoice_already_paid and not original_invoice_state_unchanged,
    }
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O estorno neutralizou apenas o valor vinculado sem apagar ou reescrever histórico."
            if matches
            else "O estorno apagou a compra original, excedeu o valor gasto ou reescreveu fatura já paga."
        ),
        expected={
            "original_transaction_exists": True,
            "linked_refund_total_within_original_amount": True,
            "already_paid_invoice_rewritten": False,
        },
        actual={
            "original_transaction_exists": original_exists,
            "linked_refund_total": str(linked_refund_total),
            "original_amount": str(original_amount),
            **failures,
        },
        difference=_money(max(Decimal("0"), linked_refund_total - original_amount)),
    )


def validate_card_invoice_divergence_not_silently_adjusted(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-028: `card_invoice_lifecycle.invoice_divergence` only ever
    reports one of the four documented statuses and never fabricates a
    synthetic adjustment to force `reconciled` -- rebaseline §6.4."""

    status = _text(context, "divergence_status")
    synthetic_adjustment_created = _boolean(context, "synthetic_adjustment_created")
    allowed_statuses = {"reconciled", "unreconciled_explained", "unreconciled_unexplained", "not_applicable"}
    matches = status in allowed_statuses and not synthetic_adjustment_created
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "A divergência de fatura foi sinalizada sem ajuste sintético."
            if matches
            else "A divergência de fatura foi resolvida com um status inválido ou um ajuste sintético."
        ),
        expected={"divergence_status": sorted(allowed_statuses), "synthetic_adjustment_created": False},
        actual={"divergence_status": status, "synthetic_adjustment_created": synthetic_adjustment_created},
    )


def validate_received_commission_excluded_from_projection(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-029 (October Go-Live Slice 3): a `Commission` already marked
    received (`received_date` set) is never fed into the forecast's income
    lines again -- the real credit already exists as a fact; projecting the
    same commission a second time would duplicate income
    (`docs/OCTOBER_GO_LIVE_REBASELINE.md` §8.3,
    `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §2.4/§4)."""

    received_ids = set(_text_sequence(context, "received_commission_ids"))
    projected_ids = set(_text_sequence(context, "projected_commission_ids"))
    overlap = sorted(received_ids & projected_ids)
    matches = not overlap
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "Nenhuma comissão já recebida foi incluída na projeção futura."
            if matches
            else "Uma comissão já marcada como recebida ainda está incluída na projeção -- risco de renda duplicada."
        ),
        expected={"received_commissions_in_projection": []},
        actual={"received_commissions_in_projection": overlap},
        difference=Decimal(len(overlap)),
    )


def validate_no_automatic_unreceived_commission_in_projection(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-031 (October Go-Live Slice 3, Round 2 of PR #91's review): the
    rebaseline's primary commission rule -- "Comissões nunca entram como
    receita PREVISTA automaticamente ... não deve inflar projeções futuras
    por expectativa" (`docs/OCTOBER_GO_LIVE_REBASELINE.md` §8.3) -- is
    broader than INV-029 (which only guards a commission *already marked
    received* from re-entering). This proves the primary rule directly: no
    matter how many unreceived, non-cancelled `Commission` rows exist for the
    household, the canonical projection's `commission_expected`/
    `commission_delayed` totals across the whole displayed horizon are
    always zero -- there is no explicit, audited opt-in mechanism yet, so
    the only correct automatic contribution is none."""

    total = _decimal(context, "total_projected_commission")
    unreceived_count = _integer(context, "unreceived_commission_count")
    matches = total == 0
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            f"Nenhuma das {unreceived_count} comissões pendentes inflou a projeção "
            "automaticamente."
            if matches
            else (
                f"A projeção somou R$ {total} em comissões pendentes automaticamente -- "
                "violação direta do rebaseline §8.3."
            )
        ),
        expected=Decimal("0.00"),
        actual=total,
        difference=_money(total),
    )


def validate_recurring_income_no_duplicate(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-030 (October Go-Live Slice 3): when
    `app.services.recurring_income.reconcile_recurring_income` finds a real
    credit for the household's configured recurring salary in a period, the
    projection for that period uses *only* the confirmed amount -- never the
    confirmed amount plus the flat PREVISTO estimate
    (`docs/OCTOBER_GO_LIVE_REBASELINE.md` §8.4)."""

    has_evidence = _boolean(context, "has_recurring_income_evidence")
    expected_amount = _decimal(context, "expected_amount")
    reconciled_amount = _decimal(context, "reconciled_amount")
    projected_amount = _decimal(context, "projected_amount")
    expected_projected = reconciled_amount if has_evidence else expected_amount
    matches = projected_amount == expected_projected
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "A renda recorrente do período usa exatamente um valor, sem somar previsto e realizado."
            if matches
            else "A renda recorrente do período somou o valor previsto ao valor real -- risco de renda duplicada."
        ),
        expected=expected_projected,
        actual=projected_amount,
        difference=_money(projected_amount - expected_projected),
    )


def validate_assistant_action_always_audited(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-032 (October Go-Live Slice 4): every typed action the Assistente
    Financeiro executes produces a persistent audit trail -- rebaseline
    §11.3/§18 item 17 ("ação do Assistente é auditável e reversível"). A
    typed-action execution that somehow produced no `AuditEvent`, no paired
    `AssistantActionEvent`, or lost the original user message would be a
    silent gap in exactly the trail the rebaseline requires; this proves
    all three survive together, every time."""

    has_audit_event = _boolean(context, "has_audit_event")
    has_action_event = _boolean(context, "has_action_event")
    original_message_present = _boolean(context, "original_message_present")
    matches = has_audit_event and has_action_event and original_message_present
    return _boolean_result(
        definition,
        context,
        matches,
        expected=True,
        actual=matches,
        pass_message="A ação do Assistente produziu AuditEvent, AssistantActionEvent e mensagem original.",
        fail_message="A ação do Assistente executou sem trilha de auditoria completa.",
        metadata={
            "has_audit_event": has_audit_event,
            "has_action_event": has_action_event,
            "original_message_present": original_message_present,
        },
    )


def validate_assistant_undo_preserves_history(
    definition: InvariantDefinition, context: InvariantContext
) -> InvariantResult:
    """INV-033 (October Go-Live Slice 4): undoing an Assistant action never
    erases the fact that the original action happened -- rebaseline §11.3,
    Work Order item 7 ("Undo... nunca apagar história nem desfazer fato
    externo impossível de reverter"). A reversal always (a) keeps the
    original `AssistantActionEvent` row (marked `undone_at`/`undone_by`,
    never deleted), and (b) creates a brand new `AuditEvent` for the
    reversal itself -- the trail only ever grows."""

    original_action_preserved = _boolean(context, "original_action_preserved")
    undo_audit_event_created = _boolean(context, "undo_audit_event_created")
    undone_at_recorded = _boolean(context, "undone_at_recorded")
    matches = original_action_preserved and undo_audit_event_created and undone_at_recorded
    return _boolean_result(
        definition,
        context,
        matches,
        expected=True,
        actual=matches,
        pass_message="O undo preservou a ação original e registrou um novo AuditEvent da reversão.",
        fail_message="O undo apagou/perdeu a ação original ou não registrou a reversão.",
        metadata={
            "original_action_preserved": original_action_preserved,
            "undo_audit_event_created": undo_audit_event_created,
            "undone_at_recorded": undone_at_recorded,
        },
    )


def duplicate_confidence_band(confidence: Decimal | int | float | str) -> str:
    value = Decimal(str(confidence))
    if value < 0 or value > 1:
        raise ValueError("duplicate confidence must be between 0 and 1")
    if value >= STRONG_DUPLICATE_THRESHOLD:
        return "strong"
    if value >= PROBABLE_DUPLICATE_THRESHOLD:
        return "probable"
    return "low"


def _validate_zero_effects(
    definition: InvariantDefinition,
    context: InvariantContext,
    *,
    fields: Sequence[str],
    pass_message: str,
    fail_message: str,
) -> InvariantResult:
    actual = {key: _decimal(context, key) for key in fields}
    expected = {key: Decimal("0.00") for key in fields}
    matches = actual == expected
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        pass_message if matches else fail_message,
        expected=expected,
        actual=actual,
        difference=_money(sum((abs(value) for value in actual.values()), Decimal("0"))),
    )


def _validate_financial_mappings(
    definition: InvariantDefinition,
    context: InvariantContext,
    *,
    expected_key: str,
    actual_key: str,
    pass_message: str,
    fail_message: str,
) -> InvariantResult:
    expected_raw = _mapping(context, expected_key)
    actual_raw = _mapping(context, actual_key)
    tolerance = _decimal(context, "monetary_tolerance")
    if tolerance < 0:
        raise InvalidFactError("monetary_tolerance cannot be negative")
    expected = _decimal_mapping(expected_raw, expected_key)
    actual = _decimal_mapping(actual_raw, actual_key)
    all_keys = sorted(set(expected) | set(actual))
    mismatches: dict[str, dict[str, object]] = {}
    largest_difference = Decimal("0.00")
    for key in all_keys:
        if key not in expected or key not in actual:
            mismatches[key] = {"expected": expected.get(key), "actual": actual.get(key)}
            continue
        difference = _money(actual[key] - expected[key])
        largest_difference = max(largest_difference, abs(difference))
        if abs(difference) > tolerance:
            mismatches[key] = {
                "expected": expected[key],
                "actual": actual[key],
                "difference": difference,
            }
    matches = not mismatches
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        pass_message if matches else fail_message,
        expected=expected,
        actual=actual,
        difference=largest_difference,
        metadata={"tolerance": tolerance, "mismatches": mismatches},
    )


def _liquidity_values(
    context: InvariantContext,
) -> tuple[LiquidityTransition, dict[str, Decimal]]:
    opening = _decimal(context, "opening_liquidity_balance")
    monthly_result = _decimal(context, "monthly_operating_result")
    try:
        transition = calculate_liquidity_transition(opening, monthly_result)
    except ValueError as exc:
        raise InvalidFactError(str(exc)) from exc
    actual = {
        "closing_liquidity_balance": _decimal(context, "closing_liquidity_balance"),
        "uncovered_deficit": _decimal(context, "uncovered_deficit"),
        "liquidity_used": _decimal(context, "liquidity_used"),
    }
    return transition, actual


def _boolean_result(
    definition: InvariantDefinition,
    context: InvariantContext,
    matches: bool,
    expected: bool,
    actual: bool,
    pass_message: str,
    fail_message: str,
    *,
    metadata: Mapping[str, object] | None = None,
) -> InvariantResult:
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        pass_message if matches else fail_message,
        expected=expected,
        actual=actual,
        metadata=metadata,
    )


def _result(
    definition: InvariantDefinition,
    context: InvariantContext,
    status: InvariantStatus,
    message: str,
    *,
    expected: object | None = None,
    actual: object | None = None,
    difference: Decimal | None = None,
    metadata: Mapping[str, object] | None = None,
) -> InvariantResult:
    return InvariantResult(
        invariant_id=definition.id,
        financial_rules_version=FINANCIAL_RULES_VERSION,
        status=status,
        severity=definition.severity,
        scope=context.scope,
        entity_type=context.entity_type,
        entity_id=context.entity_id,
        period=context.period,
        trace_id=context.trace_id,
        message=message,
        expected=expected,
        actual=actual,
        difference=difference,
        metadata=metadata or {},
    )


def _require(context: InvariantContext, *keys: str) -> None:
    missing = [key for key in keys if key not in context.facts or context.facts[key] is None]
    if missing:
        raise MissingFactsError(missing)


def _decimal(context: InvariantContext, key: str, *, quantize: bool = True) -> Decimal:
    _require(context, key)
    try:
        value = Decimal(str(context.facts[key]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidFactError(f"{key} must be numeric") from exc
    if not value.is_finite():
        raise InvalidFactError(f"{key} must be finite")
    return _money(value) if quantize else value


def _integer(context: InvariantContext, key: str) -> int:
    _require(context, key)
    value = context.facts[key]
    if isinstance(value, bool):
        raise InvalidFactError(f"{key} must be an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise InvalidFactError(f"{key} must be an integer") from exc
    if str(parsed) != str(value):
        raise InvalidFactError(f"{key} must be an integer")
    return parsed


def _boolean(context: InvariantContext, key: str) -> bool:
    _require(context, key)
    value = context.facts[key]
    if not isinstance(value, bool):
        raise InvalidFactError(f"{key} must be boolean")
    return value


def _text(context: InvariantContext, key: str) -> str:
    _require(context, key)
    value = context.facts[key]
    if not isinstance(value, str):
        raise InvalidFactError(f"{key} must be text")
    if not value.strip():
        raise InvalidFactError(f"{key} cannot be empty")
    return value


def _date(context: InvariantContext, key: str) -> date:
    raw = _text(context, key)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidFactError(f"{key} must use ISO date format") from exc


def _mapping(context: InvariantContext, key: str) -> Mapping[str, object]:
    _require(context, key)
    value = context.facts[key]
    if not isinstance(value, Mapping):
        raise InvalidFactError(f"{key} must be an object")
    return value


def _mapping_sequence(context: InvariantContext, key: str) -> tuple[Mapping[str, object], ...]:
    _require(context, key)
    value = context.facts[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidFactError(f"{key} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise InvalidFactError(f"{key} must contain objects")
    return tuple(value)  # type: ignore[arg-type]


def _text_sequence(context: InvariantContext, key: str) -> tuple[str, ...]:
    _require(context, key)
    value = context.facts[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidFactError(f"{key} must be a list")
    if not all(isinstance(item, (str, int)) and not isinstance(item, bool) for item in value):
        raise InvalidFactError(f"{key} must contain identifiers")
    identifiers = tuple(str(item) for item in value)
    if any(not item.strip() for item in identifiers):
        raise InvalidFactError(f"{key} cannot contain empty identifiers")
    return identifiers


def _decimal_mapping(value: Mapping[str, object], fact_name: str) -> dict[str, Decimal]:
    parsed: dict[str, Decimal] = {}
    for key, raw in value.items():
        try:
            parsed[str(key)] = _money(raw)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise InvalidFactError(f"{fact_name}.{key} must be numeric") from exc
    return parsed


def _money(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("monetary value must be finite")
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_json_safe(item) for item in value]
    return value
