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
FINANCIAL_RULES_VERSION = "2026.10.1"
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
    has_divergence = divergence != 0
    matches = not synthetic_adjustment and (not has_divergence or disclosed)
    return _result(
        definition,
        context,
        InvariantStatus.PASS if matches else InvariantStatus.FAIL,
        (
            "O saldo confirmado permaneceu soberano; nenhuma divergência foi mascarada e "
            "nenhum ajuste sintético foi criado."
            if matches
            else "Uma divergência de reconciliação foi mascarada ou um ajuste sintético foi "
            "criado em vez de expor a divergência para revisão."
        ),
        expected={"synthetic_adjustment_created": False, "divergence_disclosed": True},
        actual={
            "synthetic_adjustment_created": synthetic_adjustment,
            "divergence_disclosed": disclosed,
            "reconciliation_divergence": divergence,
        },
        difference=abs(divergence) if not matches else Decimal("0.00"),
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
