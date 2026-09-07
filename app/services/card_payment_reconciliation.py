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
   R$ 0.01, `app/services/reconciliation.py`) *and* the documented financial
   direction: the checking-side row must be a negative bank debit and the
   card-side row must be a positive payment-received line (see
   `_normalize_credit_card_amount` in `app/services/importer.py`, which
   always normalizes a `PAYMENT_PATTERN` row on the card side to a positive
   amount, and `docs/ARCHITECTURE.md`'s "Conciliação visual de pagamento de
   fatura" section). Matching on absolute value alone would also treat two
   same-magnitude rows on the *wrong* sides -- e.g. a positive checking
   credit and a positive card row, or two negative rows -- as a valid card
   payment, which is not the relationship this feature exists to evidence.
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
   late payment): `link_card_payment` only enforces the amount tolerance and
   the same direction contract as the candidate policy above, never the date
   window, because the operator -- not a heuristic -- is providing the date
   evidence at that point. A human confirmation can override *when* the
   debit happened, but it cannot turn a non-debit/non-payment pair (wrong
   sign on either side) into a card-payment reconciliation -- that would
   silently misrepresent the lineage this feature is supposed to make
   auditable, not merely bypass a convenience heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.services.duplicates import DuplicateAssessment

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


def _is_card_payment_direction(*, checking_amount: Decimal, card_amount: Decimal) -> bool:
    """The only financial direction this feature evidences: a negative bank
    debit paired with a positive card "payment received" line (see the
    module docstring, point 2, and `_normalize_credit_card_amount` in
    `app/services/importer.py`). Anything else -- a positive checking
    credit, a negative card-side row -- is not a card-invoice payment no
    matter how well the absolute values line up.
    """

    return checking_amount < 0 and card_amount > 0


