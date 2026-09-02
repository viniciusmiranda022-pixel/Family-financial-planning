from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.finance import ForecastInput, build_forecast
from app.services.financial_invariants import (
    FINANCIAL_RULES_VERSION,
    InvariantContext,
    InvariantScope,
    InvariantStatus,
    calculate_liquidity_transition,
    duplicate_confidence_band,
)
from app.services.invariant_registry import INVARIANT_REGISTRY, evaluate_invariant


def _evaluate(invariant_id: str, **facts: object):
    return evaluate_invariant(
        invariant_id,
        InvariantContext(
            facts=facts,
            scope=InvariantScope.PERIOD,
            entity_type="financial_snapshot",
            entity_id="snapshot-2026-08",
            period="2026-08",
            trace_id="trace-test-2026-08",
        ),
    )


ZERO_OPERATING_EFFECTS = {
    "operating_income_effect": Decimal("0"),
    "operating_expense_effect": Decimal("0"),
    "budget_usage_effect": Decimal("0"),
    "operating_result_effect": Decimal("0"),
}


def test_registry_contains_all_permanent_invariants_once() -> None:
    assert FINANCIAL_RULES_VERSION == "2026.09.1"
    assert tuple(INVARIANT_REGISTRY) == tuple(f"INV-{number:03d}" for number in range(1, 23))
    assert len({item.name for item in INVARIANT_REGISTRY.values()}) == 22
    assert all(item.required_facts for item in INVARIANT_REGISTRY.values())


def test_documented_invariants_match_executable_registry() -> None:
    document = (Path(__file__).resolve().parents[1] / "docs/FINANCIAL_INVARIANTS.md").read_text(
        encoding="utf-8"
    )
    for invariant_id in INVARIANT_REGISTRY:
        assert document.count(f"## {invariant_id} —") == 1


def test_missing_facts_are_unknown_and_never_artificial_pass() -> None:
    result = _evaluate("INV-005")
    assert result.status is InvariantStatus.UNKNOWN
    assert result.metadata["missing_facts"] == list(INVARIANT_REGISTRY["INV-005"].required_facts)
    assert result.to_dict()["status"] == "unknown"
    assert result.to_dict()["severity"] == "block"
    assert result.to_dict()["financial_rules_version"] == FINANCIAL_RULES_VERSION
    assert result.to_dict()["trace_id"] == "trace-test-2026-08"


