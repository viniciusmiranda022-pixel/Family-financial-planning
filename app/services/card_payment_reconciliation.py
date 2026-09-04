"""Deterministic visual reconciliation of card-invoice payments to bank debits.

Scope (see `docs/WORK_ORDER_VISUAL_CARD_PAYMENT_RECONCILIATION.md`): every
line the importer classifies as `transaction_type == "reconciliation"` is
already, by the single shared classifier (`app/services/classifier.py`,
`PAYMENT_PATTERN`), excluded from every operating total -- INV-002 is true
before this module ever runs and stays true after, because nothing here ever
changes `amount`, `transaction_type` or `excluded` on any `Transaction`. Two
independent documents can each produce their own "reconciliation" row for the
very same real-world event:

- the **card invoice** ("fatura") itself, on the `credit_card` account,
  carrying its own "payment received" line (positive amount -- see
  `_normalize_credit_card_amount`), dated to the invoice's reference month
  (`booked_at = reference.replace(day=1)` in every PDF parser); and
- the **bank statement**, on the `checking` account, carrying the actual
  debit that left the household's cash (negative amount), dated to the real
  banking day the money moved.

This module only *links* those two already-canonical, already-excluded rows
together for human review -- it is pure lineage/evidence, not a new
financial calculation. Linking or unlinking a pair changes exactly one
column, `Transaction.linked_transaction_id` (already part of the schema
since migration 0004), symmetrically on both rows; it never touches amount,
type, category, exclusion or any other financial fact, so it cannot affect
any snapshot, report, projection or invariant total.

Matching policy (single, shared with the API/UI -- no second policy):

1. Only rows with `transaction_type == "reconciliation"` are ever considered
   on either side; a plain expense/income transaction is never treated as a
   candidate no matter how well its amount lines up (that would be exactly
   the "infer a bank debit from amount alone" anti-pattern the Work Order
   prohibits).
2. A candidate pair must have `abs(card.amount) == abs(bank.amount)` within
   the project's standard monetary tolerance (`RECONCILIATION_TOLERANCE`,
   R$ 0.01, `app/services/reconciliation.py`).
3. An *automatically suggested* candidate must additionally fall inside
   `CARD_PAYMENT_MATCH_WINDOW_DAYS` of the invoice's `booked_at` (see the
   module-level constant for why the window is symmetric and how wide it
   is). This bounds the search space so "same amount, unrelated month" pairs
   two years apart are never silently offered as a match.
4. A pair is a deterministic `matched` suggestion only when it is a mutual,
   isolated one-to-one edge in the bipartite candidate graph: the checking
   row has exactly one card candidate *and* that same card row has exactly
   one checking candidate (see `_candidate_edges`/`_card_side_degrees`).
   Checking one side alone is not enough -- a single card-side payment can be the sole
   candidate for two different bank debits of the same amount in the same
   window, and neither is then a safe one-click suggestion even though each
   looks deterministic in isolation. Zero candidates is `unmatched` (explicit
   unknown/review, never fabricated). Any other shape -- more than one
   candidate on either side, including the "1 checking : 1 candidate, but
   that candidate also serves another checking row" case above -- is
   `ambiguous`, surfaced with every candidate, never auto-resolved.
5. A human can still confirm a link outside the date window (a legitimately
   late payment): `link_card_payment` only enforces the amount tolerance,
   never the date window, because the operator -- not a heuristic -- is
   providing the evidence at that point. The window only gates what the
   system offers unprompted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.reconciliation import RECONCILIATION_TOLERANCE

# Every PDF/CSV parser pins a card invoice's own rows (purchases and its own
# payment-received line alike) to the first day of the invoice's reference
# month (see `_parse_nubank_credit_card_pdf`/`_parse_itau_credit_card_pdf`),
# while the paired bank debit keeps its real banking date. In practice the
# due date -- and so the debit -- almost always falls within the same
# calendar month or shortly into the next one, but the exact gap depends on
# each institution's billing cycle and is not documented as a fixed number of
# days anywhere in `docs/FINANCIAL_RULES.md`. 45 days comfortably covers a
# full billing cycle either direction (a debit posted a little before the
# parser's reference date, e.g. a CSV/manual import that preserves the real
# purchase date instead of shifting it, is still considered) without being
# unbounded. Widening this window only ever produces more `ambiguous`
# candidates for a human to review -- it can never cause an automatic
# `matched` result, so it is a usefulness knob, not a financial-safety one.
CARD_PAYMENT_MATCH_WINDOW_DAYS = 45


class CardPaymentLinkError(ValueError):
    """Raised for a link/unlink request that conflicts with current state."""


@dataclass(frozen=True, slots=True)
class CardPaymentCandidate:
    transaction_id: str
    amount: Decimal
    booked_at: str
    competence: str | None
    description: str
    account: str
    difference: Decimal


@dataclass(frozen=True, slots=True)
class CardPaymentMatch:
    """One bank-side reconciliation row and its evidence.

    `status`:
    - `linked`: a human has confirmed (or a prior deterministic suggestion
      was confirmed into) `Transaction.linked_transaction_id` on both sides.
    - `matched`: exactly one deterministic candidate exists and is ready to
      confirm, but no one has confirmed it yet.
    - `ambiguous`: more than one candidate exists; none is auto-selected.
    - `unmatched`: no candidate exists yet; explicit unknown/review state.
    """

    checking_transaction_id: str
    status: str
    linked_transaction_id: str | None = None
    candidates: tuple[CardPaymentCandidate, ...] = field(default_factory=tuple)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _candidates_for_checking(checking: Any, card_rows: list[Any], window: timedelta) -> list[Any]:
    candidates = [
        card
        for card in card_rows
        if card.linked_transaction_id is None
        and abs(abs(card.amount) - abs(checking.amount)) <= RECONCILIATION_TOLERANCE
        and abs(card.booked_at - checking.booked_at) <= window
    ]
    candidates.sort(key=lambda card: (abs(card.booked_at - checking.booked_at), card.id))
    return candidates


def _candidate_edges(checking_rows: list[Any], card_rows: list[Any]) -> dict[str, list[Any]]:
    """Every unlinked checking row's amount/window-eligible unlinked card
    candidates, keyed by checking id.

    This is the whole bipartite candidate graph for the household -- built
    once over *every* unlinked reconciliation row regardless of the `period`
    a caller asked to display, because a card row's true cardinality (does
    it serve one checking row or several?) depends on the household's whole
    history, not on whichever slice of it is currently being viewed. Slicing
    by period first and only then computing degree would let two checking
    rows that both claim the same card row, one in-period and one out, each
    look deterministic in isolation -- exactly the false-certainty bug this
    function exists to close.

    A checking row that already carries a `linked_transaction_id` has no
    entry here on purpose: it is resolved, so it must never contribute a
    phantom edge that inflates another, still-unresolved checking row's
    card-side degree count and wrongly flags a genuine 1:1 pair as
    `ambiguous`. `list_card_payment_reconciliations` computes its own
    stand-alone candidate list for the one defensive corner case where a
    row is linked but its counterpart fails to resolve.
    """

    window = timedelta(days=CARD_PAYMENT_MATCH_WINDOW_DAYS)
    edges: dict[str, list[Any]] = {}
    for checking in checking_rows:
        if checking.linked_transaction_id:
            continue
        edges[checking.id] = _candidates_for_checking(checking, card_rows, window)
    return edges


def _card_side_degrees(edges: dict[str, list[Any]]) -> dict[str, int]:
    """How many distinct checking rows currently list each card row as a
    candidate -- the inverse-cardinality count a per-checking-row view alone
    cannot see (see `_candidate_edges` and the module docstring, point 4)."""

    degrees: dict[str, int] = {}
    for candidates in edges.values():
        for card in candidates:
            degrees[card.id] = degrees.get(card.id, 0) + 1
    return degrees


def list_card_payment_reconciliations(
    db: Session,
    *,
    household_id: str,
    period: str | None = None,
) -> list[CardPaymentMatch]:
    """Deterministic, read-only evidence for every bank-side card payment.

    Never mutates the database -- always recomputed from current facts, so
    it can never drift from what a fresh import or a human unlink just
    changed. `period` filters which bank-side (checking) rows are *returned*
    by competence (`YYYY-MM`); the candidate graph itself (and therefore
    `matched` vs `ambiguous`) is always computed across the household's
    whole history, both because a payment can land in the month after its
    invoice and because restricting the graph to one period would hide the
    other checking row in a two-checkings-one-card conflict whenever that
    row falls outside the requested period (see `_candidate_edges`).
    """

    from app.models import Account, Transaction

    all_checking_rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "reconciliation",
            Account.account_type == "checking",
        )
        .order_by(Transaction.booked_at.desc(), Transaction.id)
    ).all()
    if not all_checking_rows:
        return []

    if period:
        checking_rows = [row for row in all_checking_rows if row.competence == period]
        if not checking_rows:
            return []
    else:
        checking_rows = all_checking_rows

    card_rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "reconciliation",
            Account.account_type == "credit_card",
        )
    ).all()
    card_by_id = {row.id: row for row in card_rows}

    # Built once, over the whole household -- see `_candidate_edges`.
    edges = _candidate_edges(all_checking_rows, card_rows)
    card_degrees = _card_side_degrees(edges)
    window = timedelta(days=CARD_PAYMENT_MATCH_WINDOW_DAYS)

    results: list[CardPaymentMatch] = []
    for checking in checking_rows:
        if checking.linked_transaction_id:
            linked = card_by_id.get(checking.linked_transaction_id) or db.get(
                Transaction, checking.linked_transaction_id
            )
            if linked is not None and linked.household_id == household_id:
                results.append(
                    CardPaymentMatch(
                        checking_transaction_id=checking.id,
                        status="linked",
                        linked_transaction_id=linked.id,
                    )
                )
                continue
            # Defensive corner case: `linked_transaction_id` is set but does
            # not resolve to a same-household counterpart (should not
            # happen through this module's own API, which always sets/clears
            # both sides together -- but this row has no entry in `edges`,
            # which deliberately excludes every linked checking row, so its
            # candidates are computed stand-alone here instead of via the
            # shared graph). `card_degrees` below cannot see this row's own
            # claim on its candidate, so a would-be "exactly one candidate"
            # case can never be verified as a genuine mutual 1:1 match here
            # -- always `ambiguous` (unless there is no candidate at all)
            # rather than risk a false `matched` for a row whose own link
            # state is already inconsistent.
            candidates = _candidates_for_checking(checking, card_rows, window)
            status = "unmatched" if not candidates else "ambiguous"
        else:
            candidates = edges[checking.id]
            if len(candidates) == 0:
                status = "unmatched"
            elif len(candidates) == 1 and card_degrees.get(candidates[0].id, 0) == 1:
                # Mutual, isolated edge: this checking row has exactly one
                # candidate, and that card row has no other suitor either.
                status = "matched"
            else:
                # Either multiple card candidates for this checking row, or
                # its single candidate is also claimed by another checking
                # row -- both are globally ambiguous evidence, never
                # auto-resolved.
                status = "ambiguous"
        results.append(
            CardPaymentMatch(
                checking_transaction_id=checking.id,
                status=status,
                candidates=tuple(
                    CardPaymentCandidate(
                        transaction_id=card.id,
                        amount=card.amount,
                        booked_at=card.booked_at.isoformat(),
                        competence=card.competence,
                        description=card.description,
                        account=card.account.name if card.account else "",
                        difference=_money(abs(card.amount) - abs(checking.amount)),
                    )
                    for card in candidates
                ),
            )
        )
    return results


def serialize_transaction_summary(transaction: Any) -> dict[str, Any]:
    return {
        "id": transaction.id,
        "date": transaction.booked_at.isoformat(),
        "competence": transaction.competence,
        "description": transaction.description,
        "amount": str(transaction.amount),
        "account_id": transaction.account_id,
        "account": transaction.account.name if transaction.account else "",
        "account_type": transaction.account.account_type if transaction.account else None,
    }


def serialize_card_payment_match(db: Session, match: CardPaymentMatch) -> dict[str, Any]:
    from app.models import Transaction

    checking = db.get(Transaction, match.checking_transaction_id)
    payload: dict[str, Any] = {
        "checking_transaction": serialize_transaction_summary(checking) if checking else None,
        "status": match.status,
        "linked_transaction": None,
        "candidates": [
            {
                "transaction_id": candidate.transaction_id,
                "amount": str(candidate.amount),
                "date": candidate.booked_at,
                "competence": candidate.competence,
                "description": candidate.description,
                "account": candidate.account,
                "difference": str(candidate.difference),
            }
            for candidate in match.candidates
        ],
    }
    if match.linked_transaction_id:
        linked = db.get(Transaction, match.linked_transaction_id)
        payload["linked_transaction"] = serialize_transaction_summary(linked) if linked else None
    return payload


def _reconciliation_transaction_or_404(
    db: Session, *, household_id: str, transaction_id: str, for_update: bool = False
) -> Any:
    from app.models import Transaction

    query = select(Transaction).where(
        Transaction.id == transaction_id,
        Transaction.household_id == household_id,
    )
    if for_update:
        # No-op on SQLite (used by the test suite); on PostgreSQL this takes
        # the row-level write lock `link_card_payment` needs -- see its
        # docstring.
        query = query.with_for_update()
    transaction = db.scalar(query)
    if transaction is None:
        raise LookupError("transaction not found")
    if transaction.transaction_type != "reconciliation":
        raise CardPaymentLinkError("Somente lançamentos classificados como conciliação podem ser vinculados")
    return transaction


def link_card_payment(
    db: Session,
    *,
    household_id: str,
    checking_transaction_id: str,
    card_transaction_id: str,
) -> tuple[Any, Any]:
    """Human-confirmed link between a bank debit and its card-invoice payment.

    Pure lineage: only `linked_transaction_id` changes, symmetrically, on
    both rows. Amount, type, category and exclusion are untouched, so this
    can never change any operating total, snapshot or invariant result.
    Raises `LookupError` (-> 404) when a transaction id does not belong to
    this household, `CardPaymentLinkError` (-> 409) for any business-rule
    conflict.

    Concurrency: both candidate rows are fetched with `SELECT ... FOR
    UPDATE` (a no-op outside PostgreSQL) *before* the `linked_transaction_id`
    conflict check below, always in the same id-sorted order regardless of
    which argument named which row. That ordering is what makes two
    concurrent link attempts that reference either of the same two rows
    serialize instead of deadlocking -- a fixed global order for
    multi-row locks is the standard way to avoid a lock-ordering deadlock.
    Without the lock, two requests could both read `linked_transaction_id is
    None` for the same row before either writes, and both commit a link,
    leaving that row's counterpart pointing at two different partners (a
    non-symmetric pair).
    """

    if checking_transaction_id == card_transaction_id:
        raise CardPaymentLinkError("Selecione dois lançamentos diferentes")

    locked_by_id = {
        row_id: _reconciliation_transaction_or_404(
            db, household_id=household_id, transaction_id=row_id, for_update=True
        )
        for row_id in sorted((checking_transaction_id, card_transaction_id))
    }
    checking = locked_by_id[checking_transaction_id]
    card = locked_by_id[card_transaction_id]
    checking_type = checking.account.account_type if checking.account else None
    card_type = card.account.account_type if card.account else None
    if {checking_type, card_type} != {"checking", "credit_card"}:
        raise CardPaymentLinkError(
            "O vínculo exige um lançamento de conta corrente e um lançamento de cartão de crédito"
        )
    if checking_type != "checking":
        checking, card = card, checking
    if checking.linked_transaction_id or card.linked_transaction_id:
        raise CardPaymentLinkError(
            "Um dos lançamentos já está vinculado; desvincule antes de criar um novo vínculo"
        )
    if abs(abs(checking.amount) - abs(card.amount)) > RECONCILIATION_TOLERANCE:
        raise CardPaymentLinkError(
            "Os valores dos dois lançamentos não coincidem dentro da tolerância de R$ 0,01"
        )
    checking.linked_transaction_id = card.id
    card.linked_transaction_id = checking.id
    return checking, card


def unlink_card_payment(
    db: Session,
    *,
    household_id: str,
    transaction_id: str,
) -> tuple[Any, Any]:
    """Reverse a confirmed link. Non-destructive: both rows keep existing.

    Concurrency: an unlocked probe read of `transaction_id` only tells us
    who its counterpart *was* at that instant. Both rows are then re-fetched
    with `SELECT ... FOR UPDATE`, in the same id-sorted order
    `link_card_payment` uses -- two concurrent `unlink` calls naming either
    end of the very same pair sort to the identical `(X, Y)` order, so they
    serialize instead of deadlocking. After acquiring the locks we re-check
    that `transaction`'s counterpart is still the one we probed; if a
    concurrent request already unlinked and relinked it to a third row in
    the gap between the probe and the lock, we fail closed with a conflict
    instead of silently nulling out that unrelated, newer link.
    """

    probe = _reconciliation_transaction_or_404(db, household_id=household_id, transaction_id=transaction_id)
    if not probe.linked_transaction_id:
        raise CardPaymentLinkError("Este lançamento não está vinculado")
    counterpart_id = probe.linked_transaction_id

    locked_by_id = {
        row_id: _reconciliation_transaction_or_404(
            db, household_id=household_id, transaction_id=row_id, for_update=True
        )
        for row_id in sorted((transaction_id, counterpart_id))
    }
    transaction = locked_by_id[transaction_id]
    if transaction.linked_transaction_id != counterpart_id:
        raise CardPaymentLinkError(
            "O vínculo deste lançamento mudou; atualize a tela e tente novamente"
        )
    counterpart = locked_by_id[counterpart_id]
    transaction.linked_transaction_id = None
    counterpart.linked_transaction_id = None
    return transaction, counterpart
