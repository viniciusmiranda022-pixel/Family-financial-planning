from dataclasses import dataclass, field
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


def amortized_installment_payment(
    principal: Decimal, installments: int, monthly_rate: Decimal
) -> tuple[Decimal, Decimal]:
    """Canonical price/Gauss amortization: `(monthly_payment, total_cost)` for
    financing `principal` over `installments` equal monthly payments at
    `monthly_rate` (0 for interest-free financing).

    Single source of truth for this formula -- the Advisor chat's free-text
    purchase parser (`_advisor_payment`, `app/api.py`) and the purchase
    scenario comparison (`POST /purchases/scenario-comparison`) both call
    this exact function so a financed purchase's monthly payment can never be
    computed two different ways.

    Engineering review (`docs/WORK_ORDER_INSTALLMENT_REGRESSIONS_PR115.md`,
    item 3): interest-free financing costs nothing beyond the principal --
    `total_cost` for `monthly_rate == 0` is always exactly `principal`
    (`money(principal)`), never `monthly_payment * installments`. Splitting
    `principal` into `installments` equal cent-rounded payments can lose up
    to a few cents to rounding (`money(100 / 3) * 3 == 99.99`, not the
    stated `100.00`); reporting that reconstruction as `total_cost` would
    silently replace the user-stated contracted total with a lossy
    recomputation. `monthly_payment` (the *per-installment* figure) is
    unaffected and still carries whatever rounding is unavoidable for a
    single equal installment -- callers that need the exact contracted
    total preserved (not just each installment's individual payment) must
    keep it from its own source (e.g. `amount_text`), never re-derive it
    from `monthly_payment`.
    """
    if installments <= 0:
        return Decimal("0"), Decimal("0")
    if installments == 1 or monthly_rate == 0:
        monthly_payment = money(principal / installments)
        return monthly_payment, money(principal)
    factor = Decimal("1") - (Decimal("1") + monthly_rate) ** (-installments)
    monthly_payment = money(principal * monthly_rate / factor)
    return monthly_payment, money(monthly_payment * installments)


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
    # October Go-Live Slice 3 (P0 #87): committed `CardInvoice` balances
    # already closed/partially paid (rebaseline §6.2/§7) -- a real
    # COMPROMETIDO commitment distinct from `installments` (still-future,
    # not-yet-closed parcelamento) and from `obligations` (the `Obligation`
    # model). Kept as its own field, never merged into `obligations`, so a
    # consumer can always tell the two sources apart instead of only seeing
    # one combined number.
    card_invoices: dict[str, Decimal] = field(default_factory=dict)
    # October Go-Live Slice 3: period -> confirmed amount, used *instead of*
    # `monthly_salary` for that one period when
    # `app.services.recurring_income.reconcile_recurring_income` already
    # found a real income `Transaction` for it (rebaseline §8.4 -- "a
    # conciliação não pode criar uma segunda receita"). Never additive with
    # `monthly_salary`; an empty dict reproduces the previous flat-salary
    # behaviour exactly.
    monthly_salary_overrides: dict[str, Decimal] = field(default_factory=dict)


def build_forecast(data: ForecastInput) -> list[dict[str, Decimal | str]]:
    from app.services.projection_engine import build_projection

    return build_projection(data)
