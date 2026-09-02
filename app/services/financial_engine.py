"""Pure, versioned financial calculations used by every presentation channel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.financial_invariants import FINANCIAL_RULES_VERSION

CALCULATION_VERSION = "2026.09.1"


@dataclass(frozen=True, slots=True)
class LiquidityTransition:
    opening_balance: Decimal
    opening_uncovered_deficit: Decimal
    investment_yield: Decimal
    operating_result: Decimal
    deposit: Decimal
    liquidity_used: Decimal
    closing_balance: Decimal
    closing_uncovered_deficit: Decimal
    safety_floor: Decimal
    distance_to_floor: Decimal
    floor_breached: bool


@dataclass(frozen=True, slots=True)
class FinancialEngineInput:
    period: str
    operating_income: Decimal = Decimal("0")
    operating_expenses: Decimal = Decimal("0")
    bank_cash_in: Decimal = Decimal("0")
    bank_cash_out: Decimal = Decimal("0")
    investments: Decimal = Decimal("0")
    redemptions: Decimal = Decimal("0")
    internal_transfers: Decimal = Decimal("0")
    card_spend: Decimal = Decimal("0")
    card_payments: Decimal = Decimal("0")
    refunds: Decimal = Decimal("0")
    opening_liquidity_balance: Decimal = Decimal("0")
    investment_yield: Decimal = Decimal("0")
    opening_uncovered_deficit: Decimal = Decimal("0")
    safety_floor: Decimal = Decimal("0")
    budget_cap: Decimal = Decimal("0")
    commitments: Decimal = Decimal("0")
    source_count: int = 0


@dataclass(frozen=True, slots=True)
class FinancialEngineResult:
    period: str
    operating_income: Decimal
    operating_expenses: Decimal
    operating_result: Decimal
    bank_cash_in: Decimal
    bank_cash_out: Decimal
    bank_cash_result: Decimal
    investments: Decimal
    redemptions: Decimal
    internal_transfers: Decimal
    card_spend: Decimal
    card_payments: Decimal
    refunds: Decimal
    opening_liquidity_balance: Decimal
    investment_yield: Decimal
    liquidity_used: Decimal
    liquidity_deposit: Decimal
    closing_liquidity_balance: Decimal
    opening_uncovered_deficit: Decimal
    closing_uncovered_deficit: Decimal
    safety_floor: Decimal
    distance_to_floor: Decimal
    floor_breached: bool
    budget_cap: Decimal
    budget_usage: Decimal
    budget_remaining: Decimal
    commitments: Decimal
    projected_balance: Decimal
    source_count: int
    calculation_version: str = CALCULATION_VERSION
    financial_rules_version: str = FINANCIAL_RULES_VERSION

    def canonical_payload(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def checksum(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def settle_liquidity(
    *,
    opening_balance: Decimal,
    operating_result: Decimal,
    investment_yield: Decimal = Decimal("0"),
    opening_uncovered_deficit: Decimal = Decimal("0"),
    safety_floor: Decimal = Decimal("0"),
) -> LiquidityTransition:
    """Apply the canonical Privilège state transition without protecting the floor.

    Prior uncovered debt is paid before a future surplus can rebuild liquidity.
    The result can never expose a negative balance or simultaneous positive
    liquidity and uncovered debt.
    """

    opening = _money(max(Decimal("0"), opening_balance))
    debt = _money(max(Decimal("0"), opening_uncovered_deficit))
    yield_amount = _money(investment_yield)
    result = _money(operating_result + yield_amount)
    floor = _money(max(Decimal("0"), safety_floor))

    if debt > 0:
        # A prior uncovered deficit implies liquidity was already exhausted.
        opening = Decimal("0.00")
        if result >= 0:
            debt_payment = min(debt, result)
            closing_debt = _money(debt - debt_payment)
            deposit = _money(result - debt_payment)
            closing = deposit if closing_debt == 0 else Decimal("0.00")
        else:
            deposit = Decimal("0.00")
            closing = Decimal("0.00")
            closing_debt = _money(debt + abs(result))
        used = Decimal("0.00")
    elif result >= 0:
        deposit = result
        used = Decimal("0.00")
        closing = _money(opening + deposit)
        closing_debt = Decimal("0.00")
    else:
        deficit = abs(result)
        used = _money(min(opening, deficit))
        deposit = Decimal("0.00")
        closing = _money(opening - used)
        closing_debt = _money(deficit - used)

    distance = _money(closing - floor)
    return LiquidityTransition(
        opening_balance=opening,
        opening_uncovered_deficit=debt,
        investment_yield=yield_amount,
        operating_result=_money(operating_result),
        deposit=deposit,
        liquidity_used=used,
        closing_balance=closing,
        closing_uncovered_deficit=closing_debt,
        safety_floor=floor,
        distance_to_floor=distance,
        floor_breached=closing < floor,
    )


def calculate_actual_snapshot(data: FinancialEngineInput) -> FinancialEngineResult:
    income = _money(max(Decimal("0"), data.operating_income))
    expenses = _money(max(Decimal("0"), data.operating_expenses))
    operating_result = _money(income - expenses)
    liquidity = settle_liquidity(
        opening_balance=data.opening_liquidity_balance,
        operating_result=operating_result,
        investment_yield=data.investment_yield,
        opening_uncovered_deficit=data.opening_uncovered_deficit,
        safety_floor=data.safety_floor,
    )
    budget_cap = _money(max(Decimal("0"), data.budget_cap))
    return FinancialEngineResult(
        period=data.period,
        operating_income=income,
        operating_expenses=expenses,
        operating_result=operating_result,
        bank_cash_in=_money(data.bank_cash_in),
        bank_cash_out=_money(data.bank_cash_out),
        bank_cash_result=_money(data.bank_cash_in - data.bank_cash_out),
        investments=_money(max(Decimal("0"), data.investments)),
        redemptions=_money(max(Decimal("0"), data.redemptions)),
        internal_transfers=_money(max(Decimal("0"), data.internal_transfers)),
        card_spend=_money(max(Decimal("0"), data.card_spend)),
        card_payments=_money(max(Decimal("0"), data.card_payments)),
        refunds=_money(max(Decimal("0"), data.refunds)),
        opening_liquidity_balance=liquidity.opening_balance,
        investment_yield=liquidity.investment_yield,
        liquidity_used=liquidity.liquidity_used,
        liquidity_deposit=liquidity.deposit,
        closing_liquidity_balance=liquidity.closing_balance,
        opening_uncovered_deficit=liquidity.opening_uncovered_deficit,
        closing_uncovered_deficit=liquidity.closing_uncovered_deficit,
        safety_floor=liquidity.safety_floor,
        distance_to_floor=liquidity.distance_to_floor,
        floor_breached=liquidity.floor_breached,
        budget_cap=budget_cap,
        budget_usage=expenses,
        budget_remaining=_money(budget_cap - expenses),
        commitments=_money(max(Decimal("0"), data.commitments)),
        projected_balance=liquidity.closing_balance,
        source_count=max(0, data.source_count),
    )


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value
