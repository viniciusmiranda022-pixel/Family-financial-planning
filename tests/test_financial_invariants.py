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
    assert FINANCIAL_RULES_VERSION == "2026.10.6"
    # October Go-Live Slice 3 (P0 #87): INV-029/INV-030 join the registry,
    # guarding the forecast against a received Commission or a reconciled
    # recurring income being counted twice. Round 2 of the PR #91 review adds
    # INV-031, guarding the broader rebaseline §8.3 rule that no unreceived
    # commission may ever inflate the projection in the first place
    # (docs/FINANCIAL_INVARIANTS.md). October Go-Live Slice 4 adds INV-032/
    # INV-033, guarding the Assistente Financeiro's audit/undo trail. October
    # Go-Live Slice 6 adds INV-034, guarding that an investment/asset's net
    # worth contribution is exactly its current value. Issue #85 adds
    # INV-035, guarding that a CVM-quota-derived fund valuation never
    # contributes to income, expense, budget usage, operating result or net
    # worth.
    assert tuple(INVARIANT_REGISTRY) == tuple(f"INV-{number:03d}" for number in range(1, 36))
    assert len({item.name for item in INVARIANT_REGISTRY.values()}) == 35
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


def test_net_worth_uses_current_value_only_pass_and_fail() -> None:
    # Rebaseline §16.2: the Studio's net worth contribution must equal its
    # current value exactly -- historical cost and future projection never
    # enter the total.
    correct = _evaluate(
        "INV-034",
        historical_cost=Decimal("30000.00"),
        current_value=Decimal("35000.00"),
        expected_receivable_value=Decimal("45000.00"),
        net_worth_contribution=Decimal("35000.00"),
    )
    assert correct.status is InvariantStatus.PASS
    assert correct.difference == Decimal("0.00")

    double_counted = _evaluate(
        "INV-034",
        historical_cost=Decimal("30000.00"),
        current_value=Decimal("35000.00"),
        expected_receivable_value=Decimal("45000.00"),
        # A bug that summed historical_cost + current_value into net worth.
        net_worth_contribution=Decimal("65000.00"),
    )
    assert double_counted.status is InvariantStatus.FAIL
    assert double_counted.difference == Decimal("30000.00")

    projection_inflated = _evaluate(
        "INV-034",
        historical_cost=Decimal("30000.00"),
        current_value=Decimal("35000.00"),
        expected_receivable_value=Decimal("45000.00"),
        # A bug that reported the future projection as if it were patrimony.
        net_worth_contribution=Decimal("45000.00"),
    )
    assert projection_inflated.status is InvariantStatus.FAIL
    assert projection_inflated.difference == Decimal("10000.00")

    # No projection yet (a brand-new asset): the fact producer sanitizes
    # `expected_receivable_value` to zero rather than passing `None` through
    # -- required_facts must always be a concrete decimal, never a raw
    # nullable domain value (`app.services.financial_invariants._decimal`
    # treats `None` as a missing fact, by design).
    no_projection = _evaluate(
        "INV-034",
        historical_cost=Decimal("30000.00"),
        current_value=Decimal("35000.00"),
        expected_receivable_value=Decimal("0"),
        net_worth_contribution=Decimal("35000.00"),
    )
    assert no_projection.status is InvariantStatus.PASS


def test_fund_valuation_is_not_cash_flow_pass_and_fail() -> None:
    # Issue #85: a CVM-quota-derived fund valuation must never touch
    # income, expense, budget usage, operating result or net worth.
    valid = _evaluate("INV-035", **ZERO_OPERATING_EFFECTS, net_worth_effect=Decimal("0"))
    assert valid.status is InvariantStatus.PASS
    assert valid.difference == Decimal("0.00")

    counted_as_income = _evaluate(
        "INV-035",
        **{**ZERO_OPERATING_EFFECTS, "operating_income_effect": Decimal("110.61")},
        net_worth_effect=Decimal("0"),
    )
    assert counted_as_income.status is InvariantStatus.FAIL
    assert counted_as_income.difference == Decimal("110.61")

    leaked_into_net_worth = _evaluate(
        "INV-035",
        **ZERO_OPERATING_EFFECTS,
        net_worth_effect=Decimal("40641.87"),
    )
    assert leaked_into_net_worth.status is InvariantStatus.FAIL
    assert leaked_into_net_worth.difference == Decimal("40641.87")


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


