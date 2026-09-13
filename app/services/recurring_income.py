"""Reconciles the household's configured recurring monthly salary against a
real income `Transaction`, so PREVISTO becomes REALIZADO without ever
creating a second income fact.

October Go-Live Rebaseline §8.4 (`docs/OCTOBER_GO_LIVE_REBASELINE.md`):
Kelly's recurring salary may appear in the forward projection as PREVISTO
(`FinancialProfile.monthly_salary_net`, applied flatly every future month by
`app.services.projection_engine.build_projection`). "Quando o crédito real
chegar: PREVISTO -> REALIZADO. A conciliação não pode criar uma segunda
receita." This module is the deterministic mechanism that closes that loop:
for a given competence, it looks for a real `income` `Transaction` that
plausibly *is* the configured recurring salary and, when found, reports the
period as REALIZADO with the matched transaction's own amount -- callers
must then use that amount **instead of** the flat PREVISTO estimate for that
period, never in addition to it (see `app.api._build_projection_gate_checks`,
which folds the result into `ForecastInput.monthly_salary_overrides`).

Mirrors the tolerance-based matching pattern already used by
`app.services.card_payment_reconciliation` -- a household-configured amount
compared, within `MONEY_TOLERANCE`, against a real transaction already
persisted for the exact competence in question. This is not a second
reconciliation engine: it never creates, edits or deletes a `Transaction`,
never guesses a competence, and a period with no match simply stays
PREVISTO -- an ambiguous or absent match is never promoted to fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.finance import money
from app.services.financial_invariants import MONEY_TOLERANCE
from app.services.financial_state import PREVISTO, REALIZADO


@dataclass(frozen=True, slots=True)
class RecurringIncomeReconciliation:
    """Result of reconciling one competence's configured recurring income."""

    period: str
    expected_amount: Decimal
    financial_state: str
    matched_transaction_id: str | None = None
    matched_amount: Decimal | None = None
    matched_booked_at: date | None = None
    candidate_count: int = 0


def reconcile_recurring_income(
    db: Session,
    *,
    household_id: str,
    period: str,
    expected_amount: Decimal,
    owner_label: str | None = None,
) -> RecurringIncomeReconciliation:
    """Looks for a real income `Transaction` already recorded for `period`
    (a canonical "YYYY-MM" competence) that plausibly *is* the household's
    configured recurring salary for that month.

    Never invents a match: outside the tolerance window this reports
    PREVISTO untouched. A duplicate/possible-duplicate or excluded
    transaction is never a candidate -- the same guard `_forecast_obligations`
    and every other canonical aggregate already respects. `owner_label` is an
    optional extra filter (e.g. restrict to one person's transactions); the
    matching itself never hardcodes a name -- the recurring salary belongs to
    whichever household member `FinancialProfile.monthly_salary_net` was
    configured for.
    """

    expected = money(expected_amount)
    if expected <= 0:
        return RecurringIncomeReconciliation(period, expected, PREVISTO)

    query = select(Transaction).where(
        Transaction.household_id == household_id,
        Transaction.transaction_type == "income",
        Transaction.excluded.is_(False),
        Transaction.possible_duplicate.is_(False),
        Transaction.competence == period,
    )
    if owner_label:
        query = query.where(Transaction.owner_label == owner_label)
    candidates = list(db.scalars(query))

    matches = [tx for tx in candidates if abs(money(tx.amount) - expected) <= MONEY_TOLERANCE]
    if not matches:
        return RecurringIncomeReconciliation(period, expected, PREVISTO, candidate_count=len(candidates))

    # Deterministic tie-break: closest amount first, then earliest booking --
    # never a heuristic guess presented as certainty.
    best = min(
        matches,
        key=lambda tx: (abs(money(tx.amount) - expected), tx.booked_at, tx.id),
    )
    return RecurringIncomeReconciliation(
        period,
        expected,
        REALIZADO,
        matched_transaction_id=best.id,
        matched_amount=money(best.amount),
        matched_booked_at=best.booked_at,
        candidate_count=len(candidates),
    )
