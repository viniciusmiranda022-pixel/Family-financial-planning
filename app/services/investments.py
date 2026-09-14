"""Canonical patrimony/investment calculations (P0 #87, October Go-Live
Slice 6 -- `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md`, rebaseline §16).

Every derived investment figure (gain, return %) and the household's net
worth total are computed here exactly once. `app.api` (the `/dashboard`
endpoint and the `/investments*` routes) and, later, Slice 7's reports must
call these functions instead of re-deriving any of them independently --
rebaseline "nenhum cálculo patrimonial duplicado no frontend"/"não criar
segundo motor de cálculo".

**Invariant central (rebaseline §16.2):** `net_worth_summary` sums
`current_value` only. `historical_cost` (cost basis) and
`expected_receivable_value` (future projection) never enter the total --
see `tests/test_investments_slice6.py` for the property/regression tests
proving this, and `app.services.financial_invariants.
validate_net_worth_current_value_only` (INV-034) for the formal contract.

Deliberately independent from SQLAlchemy's `Session` type where possible
(only `net_worth_summary` needs a `Session` to query); the per-asset
derived calculations take a plain `Investment` ORM instance (or any object
exposing the same four Decimal-typed attributes) so they stay trivially
unit-testable without a database.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.finance import money

PERCENT_QUANTUM = Decimal("0.0001")


class _InvestmentLike(Protocol):
    historical_cost: Decimal
    current_value: Decimal
    expected_receivable_value: Decimal | None


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Ratio `numerator / denominator` quantized to four decimal places
    (the same rate precision `FinancialProfile.investment_gross_annual_rate`
    already uses), or `None` when `denominator` is zero/negative -- never a
    `ZeroDivisionError`, never a fabricated `0`/`100%` standing in for
    "undefined" (Work Order "Testes obrigatórios": "percentuais/ganhos
    tratam custo zero/nulo sem divisão inválida")."""

    if denominator <= 0:
        return None
    return (numerator / denominator).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)


def investment_gain_current(investment: _InvestmentLike) -> Decimal:
    """Ganho/perda atual = valor de hoje - valor investido (rebaseline
    §16.1). Always defined -- zero cost basis is a valid input (a donated
    or gifted asset), it just means the entire current value is gain."""

    return money(Decimal(investment.current_value) - Decimal(investment.historical_cost))


def investment_return_current_pct(investment: _InvestmentLike) -> Decimal | None:
    """Retorno atual % = ganho atual / valor investido. `None` when
    `historical_cost` is zero/negative -- a return percentage on zero cost
    basis is mathematically undefined, never reported as `0` or `100%`."""

    return _percent(investment_gain_current(investment), Decimal(investment.historical_cost))


def investment_gain_projected(investment: _InvestmentLike) -> Decimal | None:
    """Ganho projetado = valor previsto a receber - valor investido.
    `None` when there is no `expected_receivable_value` yet -- never
    defaulted to the current gain or to zero."""

    if investment.expected_receivable_value is None:
        return None
    return money(Decimal(investment.expected_receivable_value) - Decimal(investment.historical_cost))


def investment_return_projected_pct(investment: _InvestmentLike) -> Decimal | None:
    """Retorno projetado % = ganho projetado / valor investido. `None`
    when there is no projection yet, or when `historical_cost` is
    zero/negative."""

    gain = investment_gain_projected(investment)
    if gain is None:
        return None
    return _percent(gain, Decimal(investment.historical_cost))


def serialize_investment(investment: Any) -> dict[str, Any]:
    """Canonical row shape for one investment, including every derived
    figure -- the one function `GET /investments`, `net_worth_summary`'s
    `investments` list, and (in Slice 7) reports must all use, instead of
    each re-deriving `gain_current`/`return_current_pct`/etc. independently.
    Values stay `Decimal`/`date`/`None` here (no JSON coercion) -- callers
    apply `app.api.decimal_value` at the HTTP boundary, same convention as
    every other service in this project."""

    return {
        "id": investment.id,
        "name": investment.name,
        "historical_cost": investment.historical_cost,
        "current_value": investment.current_value,
        "expected_receivable_value": investment.expected_receivable_value,
        "gain_current": investment_gain_current(investment),
        "return_current_pct": investment_return_current_pct(investment),
        "gain_projected": investment_gain_projected(investment),
        "return_projected_pct": investment_return_projected_pct(investment),
        "last_updated_at": investment.last_updated_at,
        "expected_receipt_date": investment.expected_receipt_date,
        "notes": investment.notes,
        "active": investment.active,
    }


def net_worth_summary(db: Session, *, household_id: str) -> dict[str, Any]:
    """The single, canonical patrimony aggregation for a household
    (rebaseline §16.2 -- "o patrimônio atual usa somente o Valor de hoje").

    Sums `current_value` across every *active* investment exactly once
    each; `historical_cost`/`expected_receivable_value` never enter
    `total_current_value` -- see `tests/test_investments_slice6.py::
    test_net_worth_summary_sums_current_value_only_never_historical_or_projected`.
    `/dashboard` and, in Slice 7, reports must call this function rather
    than re-querying/re-summing `Investment` rows independently."""

    from app.models import Investment

    investments = list(
        db.scalars(
            select(Investment)
            .where(Investment.household_id == household_id, Investment.active.is_(True))
            .order_by(Investment.name)
        )
    )
    total_current_value = money(sum((Decimal(item.current_value) for item in investments), Decimal("0")))
    return {
        "total_current_value": total_current_value,
        "investments": [serialize_investment(item) for item in investments],
    }