def test_card_purchase_competence_matches_real_engine_open_invoice_hotfix() -> None:
    """Regression for docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md:
    unlike `test_card_purchase_uses_statement_competence` above (which only
    proves the abstract evaluator compares two given facts correctly), this
    proves INV-017 actually holds for the *real* canonical engine
    (`app.api._card_invoice_competence`) against the exact bug scenario --
    a card purchase made while the next invoice is still open. Before the
    hotfix this fact would have been `2026-09` (`booked_at`'s own month);
    the canonical engine must now report the still-open October invoice,
    and feeding that into the same INV-017 evaluator this suite already
    trusts must come back `pass`, never `fail` or `unknown`."""
    from datetime import date as _date

    from app.api import _card_invoice_competence
    from app.models import Account

    card = Account(
        account_type="credit_card",
        name="Cartão com fatura aberta",
        card_closing_day=2,
        card_due_day=9,
    )
    booked_at = _date(2026, 9, 15)
    canonical_competence = _card_invoice_competence(card, booked_at)
    assert canonical_competence == "2026-10"

    matching = _evaluate(
        "INV-017",
        canonical_competence=canonical_competence,
        counted_competence=canonical_competence,
    )
    assert matching.status is InvariantStatus.PASS

    # The pre-hotfix bug's actual behaviour (counting under `booked_at`'s
    # own month) must still be caught as a violation by the same rule.
    pre_hotfix_bug = _evaluate(
        "INV-017",
        canonical_competence=canonical_competence,
        counted_competence=booked_at.strftime("%Y-%m"),
    )
    assert pre_hotfix_bug.status is InvariantStatus.FAIL


def test_card_purchase_without_configured_cycle_never_fabricates_a_competence() -> None:
    """The canonical engine must fail closed (never guess) when the account
    has no persisted `card_closing_day`/`card_due_day` -- INV-017 protects
    against a *wrong* competence, and fail-closed is how the engine avoids
    ever producing one to check in the first place."""
    from datetime import date as _date

    from fastapi import HTTPException

    from app.api import _card_invoice_competence
    from app.models import Account

    card = Account(account_type="credit_card", name="Cartão sem ciclo")
    with pytest.raises(HTTPException) as exc_info:
        _card_invoice_competence(card, _date(2026, 9, 15))
    assert exc_info.value.status_code == 422


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


# ---------------------------------------------------------------------------
# October Go-Live Slice 2 (P0 #87): INV-025..INV-028, the `CardInvoice`
# lifecycle's own contribution to the Financial Integrity Engine
# (`docs/FINANCIAL_INVARIANTS.md`, `app/services/card_invoice_lifecycle.py`).
# ---------------------------------------------------------------------------


def test_card_invoice_payment_never_duplicates_expense() -> None:
    valid = _evaluate("INV-025", expense_count_before=3, expense_count_after=3)
    regressed = _evaluate("INV-025", expense_count_before=3, expense_count_after=4)
    assert valid.status is InvariantStatus.PASS
    assert regressed.status is InvariantStatus.FAIL
    assert regressed.difference == Decimal("1")
    assert regressed.severity.value == "block"


def test_card_invoice_principal_carry_never_becomes_new_expense() -> None:
    valid = _evaluate(
        "INV-026", principal_carried_in=Decimal("600.00"), new_expense_transactions_for_carry=0
    )
    fabricated = _evaluate(
        "INV-026", principal_carried_in=Decimal("600.00"), new_expense_transactions_for_carry=1
    )
    negative = _evaluate(
        "INV-026", principal_carried_in=Decimal("-1.00"), new_expense_transactions_for_carry=0
    )
    assert valid.status is InvariantStatus.PASS
    assert fabricated.status is InvariantStatus.FAIL
    assert negative.status is InvariantStatus.FAIL


def test_card_invoice_refund_preserves_history() -> None:
    valid = _evaluate(
        "INV-027",
        original_transaction_exists=True,
        linked_refund_total=Decimal("200.00"),
        original_amount=Decimal("200.00"),
        original_invoice_already_paid=True,
        original_invoice_state_unchanged=True,
    )
    deleted = _evaluate(
        "INV-027",
        original_transaction_exists=False,
        linked_refund_total=Decimal("200.00"),
        original_amount=Decimal("200.00"),
        original_invoice_already_paid=False,
        original_invoice_state_unchanged=True,
    )
    overshoot = _evaluate(
        "INV-027",
        original_transaction_exists=True,
        linked_refund_total=Decimal("250.00"),
        original_amount=Decimal("200.00"),
        original_invoice_already_paid=False,
        original_invoice_state_unchanged=True,
    )
    rewritten = _evaluate(
        "INV-027",
        original_transaction_exists=True,
        linked_refund_total=Decimal("200.00"),
        original_amount=Decimal("200.00"),
        original_invoice_already_paid=True,
        original_invoice_state_unchanged=False,
    )
    assert valid.status is InvariantStatus.PASS
    assert deleted.status is InvariantStatus.FAIL
    assert overshoot.status is InvariantStatus.FAIL
    assert overshoot.difference == Decimal("50.00")
    assert rewritten.status is InvariantStatus.FAIL


