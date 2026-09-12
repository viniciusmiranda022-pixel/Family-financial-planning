"""Pure, versioned financial calculations used by every presentation channel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.financial_invariants import FINANCIAL_RULES_VERSION

# October Go-Live Slice 1 (P0 #87): `calculate_actual_snapshot` no longer
# derives Privilège liquidity movement from the sign of `operating_result`
# (see `reconcile_actual_liquidity` below). A realized snapshot built under
# the previous formula is never rewritten -- it keeps its own
# `calculation_version` -- but any snapshot built from this version forward
# uses the evidence-based reconciliation exclusively.
CALCULATION_VERSION = "2026.10.1"


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
class LiquidityReconciliation:
    """Evidence-based Conta Corrente <-> Privilège DI closing for a REALIZADO period.

    Unlike `LiquidityTransition` (projection/hypothetical only), this never
    infers a deposit or withdrawal from the sign of the operating result. It
    only recognizes liquidity movement backed by a real, already-categorized
    "Transferência patrimonial" transaction -- itself created either from an
    imported/observed bank movement or from a user/Assistant-confirmed action
    (e.g. `funding_source="privilege"`) -- never from `AccountBalanceObservation`
    alone and never fabricated from `operating_result`.
    """

    opening_balance: Decimal
    opening_uncovered_deficit: Decimal
    evidenced_deposit: Decimal
    evidenced_used: Decimal
    investment_yield: Decimal
    operating_result: Decimal
    liquidity_deposit: Decimal
    liquidity_used: Decimal
    closing_balance: Decimal
    closing_uncovered_deficit: Decimal
    has_transfer_evidence: bool
    unexplained_operating_result: Decimal


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
    has_transfer_evidence: bool = False
    unexplained_operating_result: Decimal = Decimal("0.00")
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
    """Hypothetical Privilège state transition used ONLY by the projection engine.

    October Go-Live Rebaseline §4.3 forbids inferring a real (REALIZADO)
    Privilège withdrawal/deposit from the sign of an operating result. This
    formula still does exactly that -- deliberately -- but it is now reserved
    for `app.services.projection_engine.build_projection`'s 30/60/90-day
    hypothetical scenarios (`no_commission`/`delayed`/`expected`), where
    modeling "what would happen if the household kept spending/earning at
    this pace" is the whole point. A REALIZADO/closed period must use
    `reconcile_actual_liquidity` instead, which never derives movement from
    `operating_result` and only recognizes evidenced transfers.

    Call it as `settle_liquidity_projection` at any new call site -- the name
    is kept as an alias so existing imports/tests are not broken by the
    rename.

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


def reconcile_actual_liquidity(
    *,
    opening_balance: Decimal,
    evidenced_deposit: Decimal = Decimal("0"),
    evidenced_used: Decimal = Decimal("0"),
    investment_yield: Decimal = Decimal("0"),
    opening_uncovered_deficit: Decimal = Decimal("0"),
    operating_result: Decimal = Decimal("0"),
) -> LiquidityReconciliation:
    """Close a REALIZADO period's Conta Corrente <-> Privilège DI position.

    October Go-Live Rebaseline §4.2/§4.3/§5.1 and the accepted Technical
    Challenge #1 (`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §8): a real
    withdrawal/deposit is never inferred from `operating_result`'s sign. It is
    recognized only through `evidenced_deposit`/`evidenced_used` -- the
    already-categorized "Transferência patrimonial" transaction totals built
    by `app.services.financial_snapshots._collect`, themselves created either
    from an imported/observed bank movement or from a user/Assistant-confirmed
    action. `AccountBalanceObservation` alone never reaches this function as
    evidence: it only ever feeds `opening_balance` (the sovereign starting
    point for the period), never `evidenced_deposit`/`evidenced_used`.

    When no evidence of movement exists this period (`evidenced_deposit == 0`
    and `evidenced_used == 0`), the closing balance is simply the opening
    balance (plus any evidenced yield) -- untouched -- and a nonzero
    `operating_result` is surfaced verbatim as `unexplained_operating_result`
    for human review, never masked and never used to fabricate a transfer.

    A prior `opening_uncovered_deficit` (carried from a period closed before
    this reconciliation existed, or from a pathological evidenced shortfall
    below) can only shrink via an evidenced deposit -- never via an inferred
    surplus. An evidenced withdrawal that exceeds what is actually available
    (e.g. real bank data disagrees with the model) is never clamped away
    silently: it is surfaced as an uncovered/unexplained shortfall for
    investigation, exactly like any other divergence in this rebaseline.
    """

    opening = _money(max(Decimal("0"), opening_balance))
    debt = _money(max(Decimal("0"), opening_uncovered_deficit))
    deposit = _money(max(Decimal("0"), evidenced_deposit))
    used = _money(max(Decimal("0"), evidenced_used))
    yield_amount = _money(investment_yield)
    has_evidence = deposit > 0 or used > 0

    debt_payment = min(debt, deposit)
    remaining_debt = _money(debt - debt_payment)
    remaining_deposit = _money(deposit - debt_payment)

    if remaining_debt > 0:
        # Still unresolved legacy debt: no positive balance can appear until
        # it is paid down by a real, evidenced deposit. A real withdrawal
        # while still in debt cannot be quietly dropped -- it can only mean
        # the real account moved money that this model did not expect, so it
        # widens the uncovered/unexplained amount instead of vanishing.
        closing = Decimal("0.00")
        closing_debt = _money(remaining_debt + used)
    else:
        raw_closing = _money(opening + remaining_deposit + yield_amount - used)
        closing = _money(max(Decimal("0"), raw_closing))
        closing_debt = _money(max(Decimal("0"), -raw_closing))

    unexplained = Decimal("0.00") if has_evidence else _money(operating_result)

    return LiquidityReconciliation(
        opening_balance=opening,
        opening_uncovered_deficit=debt,
        evidenced_deposit=deposit,
        evidenced_used=used,
        investment_yield=yield_amount,
        operating_result=_money(operating_result),
        liquidity_deposit=deposit,
        liquidity_used=used,
        closing_balance=closing,
        closing_uncovered_deficit=closing_debt,
        has_transfer_evidence=has_evidence,
        unexplained_operating_result=unexplained,
    )


