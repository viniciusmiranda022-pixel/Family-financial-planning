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
    starting_uncovered_deficit: Decimal = Decimal("0")
    safety_floor: Decimal = Decimal("0")


def build_forecast(data: ForecastInput) -> list[dict[str, Decimal | str]]:
    from app.services.projection_engine import build_projection

    return build_projection(data)
