"""Canonical internal-transfer command -- go-live manual slice 2.

`docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md` requires a
transfer between two accounts of the same household to create both legs
atomically, with a deterministic/traceable link, and zero net operational
effect (INV-001). This module is the single place that builds those two
`Transaction` rows -- `app/api.py`'s structured `POST /transfers` endpoint
is the only caller today, but any future caller (e.g. a "Lançar agora"
flow that eventually knows both accounts) must reuse this function instead
of hand-building a second pair, so the two legs never drift apart.

It deliberately reuses the exact building blocks every other canonical
manual command in `app/api.py` already uses -- `transaction_fingerprint`,
`register_transaction_duplicates`, `source_priority`, `money` -- instead of
inventing a second financial engine or a second duplicate-detection policy.

Atomicity: nothing here calls `db.commit()`. The caller validates upfront
(distinct accounts, both active and in the same household, positive
amount) and commits once after both legs and their duplicate-detection
pass succeed. `app/db.py::get_db` closes the request's session without
committing on any unhandled exception, which rolls back everything added
in this call -- the same implicit all-or-nothing pattern
`create_manual_transaction`/`confirm_capture` already rely on. There is no
partial-leg state to guard against because there is no intermediate
commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Account, Category, Transaction
from app.services.classifier import normalize_description
from app.services.duplicates import DuplicateAssessment, register_transaction_duplicates, source_priority
from app.services.finance import money
from app.services.importer import PARSER_CONTRACT_VERSION, ParsedTransaction, transaction_fingerprint


class TransferError(ValueError):
    """A transfer request that violates an INV-001 precondition.

    Raised only as a last line of defense -- callers are expected to have
    already validated household isolation and account state with their own
    404/422 responses before reaching this function.
    """


@dataclass(frozen=True)
class TransferLegResult:
    transaction: Transaction
    duplicate_assessment: DuplicateAssessment | None


@dataclass(frozen=True)
class TransferResult:
    transfer_group_id: str
    debit: TransferLegResult
    credit: TransferLegResult

    @property
    def has_possible_duplicate(self) -> bool:
        return self.debit.duplicate_assessment is not None or self.credit.duplicate_assessment is not None


def create_internal_transfer(
    db: Session,
    *,
    household_id: str,
    from_account: Account,
    to_account: Account,
    category: Category,
    amount: Decimal,
    booked_at: date_type,
    description: str,
) -> TransferResult:
    """Create both legs of an internal transfer between two accounts.

    Returns the two persisted (flushed, not committed) `Transaction` rows
    plus whatever duplicate assessment `register_transaction_duplicates`
    produced for each -- the same signal `create_manual_transaction` already
    surfaces as a `ReviewItem` to the caller.
    """
    if from_account.id == to_account.id:
        raise TransferError("Conta de origem e destino devem ser diferentes")
    if from_account.household_id != household_id or to_account.household_id != household_id:
        raise TransferError("Conta de origem e destino devem pertencer à mesma família")
    positive_amount = money(abs(amount))
    if positive_amount <= 0:
        raise TransferError("O valor da transferência deve ser maior que zero")

    transfer_group_id = str(uuid.uuid4())
    debit = _build_leg(
        household_id=household_id,
        account=from_account,
        category=category,
        amount=-positive_amount,
        booked_at=booked_at,
        description=description,
        transfer_group_id=transfer_group_id,
    )
    credit = _build_leg(
        household_id=household_id,
        account=to_account,
        category=category,
        amount=positive_amount,
        booked_at=booked_at,
        description=description,
        transfer_group_id=transfer_group_id,
    )
    db.add(debit)
    db.add(credit)
    db.flush()
    # Reciprocal link in addition to the shared `transfer_group_id` --
    # mirrors the reciprocal `linked_transaction_id` pattern
    # `card_payment_reconciliation.py` already established for a related
    # pair of canonical transactions (INV-022 traceability).
    debit.linked_transaction_id = credit.id
    credit.linked_transaction_id = debit.id

    _, debit_duplicate = register_transaction_duplicates(db, transaction=debit, household_id=household_id)
    debit.reviewed = debit_duplicate is None
    _, credit_duplicate = register_transaction_duplicates(db, transaction=credit, household_id=household_id)
    credit.reviewed = credit_duplicate is None

    return TransferResult(
        transfer_group_id=transfer_group_id,
        debit=TransferLegResult(debit, debit_duplicate),
        credit=TransferLegResult(credit, credit_duplicate),
    )


def _build_leg(
    *,
    household_id: str,
    account: Account,
    category: Category,
    amount: Decimal,
    booked_at: date_type,
    description: str,
    transfer_group_id: str,
) -> Transaction:
    parsed = ParsedTransaction(
        booked_at=booked_at,
        description=description,
        amount=money(amount),
        source_line=1,
        card_last_four=account.last_four,
    )
    return Transaction(
        household_id=household_id,
        account_id=account.id,
        category_id=category.id,
        booked_at=booked_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=money(amount),
        transaction_type="transfer",
        owner_label=account.owner_label,
        card_last_four=account.last_four,
        fingerprint=transaction_fingerprint(account.id, parsed, account.owner_label),
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
        transfer_group_id=transfer_group_id,
    )