@given(
    transfer_amount=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("100000000.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=200, deadline=None)
def test_internal_transfer_never_changes_operating_result(transfer_amount: Decimal) -> None:
    result = _evaluate(
        "INV-001",
        **ZERO_OPERATING_EFFECTS,
        family_cash_effect=Decimal("0"),
        transfer_amount=transfer_amount,
    )
    assert result.status is InvariantStatus.PASS


def test_card_payment_is_reconciliation_only() -> None:
    valid = _evaluate("INV-002", **ZERO_OPERATING_EFFECTS)
    duplicated = _evaluate(
        "INV-002",
        **{**ZERO_OPERATING_EFFECTS, "operating_expense_effect": Decimal("9032.65")},
    )
    assert valid.status is InvariantStatus.PASS
    assert duplicated.status is InvariantStatus.FAIL
    assert duplicated.difference == Decimal("9032.65")


def test_application_is_patrimonial_movement() -> None:
    valid = _evaluate("INV-003", **ZERO_OPERATING_EFFECTS, net_worth_effect=Decimal("0"))
    as_expense = _evaluate(
        "INV-003",
        **{
            **ZERO_OPERATING_EFFECTS,
            "operating_expense_effect": Decimal("30000"),
            "budget_usage_effect": Decimal("30000"),
        },
        net_worth_effect=Decimal("0"),
    )
    assert valid.status is InvariantStatus.PASS
    assert as_expense.status is InvariantStatus.FAIL


def test_redemption_is_not_operating_income() -> None:
    valid = _evaluate("INV-004", **ZERO_OPERATING_EFFECTS, net_worth_effect=Decimal("0"))
    as_income = _evaluate(
        "INV-004",
        **{**ZERO_OPERATING_EFFECTS, "operating_income_effect": Decimal("10000")},
        net_worth_effect=Decimal("0"),
    )
    assert valid.status is InvariantStatus.PASS
    assert as_income.status is InvariantStatus.FAIL


@given(
    opening=st.decimals(
        min_value=Decimal("0.00"),
        max_value=Decimal("10000000.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    result=st.decimals(
        min_value=Decimal("-20000000.00"),
        max_value=Decimal("10000000.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
@settings(max_examples=500, deadline=None)
def test_liquidity_transition_never_produces_negative_balance(
    opening: Decimal, result: Decimal
) -> None:
    transition = calculate_liquidity_transition(opening, result)
    assert transition.closing_balance >= 0
    assert transition.uncovered_deficit >= 0
    assert not (transition.closing_balance > 0 and transition.uncovered_deficit > 0)
    assert transition.closing_balance - transition.uncovered_deficit == opening + result


@pytest.mark.parametrize(
    ("opening", "result", "used", "closing", "uncovered"),
    (
        ("87068.54", "-10000.00", "10000.00", "77068.54", "0.00"),
        ("87068.54", "-60000.00", "60000.00", "27068.54", "0.00"),
        ("87068.54", "-87068.54", "87068.54", "0.00", "0.00"),
        ("87068.54", "-270014.03", "87068.54", "0.00", "182945.49"),
        ("87068.54", "1000.00", "0.00", "88068.54", "0.00"),
        ("0.00", "-500.00", "0.00", "0.00", "500.00"),
        ("0.00", "500.00", "0.00", "500.00", "0.00"),
        ("0.00", "0.00", "0.00", "0.00", "0.00"),
    ),
)
def test_deficit_uses_all_available_liquidity_before_becoming_uncovered(
    opening: str, result: str, used: str, closing: str, uncovered: str
) -> None:
    transition = calculate_liquidity_transition(opening, result)
    assert transition.liquidity_used == Decimal(used)
    assert transition.closing_balance == Decimal(closing)
    assert transition.uncovered_deficit == Decimal(uncovered)

    facts = {
        "opening_liquidity_balance": opening,
        "monthly_operating_result": result,
        "liquidity_used": used,
        "closing_liquidity_balance": closing,
        "uncovered_deficit": uncovered,
    }
    assert _evaluate("INV-005", **facts).status is InvariantStatus.PASS
    assert _evaluate("INV-006", **facts).status is InvariantStatus.PASS


def test_safety_floor_never_blocks_real_deficit_coverage() -> None:
    canonical = _evaluate(
        "INV-007",
        opening_liquidity_balance="87068.54",
        monthly_operating_result="-70000.00",
        liquidity_used="70000.00",
        closing_liquidity_balance="17068.54",
        uncovered_deficit="0.00",
        safety_floor="30000.00",
    )
    incorrectly_protected = _evaluate(
        "INV-007",
        opening_liquidity_balance="87068.54",
        monthly_operating_result="-70000.00",
        liquidity_used="57068.54",
        closing_liquidity_balance="30000.00",
        uncovered_deficit="12931.46",
        safety_floor="30000.00",
    )
    assert canonical.status is InvariantStatus.PASS
    assert incorrectly_protected.status is InvariantStatus.FAIL


def test_commission_is_included_only_in_its_scenario_period() -> None:
    assert (
        _evaluate(
            "INV-008",
            commission_period="2027-01",
            scenario_period="2026-12",
            scenario_includes_commission=True,
            included_in_income=False,
        ).status
        is InvariantStatus.PASS
    )
    assert (
        _evaluate(
            "INV-008",
            commission_period="2027-01",
            scenario_period="2026-12",
            scenario_includes_commission=True,
            included_in_income=True,
        ).status
        is InvariantStatus.FAIL
    )
    assert (
        _evaluate(
            "INV-008",
            commission_period="2027-01",
            scenario_period="2027-01",
            scenario_includes_commission=False,
            included_in_income=False,
        ).status
        is InvariantStatus.PASS
    )


def test_tax_is_rounded_for_each_receivable() -> None:
    receivables = (
        {"gross_amount": "0.08", "tax_rate": "0.06"},
        {"gross_amount": "0.08", "tax_rate": "0.06"},
    )
    valid = _evaluate("INV-009", receivables=receivables, actual_total_tax="0.00")
    aggregated_first = _evaluate("INV-009", receivables=receivables, actual_total_tax="0.01")
    assert valid.status is InvariantStatus.PASS
    assert aggregated_first.status is InvariantStatus.FAIL


def test_conservative_delay_never_advances_receipt() -> None:
    assert (
        _evaluate(
            "INV-010",
            original_receipt_date="2027-01-10",
            conservative_receipt_date="2027-03-10",
        ).status
        is InvariantStatus.PASS
    )
    assert (
        _evaluate(
            "INV-010",
            original_receipt_date="2027-01-10",
            conservative_receipt_date="2026-12-10",
        ).status
        is InvariantStatus.FAIL
    )


def test_net_salary_does_not_repeat_payroll_loan() -> None:
    valid = _evaluate(
        "INV-011",
        payroll_loan_already_deducted=True,
        projection_payroll_loan_deduction="0.00",
    )
    duplicated = _evaluate(
        "INV-011",
        payroll_loan_already_deducted=True,
        projection_payroll_loan_deduction="850.00",
    )
    assert valid.status is InvariantStatus.PASS
    assert duplicated.status is InvariantStatus.FAIL


def test_vacation_advance_is_not_extra_income() -> None:
    valid = _evaluate(
        "INV-012",
        vacation_advance="5000.00",
        effective_net_extra="1666.67",
        additional_income_effect="1666.67",
    )
    inflated = _evaluate(
        "INV-012",
        vacation_advance="5000.00",
        effective_net_extra="1666.67",
        additional_income_effect="6666.67",
    )
    assert valid.status is InvariantStatus.PASS
    assert inflated.status is InvariantStatus.FAIL


def test_benefits_do_not_increase_free_cash() -> None:
    valid = _evaluate(
        "INV-013",
        benefit_amount="2810.00",
        cash_balance_effect="0.00",
        debt_capacity_effect="0.00",
        property_payment_capacity_effect="0.00",
    )
    used_as_cash = _evaluate(
        "INV-013",
        benefit_amount="2810.00",
        cash_balance_effect="2810.00",
        debt_capacity_effect="0.00",
        property_payment_capacity_effect="0.00",
    )
    assert valid.status is InvariantStatus.PASS
    assert used_as_cash.status is InvariantStatus.FAIL


def test_probable_duplicate_stays_out_of_totals_until_resolution() -> None:
    assert duplicate_confidence_band("0.59") == "low"
    assert duplicate_confidence_band("0.60") == "probable"
    assert duplicate_confidence_band("0.85") == "strong"
    excluded = _evaluate(
        "INV-014",
        duplicate_confidence="0.86",
        resolution_status="open",
        included_in_totals=False,
    )
    counted = _evaluate(
        "INV-014",
        duplicate_confidence="0.86",
        resolution_status="open",
        included_in_totals=True,
    )
    resolved_as_distinct = _evaluate(
        "INV-014",
        duplicate_confidence="0.86",
        resolution_status="resolved",
        included_in_totals=True,
    )
    assert excluded.status is InvariantStatus.PASS
    assert counted.status is InvariantStatus.FAIL
    assert resolved_as_distinct.status is InvariantStatus.PASS
    invalid = _evaluate(
        "INV-014",
        duplicate_confidence="1.10",
        resolution_status="open",
        included_in_totals=False,
    )
    assert invalid.status is InvariantStatus.UNKNOWN


def test_only_canonical_historical_source_is_counted() -> None:
    valid = _evaluate(
        "INV-015",
        canonical_source_id="plan-row-10",
        counted_source_ids=["plan-row-10"],
        related_source_ids=["plan-row-10", "statement-row-20"],
        preserved_source_ids=["plan-row-10", "statement-row-20"],
    )
    duplicated = _evaluate(
        "INV-015",
        canonical_source_id="plan-row-10",
        counted_source_ids=["plan-row-10", "statement-row-20"],
        related_source_ids=["plan-row-10", "statement-row-20"],
        preserved_source_ids=["plan-row-10", "statement-row-20"],
    )
    deleted_evidence = _evaluate(
        "INV-015",
        canonical_source_id="plan-row-10",
        counted_source_ids=["plan-row-10"],
        related_source_ids=["plan-row-10", "statement-row-20"],
        preserved_source_ids=["plan-row-10"],
    )
    assert valid.status is InvariantStatus.PASS
    assert duplicated.status is InvariantStatus.FAIL
    assert deleted_evidence.status is InvariantStatus.FAIL


def test_refund_offsets_expense_without_creating_income() -> None:
    valid = _evaluate(
        "INV-016",
        refund_amount="199.90",
        expense_reduction="199.90",
        operating_income_effect="0.00",
    )
    as_income = _evaluate(
        "INV-016",
        refund_amount="199.90",
        expense_reduction="0.00",
        operating_income_effect="199.90",
    )
    assert valid.status is InvariantStatus.PASS
    assert as_income.status is InvariantStatus.FAIL


def test_card_purchase_uses_statement_competence() -> None:
    valid = _evaluate("INV-017", canonical_competence="2026-09", counted_competence="2026-09")
    shifted = _evaluate("INV-017", canonical_competence="2026-09", counted_competence="2026-08")
    assert valid.status is InvariantStatus.PASS
    assert shifted.status is InvariantStatus.FAIL


def test_projection_mismatch_blocks_trust() -> None:
    result = _evaluate(
        "INV-018",
        financial_engine_values={"2026-09.closing_balance": "50000.00"},
        projection_validator_values={"2026-09.closing_balance": "49999.98"},
        monetary_tolerance="0.01",
    )
    assert result.status is InvariantStatus.FAIL
    assert result.severity.value == "block"
    assert result.difference == Decimal("0.02")
    invalid_tolerance = _evaluate(
        "INV-018",
        financial_engine_values={"2026-09.closing_balance": "50000.00"},
        projection_validator_values={"2026-09.closing_balance": "50000.00"},
        monetary_tolerance="-0.01",
    )
    assert invalid_tolerance.status is InvariantStatus.UNKNOWN


def test_dashboard_dataset_matches_financial_engine() -> None:
    canonical = {"total_income": "25000.00", "total_expenses": "11022.92"}
    assert (
        _evaluate(
            "INV-019",
            financial_engine_values=canonical,
            dashboard_values=canonical,
            monetary_tolerance="0.01",
        ).status
        is InvariantStatus.PASS
    )


def test_report_dataset_matches_financial_engine() -> None:
    canonical = {"total_income": "25000.00", "total_expenses": "11022.92"}
    mismatch = _evaluate(
        "INV-020",
        financial_engine_values=canonical,
        report_values={"total_income": "25000.00", "total_expenses": "11022.94"},
        monetary_tolerance="0.01",
    )
    assert mismatch.status is InvariantStatus.FAIL
    assert mismatch.severity.value == "block"


def test_advisor_cannot_override_official_values() -> None:
    canonical = {"cash_balance": "43875.62", "uncovered_deficit": "0.00"}
    altered = _evaluate(
        "INV-021",
        financial_engine_values=canonical,
        advisor_values={"cash_balance": "50000.00", "uncovered_deficit": "0.00"},
        monetary_tolerance="0.01",
    )
    assert altered.status is InvariantStatus.FAIL


def test_aggregate_requires_complete_lineage() -> None:
    facts = {
        "source_count": 2,
        "source_ids": ["source-a", "source-b"],
        "transaction_ids": ["transaction-a", "transaction-b"],
        "document_ids": ["document-a"],
        "document_required": True,
        "rule_ids": ["INV-001", "INV-014"],
        "account_ids": ["account-a", "account-b"],
        "category_ids": ["category-a"],
        "calculation_version": FINANCIAL_RULES_VERSION,
        "lineage_period": "2026-08",
    }
    valid = _evaluate("INV-022", **facts)
    missing_document = _evaluate("INV-022", **{**facts, "document_ids": []})
    wrong_count = _evaluate("INV-022", **{**facts, "source_count": 3})
    duplicated_source = _evaluate(
        "INV-022",
        **{**facts, "source_count": 2, "source_ids": ["source-a", "source-b", "source-b"]},
    )
    assert valid.status is InvariantStatus.PASS
    assert missing_document.status is InvariantStatus.FAIL
    assert wrong_count.status is InvariantStatus.FAIL
    assert duplicated_source.status is InvariantStatus.FAIL


def test_projection_exposes_deficit_instead_of_negative_balance() -> None:
    row = build_forecast(
        ForecastInput(
            start_month=date(2026, 9, 1),
            end_month=date(2026, 9, 1),
            starting_balance=Decimal("100.00"),
            monthly_salary=Decimal("0.00"),
            monthly_cash_cap=Decimal("200.00"),
            monthly_investment_rate=Decimal("0.00"),
            obligations={},
            installments={},
            payroll_extras={},
            commissions=(),
        )
    )[0]
    assert row["balance_no_commission"] == Decimal("0.00")
    assert row["uncovered_deficit_no_commission"] == Decimal("100.00")

    result = _evaluate(
        "INV-005",
        opening_liquidity_balance="100.00",
        monthly_operating_result="-200.00",
        liquidity_used=row["liquidity_used_no_commission"],
        closing_liquidity_balance=row["balance_no_commission"],
        uncovered_deficit=row["uncovered_deficit_no_commission"],
    )
    assert result.status is InvariantStatus.PASS


def test_projection_validator_divergence_is_blocking() -> None:
    result = _evaluate(
        "INV-018",
        financial_engine_values={"2026-09.balance_expected": Decimal("1.02")},
        projection_validator_values={"2026-09.balance_expected": Decimal("1.00")},
        monetary_tolerance=Decimal("0.01"),
    )
    assert result.status is InvariantStatus.FAIL
    assert result.severity.value == "block"
