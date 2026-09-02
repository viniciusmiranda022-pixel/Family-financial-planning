"""Canonical three-scenario projection engine."""

from __future__ import annotations

from decimal import Decimal

from app.services.finance import ForecastInput, add_months, money, month_key
from app.services.financial_engine import settle_liquidity

PROJECTION_CALCULATION_VERSION = "2026.09.2"
SCENARIOS = ("no_commission", "delayed", "expected")


def _commission_maps(data: ForecastInput) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    expected: dict[str, Decimal] = {}
    delayed: dict[str, Decimal] = {}
    for item in data.commissions:
        expected_key = month_key(item.expected_date)
        shift = max(0, (item.delay_days + 29) // 30)
        delayed_key = month_key(add_months(item.expected_date.replace(day=1), shift))
        expected[expected_key] = expected.get(expected_key, Decimal("0")) + item.net_amount
        delayed[delayed_key] = delayed.get(delayed_key, Decimal("0")) + item.net_amount
    return expected, delayed


def build_projection(data: ForecastInput) -> list[dict[str, Decimal | str]]:
    expected_commissions, delayed_commissions = _commission_maps(data)
    balances = {scenario: money(max(Decimal("0"), data.starting_balance)) for scenario in SCENARIOS}
    debts = {
        scenario: money(max(Decimal("0"), data.starting_uncovered_deficit))
        for scenario in SCENARIOS
    }
    rows: list[dict[str, Decimal | str]] = []
    cursor = data.start_month.replace(day=1)
    end = data.end_month.replace(day=1)
    while cursor <= end:
        period = month_key(cursor)
        salary = money(data.monthly_salary)
        extras = money(data.payroll_extras.get(period, Decimal("0")))
        obligations = money(data.obligations.get(period, Decimal("0")))
        installments = money(data.installments.get(period, Decimal("0")))
        cash_cap = money(data.monthly_cash_cap)
        expected_commission = money(expected_commissions.get(period, Decimal("0")))
        delayed_commission = money(delayed_commissions.get(period, Decimal("0")))
        row: dict[str, Decimal | str] = {
            "month": period,
            "salary": salary,
            "payroll_extras": extras,
            "commission_expected": expected_commission,
            "commission_delayed": delayed_commission,
            "obligations": obligations,
            "installments": installments,
            "cash_cap": cash_cap,
        }
        for scenario, commission in (
            ("no_commission", Decimal("0")),
            ("delayed", delayed_commission),
            ("expected", expected_commission),
        ):
            opening = balances[scenario]
            opening_debt = debts[scenario]
            investment_return = money(
                opening * data.monthly_investment_rate if opening_debt == 0 else 0
            )
            income = money(salary + extras + commission)
            expenses = money(obligations + installments + cash_cap)
            result = money(income - expenses)
            transition = settle_liquidity(
                opening_balance=opening,
                opening_uncovered_deficit=opening_debt,
                operating_result=result,
                investment_yield=investment_return,
                safety_floor=data.safety_floor,
            )
            balances[scenario] = transition.closing_balance
            debts[scenario] = transition.closing_uncovered_deficit
            row.update(
                {
                    f"opening_balance_{scenario}": transition.opening_balance,
                    f"opening_uncovered_deficit_{scenario}": transition.opening_uncovered_deficit,
                    f"commission_{scenario}": money(commission),
                    f"operating_income_{scenario}": income,
                    f"operating_expenses_{scenario}": expenses,
                    f"investment_return_{scenario}": investment_return,
                    f"result_{scenario}": result,
                    f"liquidity_used_{scenario}": transition.liquidity_used,
                    f"balance_{scenario}": transition.closing_balance,
                    f"uncovered_deficit_{scenario}": transition.closing_uncovered_deficit,
                    f"distance_to_floor_{scenario}": transition.distance_to_floor,
                }
            )
        rows.append(row)
        cursor = add_months(cursor, 1)
    return rows


def flatten_projection(rows: list[dict[str, Decimal | str]]) -> dict[str, Decimal]:
    return {
        f"{row['month']}.{key}": value
        for row in rows
        for key, value in row.items()
        if key != "month" and isinstance(value, Decimal)
    }
