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

October Go-Live Slice 3, Round 2 of PR #91's review: the first cut matched
on amount + competence alone, so *any* income `Transaction` of a
coincidentally similar value in the same month (a reimbursement, a gift, an
unrelated receipt) could silently be promoted to REALIZADO and presented as
"the salary" -- violating "hipótese não vira fato silenciosamente"
(rebaseline invariant list) and the Slice 3 Work Order's own invariant
("Hipótese não vira fato silenciosamente"). Two changes close that gap:

1. Amount + competence alone is no longer sufficient identity evidence.
   Unless the caller passes an explicit `owner_label` (itself a real,
   already-persisted identity signal -- narrows the query to one person's
   transactions), a candidate must also carry a description marker that
   plausibly identifies it as a payroll credit (`SALARY_DESCRIPTION_MARKERS`
   below) -- the same kind of keyword evidence
   `app.services.classifier.normalize_description` already normalizes
   transaction text for elsewhere in this codebase, not a new
   classification engine.
2. When more than one transaction satisfies amount + competence + identity
   evidence, the period is **not** silently resolved by picking the closest
   amount. It stays PREVISTO and the ambiguity is surfaced
   (`ambiguous=True`) so a human can review it -- "não escolher `best` por
   tie-break e tratar como fato".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.classifier import normalize_description
from app.services.finance import money
from app.services.financial_invariants import MONEY_TOLERANCE
from app.services.financial_state import PREVISTO, REALIZADO

# Deliberately small and literal -- not a learned/fuzzy classifier. A real
# payroll credit's bank description reliably carries one of these words in
# Portuguese bank statements; anything else requires an explicit
# `owner_label` instead (see module docstring, point 1).
SALARY_DESCRIPTION_MARKERS: tuple[str, ...] = (
    "SALARIO",
    "HOLERITE",
    "FOLHA DE PAGAMENTO",
    "FOLHA PAGAMENTO",
    "PAYROLL",
)


def _has_salary_marker(description: str) -> bool:
    normalized = normalize_description(description)
    return any(marker in normalized for marker in SALARY_DESCRIPTION_MARKERS)


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
    # October Go-Live Slice 3, Round 2: true when two or more transactions
    # independently satisfied amount + competence + identity evidence for
    # this period -- the period stays PREVISTO and this flag tells a caller
    # a human review is warranted, instead of the ambiguity being invisible.
    ambiguous: bool = False


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

    Never invents a match: outside the tolerance window, without salary
    identity evidence, or with more than one equally-plausible candidate,
    this reports PREVISTO untouched. A duplicate/possible-duplicate or
    excluded transaction is never a candidate -- the same guard
    `_forecast_obligations` and every other canonical aggregate already
    respects. `owner_label` is an optional extra filter (e.g. restrict to
    one person's transactions); when passed, it *is* the identity evidence
    (the query itself narrows to that person), so a description marker is
    not additionally required. The matching itself never hardcodes a
    name -- the recurring salary belongs to whichever household member
    `FinancialProfile.monthly_salary_net` was configured for.
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

    # October Go-Live Slice 3, Round 2: identity evidence beyond amount +
    # competence. An explicit `owner_label` already narrowed `candidates` at
    # the query level, so every remaining match carries that evidence;
    # otherwise each candidate must independently carry a salary marker in
    # its own description.
    identified = matches if owner_label else [tx for tx in matches if _has_salary_marker(tx.description)]
    if not identified:
        return RecurringIncomeReconciliation(period, expected, PREVISTO, candidate_count=len(candidates))

    if len(identified) > 1:
        # Never pick a "best" candidate by tie-break here -- multiple
        # transactions independently look like this period's salary, so the
        # period stays a hypothesis and the ambiguity is surfaced instead of
        # silently resolved.
        return RecurringIncomeReconciliation(
            period, expected, PREVISTO, candidate_count=len(candidates), ambiguous=True
        )

    best = identified[0]
    return RecurringIncomeReconciliation(
        period,
        expected,
        REALIZADO,
        matched_transaction_id=best.id,
        matched_amount=money(best.amount),
        matched_booked_at=best.booked_at,
        candidate_count=len(candidates),
    )