def test_card_invoice_divergence_never_silently_adjusted() -> None:
    for status in ("reconciled", "unreconciled_explained", "unreconciled_unexplained", "not_applicable"):
        result = _evaluate("INV-028", divergence_status=status, synthetic_adjustment_created=False)
        assert result.status is InvariantStatus.PASS
    invalid_status = _evaluate(
        "INV-028", divergence_status="auto_adjusted", synthetic_adjustment_created=False
    )
    synthetic = _evaluate(
        "INV-028", divergence_status="reconciled", synthetic_adjustment_created=True
    )
    assert invalid_status.status is InvariantStatus.FAIL
    assert synthetic.status is InvariantStatus.FAIL


def test_received_commission_excluded_from_projection_pass_and_fail() -> None:
    clean = _evaluate(
        "INV-029",
        received_commission_ids=["c1", "c2"],
        projected_commission_ids=["c3", "c4"],
    )
    empty = _evaluate("INV-029", received_commission_ids=[], projected_commission_ids=[])
    leaked = _evaluate(
        "INV-029",
        received_commission_ids=["c1", "c2"],
        projected_commission_ids=["c2", "c3"],
    )
    assert clean.status is InvariantStatus.PASS
    assert empty.status is InvariantStatus.PASS
    assert leaked.status is InvariantStatus.FAIL
    assert leaked.actual["received_commissions_in_projection"] == ["c2"]
    assert leaked.severity.value == "critical"


def test_recurring_income_no_duplicate_pass_and_fail() -> None:
    no_evidence_uses_estimate = _evaluate(
        "INV-030",
        has_recurring_income_evidence=False,
        expected_amount=Decimal("5000.00"),
        reconciled_amount=Decimal("0"),
        projected_amount=Decimal("5000.00"),
    )
    evidence_uses_real_amount = _evaluate(
        "INV-030",
        has_recurring_income_evidence=True,
        expected_amount=Decimal("5000.00"),
        reconciled_amount=Decimal("5000.01"),
        projected_amount=Decimal("5000.01"),
    )
    double_counted = _evaluate(
        "INV-030",
        has_recurring_income_evidence=True,
        expected_amount=Decimal("5000.00"),
        reconciled_amount=Decimal("5000.01"),
        projected_amount=Decimal("10000.01"),
    )
    stale_estimate_kept_despite_evidence = _evaluate(
        "INV-030",
        has_recurring_income_evidence=True,
        expected_amount=Decimal("5000.00"),
        reconciled_amount=Decimal("5000.01"),
        projected_amount=Decimal("5000.00"),
    )
    assert no_evidence_uses_estimate.status is InvariantStatus.PASS
    assert evidence_uses_real_amount.status is InvariantStatus.PASS
    assert double_counted.status is InvariantStatus.FAIL
    assert double_counted.difference == Decimal("5000.00")
    assert stale_estimate_kept_despite_evidence.status is InvariantStatus.FAIL


def test_no_automatic_unreceived_commission_in_projection_pass_and_fail() -> None:
    clean_no_pending = _evaluate(
        "INV-031",
        total_projected_commission=Decimal("0.00"),
        unreceived_commission_count=0,
    )
    clean_with_pending = _evaluate(
        "INV-031",
        total_projected_commission=Decimal("0.00"),
        unreceived_commission_count=3,
    )
    leaked = _evaluate(
        "INV-031",
        total_projected_commission=Decimal("10000.00"),
        unreceived_commission_count=1,
    )
    assert clean_no_pending.status is InvariantStatus.PASS
    assert clean_with_pending.status is InvariantStatus.PASS
    assert leaked.status is InvariantStatus.FAIL
    assert leaked.difference == Decimal("10000.00")
    assert leaked.severity.value == "critical"


def test_assistant_action_always_audited_pass_and_fail() -> None:
    complete = _evaluate(
        "INV-032",
        has_audit_event=True,
        has_action_event=True,
        original_message_present=True,
    )
    missing_message = _evaluate(
        "INV-032",
        has_audit_event=True,
        has_action_event=True,
        original_message_present=False,
    )
    missing_audit = _evaluate(
        "INV-032",
        has_audit_event=False,
        has_action_event=True,
        original_message_present=True,
    )
    assert complete.status is InvariantStatus.PASS
    assert missing_message.status is InvariantStatus.FAIL
    assert missing_audit.status is InvariantStatus.FAIL
    assert complete.severity.value == "block"


def test_assistant_undo_preserves_history_pass_and_fail() -> None:
    complete = _evaluate(
        "INV-033",
        original_action_preserved=True,
        undo_audit_event_created=True,
        undone_at_recorded=True,
    )
    deleted_original = _evaluate(
        "INV-033",
        original_action_preserved=False,
        undo_audit_event_created=True,
        undone_at_recorded=True,
    )
    no_undo_audit = _evaluate(
        "INV-033",
        original_action_preserved=True,
        undo_audit_event_created=False,
        undone_at_recorded=True,
    )
    assert complete.status is InvariantStatus.PASS
    assert deleted_original.status is InvariantStatus.FAIL
    assert no_undo_audit.status is InvariantStatus.FAIL
    assert complete.severity.value == "block"