def _candidates_for_checking(checking: Any, card_rows: list[Any], window: timedelta) -> list[Any]:
    candidates = [
        card
        for card in card_rows
        if card.linked_transaction_id is None
        and _is_card_payment_direction(checking_amount=checking.amount, card_amount=card.amount)
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


def _verified_reconciliation_counterpart(
    db: Session,
    *,
    household_id: str,
    row: Any,
    counterpart_account_type: str,
    counterpart_hint: Any | None = None,
) -> Any | None:
    """Resolve `row.linked_transaction_id` into a genuine, reciprocal
    card-payment counterpart, or `None` if the pointer does not actually
    prove one.

    Go-live manual slice 3 review round (2026-09-07, `BLOQUEIO DE MERGE` --
    "`paid` precisa significar quitação comprovada, não apenas ponteiro não
    nulo"): a non-null `linked_transaction_id` is, by itself, not evidence
    of a card-payment pair -- it only becomes evidence once the counterpart
    is checked against the exact same contract `link_card_payment` enforces
    *before* ever writing that column. Every pair this module's own API
    (`link_card_payment`/`pay_card_invoice`) creates already satisfies every
    check below by construction; this function exists purely to defend
    both `list_card_payment_reconciliations` (the checking-side `linked`
    status) and `list_card_invoice_obligations` (the card-side `paid`
    status) against a pointer that reached this state some other way -- a
    bug, a manual data fix, a future schema change -- so that neither ever
    reports a lineage/quitação fact the data does not actually prove
    (`docs/FINANCIAL_INVARIANTS.md`: absence or inconsistency of evidence is
    never fabricated into success; it must stay explicit).

    Checks:
    - the counterpart transaction exists;
    - it belongs to the same household (lineage never crosses households,
      and the row is never loaded/serialized before this check passes);
    - it is itself `transaction_type == "reconciliation"`;
    - its account is `counterpart_account_type` -- `checking` when
      validating a card-side row's link, `credit_card` when validating a
      checking-side row's link (the same two-sided shape every genuine pair
      has) -- **and** that account itself belongs to `household_id`;
    - the link is reciprocal, `counterpart.linked_transaction_id == row.id`
      -- a one-sided pointer is not lineage, it is corruption;
    - the pair satisfies the same amount tolerance and financial-direction
      contract `link_card_payment` itself enforces
      (`RECONCILIATION_TOLERANCE`, `_is_card_payment_direction`).

    Go-live manual slice 3 review round #5 (2026-09-07, `BLOQUEIO DE MERGE
    #5` -- "household isolation precisa incluir a conta referenciada, não
    só `Transaction.household_id`"): `Transaction.household_id` and
    `Transaction.account_id` are independent columns, so a row's own
    `household_id` matching does not by itself prove its `Account` does
    too. Before this round, `counterpart.household_id == household_id`
    was checked but `counterpart.account.household_id` was not, so a
    counterpart whose *account* belonged to a different household could
    still be accepted as a genuine `checking`/`credit_card` counterpart
    purely on `counterpart.account.account_type`. This function now also
    requires `counterpart.account.household_id == household_id` -- and,
    symmetrically, `row.account.household_id == household_id` for the row
    being proven, since every caller uses this function's `paid`/`linked`
    result as the single source of truth for whether a quitação fact
    exists and whether it is safe to serialize `row`'s own account
    name/metadata. A row (on either side of the pair) whose own account
    does not verifiably belong to this household fails closed here --
    `pending`/`ambiguous`/`unmatched` for the caller, never `paid`/`linked`,
    and never a name/id/date from a foreign account.

    `counterpart_hint` lets a caller that already loaded the household's
    card/checking rows (e.g. `card_by_id` in `list_card_payment_
    reconciliations`) skip a redundant `db.get` -- it is only trusted when
    its id actually matches `row.linked_transaction_id`; every other check
    below still runs against it.
    """

    from app.models import Transaction

    if not row.linked_transaction_id:
        return None
    row_account = row.account
    if row_account is None or row_account.household_id != household_id:
        return None
    counterpart = (
        counterpart_hint
        if counterpart_hint is not None and counterpart_hint.id == row.linked_transaction_id
        else db.get(Transaction, row.linked_transaction_id)
    )
    if counterpart is None or counterpart.household_id != household_id:
        return None
    if counterpart.transaction_type != "reconciliation":
        return None
    counterpart_account = counterpart.account
    if counterpart_account is None or counterpart_account.household_id != household_id:
        return None
    if counterpart_account.account_type != counterpart_account_type:
        return None
    if counterpart.linked_transaction_id != row.id:
        return None
    if counterpart_account_type == "checking":
        checking_amount, card_amount = counterpart.amount, row.amount
    else:
        checking_amount, card_amount = row.amount, counterpart.amount
    if abs(abs(checking_amount) - abs(card_amount)) > RECONCILIATION_TOLERANCE:
        return None
    if not _is_card_payment_direction(checking_amount=checking_amount, card_amount=card_amount):
        return None
    return counterpart


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
            # BLOQUEIO DE MERGE #5 (2026-09-07): `Transaction.household_id`
            # and `Transaction.account_id` are independent columns -- also
            # require the joined `Account` to belong to this household so a
            # row whose own account reference is corrupted (points at
            # another household's account) never enters this household's
            # candidate universe at all, on either side of the pair.
            Account.household_id == household_id,
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
            Account.household_id == household_id,
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
            linked = _verified_reconciliation_counterpart(
                db,
                household_id=household_id,
                row=checking,
                counterpart_account_type="credit_card",
                counterpart_hint=card_by_id.get(checking.linked_transaction_id),
            )
            if linked is not None:
                results.append(
                    CardPaymentMatch(
                        checking_transaction_id=checking.id,
                        status="linked",
                        linked_transaction_id=linked.id,
                    )
                )
                continue
            # Defensive corner case: `linked_transaction_id` is set but does
            # not resolve to a genuine, reciprocal card-payment counterpart
            # (should not happen through this module's own API, which
            # always sets/clears both sides together in lockstep -- but this
            # row has no entry in `edges`, which deliberately excludes every
            # linked checking row, so its candidates are computed stand-alone
            # here instead of via the shared graph). `card_degrees` below
            # cannot see this row's own claim on its candidate, so a
            # would-be "exactly one candidate" case can never be verified as
            # a genuine mutual 1:1 match here -- always `ambiguous` (unless
            # there is no candidate at all) rather than risk a false
            # `matched` for a row whose own link state is already
            # inconsistent.
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
    """Resolve `transaction_id` into a reconciliation-eligible `Transaction`
    of this household, or fail closed with `LookupError` (-> 404).

    Go-live manual slice 3 review round #6 (2026-09-07, `BLOQUEIO DE MERGE
    #6` -- "o próprio fluxo de confirmação `/link` ainda não fecha household
    isolation na `Account`"): `Transaction.household_id` and
    `Transaction.account_id` are independent columns (the same fact that
    drove the `_verified_reconciliation_counterpart` fix in review round #5),
    so a row's own `household_id` matching does not by itself prove its
    `Account` does too. Every caller of this function -- `link_card_payment`,
    `unlink_card_payment`, `pay_card_invoice` -- resolves `transaction.
    account.account_type` right after calling it, without which the "human
    confirms a `checking`/`credit_card` pair" contract this slice's Work
    Order requires cannot be enforced. Before this round, a transaction whose
    own `household_id` matched but whose `account_id` pointed at another
    household's `Account` (an inconsistent-evidence state; never produced by
    this module's own writes, but not proven impossible either) could still
    be accepted here and have its `account.account_type`/`account.name`
    read and acted on as if it were this household's. Requiring
    `transaction.account.household_id == household_id` here -- the same
    account-isolation check `_verified_reconciliation_counterpart` runs on
    the *read* side -- closes that gap on the *write* side too, so the two
    paths agree: a pair the read model would refuse to ever report as
    `paid`/`linked` is never one the write path can create, confirm-unlink,
    or otherwise act on. Failing with `LookupError` (identical to "row not
    found") is deliberate: no code path here ever mutates `linked_
    transaction_id`, reveals account metadata, or "fixes" the inconsistency
    -- the caller sees the same 404 it would for a transaction id that does
    not exist at all.
    """
    from app.models import Account, Transaction

    query = (
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.id == transaction_id,
            Transaction.household_id == household_id,
            Account.household_id == household_id,
        )
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
    if not _is_card_payment_direction(checking_amount=checking.amount, card_amount=card.amount):
        # A human can override the date window (see the module docstring,
        # point 5) but not the documented sign relationship: this pair is
        # not a bank debit paired with a card payment-received line no
        # matter who confirms it.
        raise CardPaymentLinkError(
            "O lançamento da conta corrente precisa ser um débito (valor negativo) e o do "
            "cartão precisa ser um pagamento recebido (valor positivo)"
        )
    checking.linked_transaction_id = card.id
    card.linked_transaction_id = checking.id
    return checking, card


@dataclass(frozen=True, slots=True)
class CardInvoiceObligation:
    """One card-invoice ("fatura") viewed as a payable obligation -- go-live
    manual slice 3 (`docs/WORK_ORDER_MANUAL_PAYABLES_CARD_PAYMENT.md`,
    "Contas a pagar").

    `status` is derived from `Transaction.linked_transaction_id` through
    `_verified_reconciliation_counterpart`, the same canonical proof
    `list_card_payment_reconciliations` already uses -- never fabricated,
    never inferred from amount, date or any heuristic, and never a bare
    non-null check on the pointer alone:

    - `paid`: linked to a genuine, reciprocal, same-household checking-side
      reconciliation leg (whether that leg was imported from a bank
      statement or created by `pay_card_invoice` below) -- an actual
      quitação fact exists and has been verified, not merely pointed to.
    - `pending`: no link yet, or `linked_transaction_id` is set but does not
      resolve to such a counterpart (an inconsistent pointer proves no
      payment, so it is never surfaced as `paid`).

    This never buckets a pending invoice into "a vencer"/"vencida" the way
    `app/api.py::_obligation_timing` does for the generic `Obligation`
    model: no parser stores the invoice's printed "VENCIMENTO" date on the
    `Transaction` row (`_reference_date` in `app/services/importer.py` only
    keeps the invoice's *reference month*, folded into `booked_at`), so a
    due-date bucket here would fabricate a fact the data does not contain --
    exactly what the Work Order's acceptance criteria forbid ("sem inventar
    status ou saldos").
    """

    card_transaction_id: str
    status: str
    amount: Decimal
    booked_at: str
    competence: str | None
    description: str
    account: str
    paid_transaction_id: str | None = None
    paid_booked_at: str | None = None


def list_card_invoice_obligations(
    db: Session,
    *,
    household_id: str,
    period: str | None = None,
) -> list[CardInvoiceObligation]:
    """Read-only, deterministic list of card invoices as payable obligations.

    Reads exactly the population `list_card_payment_reconciliations` already
    treats as the invoice/payment-received side (`transaction_type ==
    "reconciliation"` on a `credit_card` account) -- no new query shape, no
    new financial calculation. `period` filters by the invoice's own
    `competence`; never mutates anything.
    """

    from app.models import Account, Transaction

    rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "reconciliation",
            Account.account_type == "credit_card",
            # BLOQUEIO DE MERGE #5 (2026-09-07): require the invoice's own
            # `Account` to belong to this household too -- `Transaction.
            # household_id` matching alone does not prove `Account.
            # household_id` does, and this row's `account.name` is
            # serialized below, so a corrupted cross-household account
            # reference must exclude the row entirely rather than leak a
            # foreign account's name into this household's "Contas a
            # pagar" list.
            Account.household_id == household_id,
        )
        .order_by(Transaction.booked_at.desc(), Transaction.id)
    ).all()
    if period:
        rows = [row for row in rows if row.competence == period]

    results: list[CardInvoiceObligation] = []
    for row in rows:
        # `paid` must mean a proven quitação fact, not merely a non-null
        # pointer -- see `_verified_reconciliation_counterpart` (go-live
        # manual slice 3 review round, "`paid` precisa significar quitação
        # comprovada, não apenas ponteiro não nulo"). A `linked_transaction_id`
        # that fails to resolve to a genuine, reciprocal, same-household
        # checking-side counterpart proves no payment -- it stays `pending`,
        # exactly like an invoice with no link at all, instead of leaking an
        # inconsistent or cross-household fact into this read model.
        paid_transaction = _verified_reconciliation_counterpart(
            db, household_id=household_id, row=row, counterpart_account_type="checking"
        )
        results.append(
            CardInvoiceObligation(
                card_transaction_id=row.id,
                status="paid" if paid_transaction is not None else "pending",
                amount=row.amount,
                booked_at=row.booked_at.isoformat(),
                competence=row.competence,
                description=row.description,
                account=row.account.name if row.account else "",
                paid_transaction_id=paid_transaction.id if paid_transaction is not None else None,
                paid_booked_at=paid_transaction.booked_at.isoformat() if paid_transaction is not None else None,
            )
        )
    return results


