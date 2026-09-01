from decimal import Decimal

from app.services.financial_engine import FinancialEngineInput, calculate_actual_snapshot, settle_liquidity


def test_privilege_deficit_is_limited_by_available_balance() -> None:
    transition = settle_liquidity(
        opening_balance=Decimal("87068.54"),
        operating_result=Decimal("-270014.03"),
        safety_floor=Decimal("10000"),
    )

    assert transition.liquidity_used == Decimal("87068.54")
    assert transition.closing_balance == Decimal("0.00")
    assert transition.closing_uncovered_deficit == Decimal("182945.49")
    assert transition.distance_to_floor == Decimal("-10000.00")


def test_future_surplus_pays_uncovered_deficit_before_rebuilding_liquidity() -> None:
    still_in_debt = settle_liquidity(
        opening_balance=Decimal("999"),
        opening_uncovered_deficit=Decimal("1000"),
        operating_result=Decimal("600"),
    )
    recovered = settle_liquidity(
        opening_balance=Decimal("0"),
        opening_uncovered_deficit=Decimal("1000"),
        operating_result=Decimal("1200"),
    )

    assert still_in_debt.opening_balance == Decimal("0.00")
    assert still_in_debt.closing_balance == Decimal("0.00")
    assert still_in_debt.closing_uncovered_deficit == Decimal("400.00")
    assert recovered.closing_uncovered_deficit == Decimal("0.00")
    assert recovered.closing_balance == Decimal("200.00")


def test_snapshot_checksum_is_deterministic_and_money_is_rounded() -> None:
    data = FinancialEngineInput(
        period="2026-08",
        operating_income=Decimal("5000.004"),
        operating_expenses=Decimal("1200.005"),
        opening_liquidity_balance=Decimal("10000"),
        budget_cap=Decimal("3000"),
        source_count=2,
    )

    first = calculate_actual_snapshot(data)
    second = calculate_actual_snapshot(data)

    assert first.operating_income == Decimal("5000.00")
    assert first.operating_expenses == Decimal("1200.01")
    assert first.closing_liquidity_balance == Decimal("13799.99")
    assert first.checksum() == second.checksum()
