"""Independent algebraic validator for the canonical projection engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.finance import ForecastInput, add_months, money, month_key

PROJECTION_TOLERANCE = Decimal("0.01")
SCENARIOS = ("no_commission", "delayed", "expected")


@dataclass(frozen=True, slots=True)
class ProjectionMismatch:
    key: str
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None


@dataclass(frozen=True, slots=True)
class ProjectionValidation:
    valid: bool
    expected_values: dict[str, Decimal]
    actual_values: dict[str, Decimal]
    mismatches: tuple[ProjectionMismatch, ...]


def _commission_maps(data: ForecastInput) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    expected: dict[str, Decimal] = {}
    delayed: dict[str, Decimal] = {}
    for item in data.commissions:
        expected_period = month_key(item.expected_date)
        delay_months = max(0, (item.delay_days + 29) // 30)
        delayed_period = month_key(add_months(item.expected_date.replace(day=1), delay_months))
        expected[expected_period] = money(expected.get(expected_period, 0) + item.net_amount)
        delayed[delayed_period] = money(delayed.get(delayed_period, 0) + item.net_amount)
    return expected, delayed


def independently_calculate(data: ForecastInput) -> dict[str, Decimal]:
    expected_commissions, delayed_commissions = _commission_maps(data)
    balance = {key: money(max(Decimal("0"), data.starting_balance)) for key in SCENARIOS}
    debt = {
        key: money(max(Decimal("0"), data.starting_uncovered_deficit)) for key in SCENARIOS
    }
    values: dict[str, Decimal] = {}
    cursor = data.start_month.replace(day=1)
    while cursor <= data.end_month.replace(day=1):
        period = month_key(cursor)
        salary = money(data.monthly_salary)
        extras = money(data.payroll_extras.get(period, 0))
        obligations = money(data.obligations.get(period, 0))
        installments = money(data.installments.get(period, 0))
        cash_cap = money(data.monthly_cash_cap)
        expected = money(expected_commissions.get(period, 0))
        delayed = money(delayed_commissions.get(period, 0))
        common = {
            "salary": salary,
            "payroll_extras": extras,
            "commission_expected": expected,
            "commission_delayed": delayed,
            "obligations": obligations,
            "installments": installments,
            "cash_cap": cash_cap,
        }
        values.update({f"{period}.{key}": value for key, value in common.items()})
        for scenario, commission in (
            ("no_commission", Decimal("0")),
            ("delayed", delayed),
            ("expected", expected),
        ):
            opening = balance[scenario] if debt[scenario] == 0 else Decimal("0.00")
            opening_debt = debt[scenario]
            investment_return = money(
                opening * data.monthly_investment_rate if opening_debt == 0 else 0
            )
            income = money(salary + extras + commission)
            expenses = money(obligations + installments + cash_cap)
            result = money(income - expenses)
            available_result = money(result + investment_return)
            used = Decimal("0.00")
            if opening_debt > 0:
                if available_result >= 0:
                    debt_payment = min(opening_debt, available_result)
                    closing_debt = money(opening_debt - debt_payment)
                    closing = (
                        money(available_result - debt_payment)
                        if closing_debt == 0
                        else Decimal("0.00")
                    )
                else:
                    closing_debt = money(opening_debt + abs(available_result))
                    closing = Decimal("0.00")
            elif available_result >= 0:
                closing = money(opening + available_result)
                closing_debt = Decimal("0.00")
            else:
                used = money(min(opening, abs(available_result)))
                closing = money(opening - used)
                closing_debt = money(abs(available_result) - used)
            balance[scenario] = closing
            debt[scenario] = closing_debt
            metrics = {
                "opening_balance": opening,
                "opening_uncovered_deficit": opening_debt,
                "commission": money(commission),
                "operating_income": income,
                "operating_expenses": expenses,
                "investment_return": investment_return,
                "result": result,
                "liquidity_used": used,
                "balance": closing,
                "uncovered_deficit": closing_debt,
                "distance_to_floor": money(closing - data.safety_floor),
            }
            values.update(
                {f"{period}.{key}_{scenario}": value for key, value in metrics.items()}
            )
        cursor = add_months(cursor, 1)
    return values


def validate_projection(
    data: ForecastInput,
    rows: list[dict[str, Decimal | str]],
    *,
    tolerance: Decimal = PROJECTION_TOLERANCE,
) -> ProjectionValidation:
    expected = independently_calculate(data)
    actual = {
        f"{row['month']}.{key}": value
        for row in rows
        for key, value in row.items()
        if key != "month" and isinstance(value, Decimal)
    }
    mismatches: list[ProjectionMismatch] = []
    for key in sorted(set(expected) | set(actual)):
        expected_value = expected.get(key)
        actual_value = actual.get(key)
        difference = (
            money(actual_value - expected_value)
            if expected_value is not None and actual_value is not None
            else None
        )
        if difference is None or abs(difference) > tolerance:
            mismatches.append(
                ProjectionMismatch(key, expected_value, actual_value, difference)
            )
    return ProjectionValidation(not mismatches, expected, actual, tuple(mismatches))