def serialize_card_invoice_obligation(item: CardInvoiceObligation) -> dict[str, Any]:
    return {
        "card_transaction_id": item.card_transaction_id,
        "status": item.status,
        "amount": str(item.amount),
        "date": item.booked_at,
        "competence": item.competence,
        "description": item.description,
        "account": item.account,
        "paid_transaction_id": item.paid_transaction_id,
        "paid_date": item.paid_booked_at,
    }


def _deterministic_bank_evidence_for_card(
    db: Session, *, household_id: str, card: Any, paying_account_id: str
) -> tuple[str, Any | None]:
    """Whether an already-observed, unlinked checking-side reconciliation row
    already evidences the payment of this specific card invoice.

    Go-live manual slice 3 review round (2026-09-07, `BLOQUEIO DE MERGE`):
    `pay_card_invoice` must never fabricate a second bank leg for a debit the
    household's bank statement already contains. This reuses the exact same
    global candidate graph `list_card_payment_reconciliations` computes
    (`_candidate_edges`/`_card_side_degrees`) instead of a second policy --
    same query shape, same amount/direction/window filter, same mutual
    1:1-isolated-edge rule for what counts as deterministic. The graph itself
    is always built over *every* checking account in the household (exactly
    like `list_card_payment_reconciliations`), never only `paying_account_id`
    -- narrowing the query would let a genuinely ambiguous card (candidates
    split across two accounts) look spuriously "unmatched" or "matched" from
    one account's point of view alone.

    Second review round (2026-09-07, `BLOQUEIO DE MERGE` #2): a deterministic
    global match is not, by itself, permission to link. `paying_account_id`
    is the household's explicit, human-confirmed fact for *which* account
    paid this invoice (Work Order: "conta pagadora ... é sempre confirmada
    pelo usuário"; never inferred). If the one deterministic candidate sits
    on a *different* checking account than the one the user just confirmed,
    honoring it would silently substitute the confirmed account with
    whatever the graph happens to name -- exactly the inference this feature
    must never perform, even though the pairing is otherwise unambiguous.

    Third review round (2026-09-07, `BLOQUEIO DE MERGE` #3): a deterministic
    1:1 candidate on the confirmed account is still not, by itself,
    permission to link. `POST /card-payment-reconciliations/pay`'s
    `confirmed=True` is the human's confirmation that *this invoice* is
    being paid from *this account* on *this date/amount* -- it is not proof
    the human has seen and confirmed *this specific already-imported bank
    row* as the matching lineage fact, which is exactly the confirmation
    `link_card_payment`'s own contract (and its dedicated `POST
    /card-payment-reconciliations/link` endpoint, which requires a textual
    `reason`) exists to capture. Concretely, `booked_at` is a mandatory,
    explicit field of the `/pay` request; silently linking a matched row
    whose own date can be up to `CARD_PAYMENT_MATCH_WINDOW_DAYS` away would
    let the response report success while the audited fact (the linked
    row's real date) silently diverges from the date the human just
    confirmed. So a `matched` candidate is treated the same as
    `wrong_account` below: never auto-linked, always a fail-closed 409
    naming the candidate so the caller can resolve it explicitly.

    Returns:
    - `("unmatched", None)`: no checking row lists `card` as a candidate --
      no bank fact exists yet. The caller may create the manual leg.
    - `("matched", checking)`: exactly one checking row lists `card` as a
      candidate, that pairing is a mutual, isolated 1:1 edge (the same
      "matched" status `list_card_payment_reconciliations` would report for
      it), and that checking row belongs to `paying_account_id`. Still not
      auto-linked (see the third review round above) -- the caller must
      fail closed and require an explicit `POST
      /card-payment-reconciliations/link` confirmation naming this exact
      pair before the invoice can be considered settled.
    - `("wrong_account", checking)`: the same deterministic 1:1 match exists,
      but on a checking account other than `paying_account_id`. Never
      linked and never silently superseded by a new manual leg on the
      confirmed account (that would duplicate the very same real-world
      debit this check exists to catch) -- the caller must fail closed and
      require an explicit human decision.
    - `("ambiguous", None)`: any other shape -- more than one checking row
      claims this card, or the single one that does also has other card
      candidates. Never auto-resolved; the caller must require an explicit
      human decision (`POST /card-payment-reconciliations/link`) before a
      manual leg may be considered.
    """

    from app.models import Account, Transaction

    all_checking_rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "reconciliation",
            Account.account_type == "checking",
            # BLOQUEIO DE MERGE #5 (2026-09-07): same universe-building
            # query shape as `list_card_payment_reconciliations`/
            # `list_card_invoice_obligations` -- require the joined
            # `Account` to belong to this household too, so a row with a
            # corrupted cross-household account reference can never be
            # treated as this household's bank evidence for a payment.
            Account.household_id == household_id,
        )
    ).all()
    if not all_checking_rows:
        return "unmatched", None

    card_rows = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "reconciliation",
            Account.account_type == "credit_card",
            Account.household_id == household_id,
        )
    ).all()

    edges = _candidate_edges(all_checking_rows, card_rows)
    card_degrees = _card_side_degrees(edges)
    checking_by_id = {row.id: row for row in all_checking_rows}

    claimants = [
        checking_id
        for checking_id, candidates in edges.items()
        if any(candidate.id == card.id for candidate in candidates)
    ]
    if not claimants:
        return "unmatched", None
    if len(claimants) > 1:
        return "ambiguous", None
    checking = checking_by_id[claimants[0]]
    if len(edges[claimants[0]]) != 1 or card_degrees.get(card.id, 0) != 1:
        return "ambiguous", None
    if checking.account_id != paying_account_id:
        return "wrong_account", checking
    return "matched", checking


