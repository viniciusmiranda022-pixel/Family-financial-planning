from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def commission_net(gross: Decimal, tax_rate: Decimal) -> tuple[Decimal, Decimal]:
    tax = money(gross * tax_rate)
    return tax, money(gross - tax)


def monthly_net_rate(gross_annual_rate: Decimal, income_tax_rate: Decimal) -> Decimal:
    if gross_annual_rate <= 0:
        return Decimal("0")
    gross_monthly = (Decimal("1") + gross_annual_rate) ** (Decimal("1") / Decimal("12")) - Decimal("1")
    return gross_monthly * (Decimal("1") - income_tax_rate)


def add_months(value: date, months: int) -> date:
    target = value.month - 1 + months
    year = value.year + target // 12
    month = target % 12 + 1
    return date(year, month, 1)


def month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


@dataclass(frozen=True)
class ForecastCommission:
    expected_date: date
    net_amount: Decimal
    delay_days: int


@dataclass(frozen=True)
class ForecastInput:
    start_month: date
    end_month: date
    starting_balance: Decimal
    monthly_salary: Decimal
    monthly_cash_cap: Decimal
    monthly_investment_rate: Decimal
    obligations: dict[str, Decimal]
    installments: dict[str, Decimal]
    payroll_extras: dict[str, Decimal]
    commissions: tuple[ForecastCommission, ...]


def _delayed_month(expected: date, delay_days: int) -> date:
    month_shift = max(0, (delay_days + 29) // 30)
    return add_months(date(expected.year, expected.month, 1), month_shift)


def build_forecast(data: ForecastInput) -> list[dict[str, Decimal | str]]:
    expected_commissions: dict[str, Decimal] = {}
    delayed_commissions: dict[str, Decimal] = {}
    for item in data.commissions:
        expected_key = month_key(item.expected_date)
        delayed_key = month_key(_delayed_month(item.expected_date, item.delay_days))
        expected_commissions[expected_key] = (
            expected_commissions.get(expected_key, Decimal("0")) + item.net_amount
        )
        delayed_commissions[delayed_key] = (
            delayed_commissions.get(delayed_key, Decimal("0")) + item.net_amount
        )

    balances = {
        "no_commission": money(data.starting_balance),
        "delayed": money(data.starting_balance),
        "expected": money(data.starting_balance),
    }
    rows: list[dict[str, Decimal | str]] = []
    cursor = date(data.start_month.year, data.start_month.month, 1)
    end = date(data.end_month.year, data.end_month.month, 1)
    while cursor <= end:
        key = month_key(cursor)
        salary = money(data.monthly_salary)
        extras = money(data.payroll_extras.get(key, Decimal("0")))
        obligations = money(data.obligations.get(key, Decimal("0")))
        installments = money(data.installments.get(key, Decimal("0")))
        spend = money(data.monthly_cash_cap)
        expected_commission = money(expected_commissions.get(key, Decimal("0")))
        delayed_commission = money(delayed_commissions.get(key, Decimal("0")))
        returns: dict[str, Decimal] = {}
        for scenario, commission in (
            ("no_commission", Decimal("0")),
            ("delayed", delayed_commission),
            ("expected", expected_commission),
        ):
            opening = balances[scenario]
            investment_return = money(max(Decimal("0"), opening) * data.monthly_investment_rate)
            balances[scenario] = money(
                opening
                + investment_return
                + salary
                + extras
                + commission
                - obligations
                - installments
                - spend
            )
            returns[scenario] = investment_return
        rows.append(
            {
                "month": key,
                "salary": salary,
                "payroll_extras": extras,
                "commission_expected": expected_commission,
                "commission_delayed": delayed_commission,
                "obligations": obligations,
                "installments": installments,
                "cash_cap": spend,
                "investment_return_delayed": returns["delayed"],
                "balance_no_commission": balances["no_commission"],
                "balance_delayed": balances["delayed"],
                "balance_expected": balances["expected"],
            }
        )
        cursor = add_months(cursor, 1)
    return rows