def calculate_actual_snapshot(data: FinancialEngineInput) -> FinancialEngineResult:
    income = _money(max(Decimal("0"), data.operating_income))
    expenses = _money(max(Decimal("0"), data.operating_expenses))
    operating_result = _money(income - expenses)
    investments = _money(max(Decimal("0"), data.investments))
    redemptions = _money(max(Decimal("0"), data.redemptions))
    # `investments`/`redemptions` are the same real "Transferência
    # patrimonial" evidence used as `evidenced_deposit`/`evidenced_used`
    # below -- there is deliberately no second, independent source for "how
    # much moved into/out of Privilège this period" (see
    # `reconcile_actual_liquidity`'s docstring). Reusing them here, rather
    # than inventing a parallel figure, is what keeps this a single
    # financial engine instead of two disagreeing ones.
    reconciliation = reconcile_actual_liquidity(
        opening_balance=data.opening_liquidity_balance,
        evidenced_deposit=investments,
        evidenced_used=redemptions,
        investment_yield=data.investment_yield,
        opening_uncovered_deficit=data.opening_uncovered_deficit,
        operating_result=operating_result,
    )
    floor = _money(max(Decimal("0"), data.safety_floor))
    distance_to_floor = _money(reconciliation.closing_balance - floor)
    budget_cap = _money(max(Decimal("0"), data.budget_cap))
    return FinancialEngineResult(
        period=data.period,
        operating_income=income,
        operating_expenses=expenses,
        operating_result=operating_result,
        bank_cash_in=_money(data.bank_cash_in),
        bank_cash_out=_money(data.bank_cash_out),
        bank_cash_result=_money(data.bank_cash_in - data.bank_cash_out),
        investments=investments,
        redemptions=redemptions,
        internal_transfers=_money(max(Decimal("0"), data.internal_transfers)),
        card_spend=_money(max(Decimal("0"), data.card_spend)),
        card_payments=_money(max(Decimal("0"), data.card_payments)),
        refunds=_money(max(Decimal("0"), data.refunds)),
        opening_liquidity_balance=reconciliation.opening_balance,
        investment_yield=reconciliation.investment_yield,
        liquidity_used=reconciliation.liquidity_used,
        liquidity_deposit=reconciliation.liquidity_deposit,
        closing_liquidity_balance=reconciliation.closing_balance,
        opening_uncovered_deficit=reconciliation.opening_uncovered_deficit,
        closing_uncovered_deficit=reconciliation.closing_uncovered_deficit,
        safety_floor=floor,
        distance_to_floor=distance_to_floor,
        floor_breached=reconciliation.closing_balance < floor,
        budget_cap=budget_cap,
        budget_usage=expenses,
        budget_remaining=_money(budget_cap - expenses),
        commitments=_money(max(Decimal("0"), data.commitments)),
        projected_balance=reconciliation.closing_balance,
        source_count=max(0, data.source_count),
        has_transfer_evidence=reconciliation.has_transfer_evidence,
        unexplained_operating_result=reconciliation.unexplained_operating_result,
    )


#: Explicit projection-only name for `settle_liquidity` (see its docstring).
#: Prefer this name at new call sites; `settle_liquidity` remains for
#: existing imports.
settle_liquidity_projection = settle_liquidity


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value