def pay_card_invoice(
    db: Session,
    *,
    household_id: str,
    card_transaction_id: str,
    paying_account: Any,
    category: Any,
    amount: Decimal,
    booked_at: Any,
    description: str,
) -> tuple[Any, Any, DuplicateAssessment | None]:
    """Manual payment of a card invoice -- go-live manual slice 3.

    `docs/WORK_ORDER_MANUAL_PAYABLES_CARD_PAYMENT.md`, "Pagamento manual de
    fatura": settles the invoice by reusing whatever bank fact already
    proves the debit happened, and only fabricates a new checking-side
    (bank-debit) reconciliation leg when no such fact exists yet.

    Review round (2026-09-07, `BLOQUEIO DE MERGE`): before this function ever
    builds a new `Transaction`, it asks
    `_deterministic_bank_evidence_for_card` -- the same global candidate
    graph `list_card_payment_reconciliations` already computes, not a second
    policy -- whether an already-imported, unlinked checking-side
    reconciliation row already evidences this exact debit:

    - `"matched"` (a mutual, isolated 1:1 candidate already exists *on the
      confirmed `paying_account`*): third review round (2026-09-07,
      `BLOQUEIO DE MERGE` #3) -- a generic `confirmed=True` on the payment
      request is confirmation of *the invoice being paid*, not of *this
      specific already-imported bank row being the matching lineage fact*
      (see `_deterministic_bank_evidence_for_card`'s docstring). Never
      auto-linked and never silently bypassed by fabricating a second leg
      either -- raises `CardPaymentLinkError` naming the candidate
      transaction and requiring the human to confirm that exact pair
      explicitly via `POST /card-payment-reconciliations/link` (which
      already requires a `reason`). Once linked that way, a repeat call to
      `/pay` for the same invoice hits the "already paid" guard below and
      creates nothing.
    - `"wrong_account"` (second review round, 2026-09-07, `BLOQUEIO DE
      MERGE` #2): the same deterministic 1:1 candidate exists, but on a
      checking account other than the one the user just confirmed as
      `paying_account`. Silently linking it would substitute the confirmed
      account with an inferred one; silently falling through to create a
      manual leg on `paying_account` would duplicate the very same
      real-world debit this check exists to catch. Neither is acceptable --
      raises `CardPaymentLinkError` naming the account holding the evidence
      and requiring the human to either correct `paying_account_id` or
      resolve it explicitly via `POST /card-payment-reconciliations/link`.
    - `"ambiguous"` (more than one candidate, on either side of the graph):
      never auto-resolved and never silently bypassed by fabricating a
      second leg. Raises `CardPaymentLinkError` -- a human must resolve the
      ambiguity explicitly via `POST /card-payment-reconciliations/link`
      (choosing the correct pair with a mandatory `reason`) before this
      invoice can be settled.
    - `"unmatched"` (no candidate at all): no bank fact exists yet, so this
      is the only case that reaches the leg-creation path below, exactly as
      before this review round.

    Reuses `link_card_payment`'s exact tolerance/direction contract instead
    of inventing a second one: the amount paid must match the invoice's own
    amount within `RECONCILIATION_TOLERANCE` (so partial payment is
    rejected, not silently accepted -- see the Work Order's "pagamento
    parcial" acceptance criterion, which requires an unambiguous existing
    contract or explicit out-of-scope rejection) and the same
    checking-debit/card-credit sign relationship `_is_card_payment_direction`
    already enforces. INV-002 holds by construction: the new leg's category
    is always "Conciliação" and it is always `excluded = True`, exactly like
    every other reconciliation row in this project -- it can never be
    counted as a new expense.

    The paying account must be an active `checking` account of this
    household: the only account type the existing reconciliation contract
    ever treats as the bank/debit side of a card payment
    (`list_card_payment_reconciliations`, `_is_card_payment_direction`).
    Widening that to other account types would be a second, looser
    direction policy, which this slice's Work Order forbids. (When the
    `"matched"` branch above applies, the already-imported leg's own
    account -- not `paying_account` -- is the historical fact; the
    validation above still runs because it is meaningful input validation
    independent of which branch resolves the payment.)

    Raises `LookupError` (-> 404) when `card_transaction_id` does not
    resolve to a reconciliation row of this household, `CardPaymentLinkError`
    (-> 409) for any business-rule conflict (not a credit-card invoice line,
    already paid, wrong direction, amount outside tolerance, inactive/foreign
    paying account, wrong account type, ambiguous bank evidence).

    Atomicity: like `create_internal_transfer`, nothing here calls
    `db.commit()` -- the caller commits once after this call (and, in the
    `"unmatched"` leg-creation path, its duplicate-detection pass) succeed,
    so there is no intermediate state with only the new leg persisted and no
    link, or vice versa.
    """

    import uuid

    from app.models import Transaction
    from app.services.classifier import normalize_description
    from app.services.duplicates import register_transaction_duplicates, source_priority
    from app.services.importer import PARSER_CONTRACT_VERSION, ParsedTransaction, transaction_fingerprint

    card = _reconciliation_transaction_or_404(
        db, household_id=household_id, transaction_id=card_transaction_id, for_update=True
    )
    card_account_type = card.account.account_type if card.account else None
    if card_account_type != "credit_card":
        raise CardPaymentLinkError(
            "O identificador informado não corresponde a uma fatura de cartão pendente de pagamento"
        )
    if card.linked_transaction_id:
        raise CardPaymentLinkError(
            "Esta fatura já está paga; desvincule o pagamento atual antes de registrar outro"
        )
    if paying_account.household_id != household_id:
        raise CardPaymentLinkError("A conta pagadora precisa pertencer a esta família")
    if not paying_account.active:
        raise CardPaymentLinkError("A conta pagadora precisa estar ativa")
    if paying_account.account_type != "checking":
        raise CardPaymentLinkError(
            "A conta pagadora precisa ser uma conta corrente; o contrato de conciliação de "
            "pagamento de fatura só reconhece débito bancário nesse tipo de conta"
        )

    positive_amount = _money(abs(amount))
    if positive_amount <= 0:
        raise CardPaymentLinkError("O valor pago deve ser maior que zero")
    if abs(positive_amount - abs(card.amount)) > RECONCILIATION_TOLERANCE:
        raise CardPaymentLinkError(
            "O valor pago não corresponde ao valor da fatura dentro da tolerância de R$ 0,01; "
            "pagamento parcial de fatura não é suportado por este contrato"
        )
    debit_amount = -positive_amount
    if not _is_card_payment_direction(checking_amount=debit_amount, card_amount=card.amount):
        raise CardPaymentLinkError(
            "A fatura selecionada não está no formato de pagamento recebido esperado para conciliação"
        )

    evidence_status, evidence_checking = _deterministic_bank_evidence_for_card(
        db, household_id=household_id, card=card, paying_account_id=paying_account.id
    )
    if evidence_status == "ambiguous":
        raise CardPaymentLinkError(
            "Existem lançamentos bancários já importados que podem corresponder a esta fatura, mas "
            "de forma ambígua; revise e vincule manualmente o lançamento correto em "
            "POST /card-payment-reconciliations/link antes de registrar um novo pagamento"
        )
    if evidence_status == "wrong_account":
        raise CardPaymentLinkError(
            "Já existe um lançamento bancário importado que corresponde a esta fatura, mas em "
            f"outra conta corrente (conta {evidence_checking.account_id}), não na conta pagadora "
            "confirmada; corrija a conta pagadora ou vincule explicitamente o lançamento correto "
            "em POST /card-payment-reconciliations/link antes de registrar um novo pagamento"
        )
    if evidence_status == "matched":
        # A bank-observed debit already proves this invoice was paid, but a
        # generic `confirmed=True` on this request is not the human's
        # explicit confirmation of *this specific* already-imported row as
        # the matching lineage fact (third review round, 2026-09-07,
        # `BLOQUEIO DE MERGE` #3 -- see the docstring above and
        # `_deterministic_bank_evidence_for_card`'s). Fail closed instead of
        # auto-linking or fabricating a second leg: the human must confirm
        # this exact pair explicitly via `POST
        # /card-payment-reconciliations/link` (which already requires a
        # `reason`) before the invoice can be considered settled.
        raise CardPaymentLinkError(
            "Existe um lançamento bancário já importado que corresponde a esta fatura na conta "
            f"pagadora confirmada (lançamento {evidence_checking.id}); confirme explicitamente esse "
            "vínculo em POST /card-payment-reconciliations/link (informando reason) antes de "
            "registrar o pagamento"
        )

    parsed = ParsedTransaction(
        booked_at=booked_at,
        description=description,
        amount=_money(debit_amount),
        source_line=1,
        card_last_four=paying_account.last_four,
    )
    checking_leg = Transaction(
        household_id=household_id,
        account_id=paying_account.id,
        category_id=category.id,
        booked_at=booked_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=_money(debit_amount),
        transaction_type="reconciliation",
        owner_label=paying_account.owner_label,
        card_last_four=paying_account.last_four,
        fingerprint=transaction_fingerprint(paying_account.id, parsed, paying_account.owner_label),
        occurred_at=booked_at,
        competence=booked_at.strftime("%Y-%m"),
        classification_source="manual_confirmed",
        classification_version=PARSER_CONTRACT_VERSION,
        canonical_status="unassigned",
        trace_id=str(uuid.uuid4()),
        source_priority=source_priority("manual"),
        confidence=Decimal("1"),
        excluded=True,
        possible_duplicate=False,
        reviewed=True,
    )
    db.add(checking_leg)
    db.flush()
    checking_leg.linked_transaction_id = card.id
    card.linked_transaction_id = checking_leg.id

    _, duplicate_assessment = register_transaction_duplicates(
        db, transaction=checking_leg, household_id=household_id
    )
    if duplicate_assessment is not None:
        checking_leg.reviewed = False

    return checking_leg, card, duplicate_assessment


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
