"""Canonical, auditable, reversible repair engine for historically
mis-competenced card purchases.

`docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md` (PRIORIDADE 0): PR #81 fixed
the *creation* paths so a new card purchase always resolves `competence`
through `app.services.card_competence.card_invoice_competence` (the invoice's
canonical competence, derived from the account's own persisted
`card_closing_day`/`card_due_day`), never `booked_at`'s calendar month. This
module is the *historical* counterpart: production already contains card
purchases persisted before that hotfix whose `competence` still reflects the
old, incorrect `booked_at`-month behavior. It detects them, previews the
correction with no side effects, and applies only the explicitly
admin-selected, revision-guarded subset -- never a silent bulk backfill.

Design constraints from the Work Order, all enforced here:

- Single source of truth for the formula: this module never recomputes
  competence itself -- it always calls `card_invoice_competence`, the exact
  same function `app.api`'s manual-entry and Smart-Capture-confirmation
  paths call (see `app.services.card_competence`'s module docstring).
- Conservative, closed scope (section "Escopo seguro do detector
  histórico"): only `expense` transactions on a `credit_card` account with a
  *fully* configured cycle, a non-null persisted `competence`, and a
  classification source of `manual_confirmed` or `capture_confirmed` --
  the two paths PR #81 diagnosed as affected. Import, reconciliation,
  refund, card-payment, transfer and investment/redemption transactions are
  categorically out of scope; widening this list requires a Technical
  Challenge with concrete evidence, not a silent edit here.
- Preview is strictly read-only: `find_card_competence_candidates` and
  `preview_card_competence_repair` issue only `SELECT`s -- no `Transaction`,
  `FinancialSnapshot`, `MonthlyFinancialClose`, duplicate or document row is
  ever touched, and neither function ever calls `db.flush()`/`db.commit()`.
- Apply is admin-only (enforced by the caller in `app.api`, first action),
  explicit (the caller must name every `transaction_id`), race-safe (guarded
  by the same household-wide `HouseholdFinancialRevision` barrier every
  other financial-mutation endpoint in this codebase uses -- see
  `app.services.financial_revision`), atomic (one flush; the caller commits
  once at the very end or not at all, per `app.db.get_db`'s
  rollback-on-close-without-commit contract every other mutating endpoint in
  `app.api` already relies on), and fail-closed on duplicates
  (`possible_duplicate`, an open `DuplicateGroup`, or an already-settled
  `canonical`/`supporting` role -- any of which means the purchase's
  identity could be entangled with another transaction's, and a silent
  competence change could corrupt that unresolved/settled comparison) and on
  a `trusted` `MonthlyFinancialClose` covering either the old or the new
  competence period.
- Only `Transaction.competence` (and the ORM's own `updated_at`) ever
  changes. `booked_at`, `occurred_at`, `amount`, `fingerprint`,
  `duplicate_group_id`, `canonical_status`, and every other column the Work
  Order lists as "must remain preserved" are never written by this module.
- Every mutation is paired with an `AuditEvent`
  (`event_type="transaction.card_competence_repair"`) carrying
  `before_state`/`after_state`/`reason`/a shared batch `trace_id`, plus one
  aggregate batch event -- see `_write_audit_event` and
  `apply_card_competence_repair`. Rollback (`rollback_card_competence_repair`)
  reads that same audit trail back as the only source of truth for "what was
  the competence before this repair", never inferring it, and refuses to
  revert a transaction whose current `competence` no longer matches what the
  repair produced (a later, independent change) -- fail closed rather than
  silently overwrite it.
- Snapshot/period recomputation reuses the exact canonical path every other
  consumer already uses -- `app.services.financial_snapshots.build_snapshot`
  -- for every period touched (the old and new competence of every applied
  or reverted transaction), with `force=True` so no `current` snapshot for
  an affected period is ever left stale after a mutation in this module.
  `FinancialSnapshot`/lineage rows are never edited directly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AuditEvent, DuplicateGroup, Transaction
from app.services.card_competence import card_invoice_competence
from app.services.finance import money
from app.services.financial_revision import (
    current_household_financial_revision,
    lock_household_financial_revision,
)
from app.services.financial_snapshots import build_snapshot
from app.services.monthly_close import get_monthly_close

REPAIR_EVENT_TYPE = "transaction.card_competence_repair"
REPAIR_BATCH_EVENT_TYPE = "card_competence_repair.batch"
ROLLBACK_EVENT_TYPE = "transaction.card_competence_repair_rollback"
ROLLBACK_BATCH_EVENT_TYPE = "card_competence_repair.rollback_batch"
AUDIT_SOURCE = "card_competence_repair"

# See the module docstring, "Escopo seguro do detector histórico". These are
# the only two classification sources PR #81 diagnosed as ever producing a
# `booked_at`-month competence for a card purchase; every other source
# (import, capture-unconfirmed, legacy, etc.) is categorically excluded.
IN_SCOPE_CLASSIFICATION_SOURCES = ("manual_confirmed", "capture_confirmed")


class CardCompetenceRepairError(ValueError):
    """Base error for this engine. Callers (`app.api`) map subclasses to HTTP status."""


class StaleRevisionError(CardCompetenceRepairError):
    """The household's financial revision moved since the caller's preview/last read."""


class NoLongerCandidateError(CardCompetenceRepairError):
    """One or more requested transaction ids are not valid, in-scope, divergent
    candidates for this household right now (deleted, corrected already, a
    reference into another household, or never a candidate to begin with).
    Deliberately a single generic message for every one of those cases --
    see `apply_card_competence_repair`'s docstring, criterion "apply com id
    de outro household falha sem vazar existência"."""

    def __init__(self, message: str, *, transaction_ids: tuple[str, ...]):
        super().__init__(message)
        self.transaction_ids = transaction_ids


class BlockedCandidateError(CardCompetenceRepairError):
    """One or more requested transactions are fail-closed blocked (duplicate
    signal or a `trusted` monthly close over an affected period)."""

    def __init__(self, message: str, *, blocked: tuple[dict[str, Any], ...]):
        super().__init__(message)
        self.blocked = blocked


@dataclass(frozen=True, slots=True)
class CardCompetenceCandidate:
    transaction_id: str
    account_id: str
    account_name: str
    booked_at: date
    occurred_at: date | None
    amount: Decimal
    classification_source: str
    competence_current: str
    competence_expected: str
    blocked: bool
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairApplyResult:
    batch_id: str
    applied: tuple[dict[str, Any], ...]
    periods_recomputed: tuple[str, ...]
    financial_revision: int


@dataclass(frozen=True, slots=True)
class RepairRollbackResult:
    batch_id: str
    reverted: tuple[dict[str, Any], ...]
    periods_recomputed: tuple[str, ...]
    financial_revision: int


def _write_audit_event(
    db: Session,
    *,
    household_id: str,
    user_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Same shape as `app.api.audit`, reimplemented locally: `app.api` is the
    HTTP layer and imports this service module, so importing `app.api.audit`
    back from here would be a circular import. Every field this writes
    matches what `app.api.audit` writes for every other endpoint."""

    db.add(
        AuditEvent(
            household_id=household_id,
            user_id=user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            before_state=json.loads(json.dumps(before_state, ensure_ascii=False, default=str))
            if before_state is not None
            else None,
            after_state=json.loads(json.dumps(after_state, ensure_ascii=False, default=str))
            if after_state is not None
            else None,
            reason=reason,
            trace_id=trace_id,
            source=AUDIT_SOURCE,
        )
    )


def _candidate_rows(db: Session, *, household_id: str) -> list[tuple[Transaction, Account]]:
    return list(
        db.execute(
            select(Transaction, Account)
            .join(Account, Account.id == Transaction.account_id)
            .where(
                Transaction.household_id == household_id,
                Account.household_id == household_id,
                Transaction.transaction_type == "expense",
                Account.account_type == "credit_card",
                Account.card_closing_day.is_not(None),
                Account.card_due_day.is_not(None),
                Transaction.competence.is_not(None),
                Transaction.classification_source.in_(IN_SCOPE_CLASSIFICATION_SOURCES),
            )
            .order_by(Transaction.booked_at, Transaction.created_at, Transaction.id)
        ).all()
    )


def _blocked_reasons(
    db: Session,
    *,
    household_id: str,
    transaction: Transaction,
    periods: tuple[str, str],
) -> tuple[str, ...]:
    """Fail-closed predicate. See the module docstring's "fail-closed on
    duplicates ... and on a trusted MonthlyFinancialClose" bullet."""

    reasons: list[str] = []
    if transaction.possible_duplicate:
        reasons.append("possible_duplicate")
    if transaction.canonical_status in {"canonical", "supporting"}:
        reasons.append(f"duplicate_role:{transaction.canonical_status}")
    if transaction.duplicate_group_id:
        group = db.get(DuplicateGroup, transaction.duplicate_group_id)
        if group is not None and group.status == "open":
            reasons.append("duplicate_group_open")
    for period in dict.fromkeys(periods):  # de-dup, preserve order
        close = get_monthly_close(db, household_id=household_id, period=period)
        if close is not None and close.status == "trusted":
            reasons.append(f"monthly_close_trusted:{period}")
    return tuple(reasons)


def find_card_competence_candidates(
    db: Session, *, household_id: str
) -> list[CardCompetenceCandidate]:
    """Read-only. Never flushes, never mutates a `Transaction`, `Account`,
    `DuplicateGroup` or `MonthlyFinancialClose` row."""

    candidates: list[CardCompetenceCandidate] = []
    for transaction, account in _candidate_rows(db, household_id=household_id):
        expected = card_invoice_competence(account, transaction.booked_at)
        current = transaction.competence
        if expected == current:
            continue
        reasons = _blocked_reasons(
            db,
            household_id=household_id,
            transaction=transaction,
            periods=(current, expected),
        )
        candidates.append(
            CardCompetenceCandidate(
                transaction_id=transaction.id,
                account_id=account.id,
                account_name=account.name,
                booked_at=transaction.booked_at,
                occurred_at=transaction.occurred_at,
                amount=money(transaction.amount),
                classification_source=transaction.classification_source,
                competence_current=current,
                competence_expected=expected,
                blocked=bool(reasons),
                blocked_reasons=reasons,
            )
        )
    return candidates


def preview_card_competence_repair(db: Session, *, household_id: str) -> dict[str, Any]:
    """Read-only preview: candidates, affected periods and the household's
    current financial revision (for the caller's subsequent `apply`'s
    concurrency control) -- never a mutation, never a commit."""

    candidates = find_card_competence_candidates(db, household_id=household_id)
    periods_affected = sorted(
        {period for candidate in candidates for period in (candidate.competence_current, candidate.competence_expected)}
    )
    return {
        "financial_revision": current_household_financial_revision(db, household_id=household_id),
        "candidates": candidates,
        "periods_affected": periods_affected,
        "eligible_count": sum(1 for candidate in candidates if not candidate.blocked),
        "blocked_count": sum(1 for candidate in candidates if candidate.blocked),
    }


def apply_card_competence_repair(
    db: Session,
    *,
    household_id: str,
    transaction_ids: tuple[str, ...],
    expected_financial_revision: int,
    reason: str,
    user_id: str,
) -> RepairApplyResult:
    """Admin-only (caller's responsibility, first action before this is ever
    called), explicit, race-safe, atomic. Raises without mutating anything on
    any failure -- see the module docstring's "Apply is admin-only ..."
    bullet for the full contract each raised exception enforces.
    """

    if not transaction_ids:
        raise CardCompetenceRepairError("Nenhuma transação selecionada.")
    unique_ids = tuple(dict.fromkeys(transaction_ids))

    # First action: take the household-wide revision barrier, the same one
    # `run`/`trust`/`reopen` monthly-close transitions and every other
    # financial-mutation endpoint in this codebase take as their own first
    # action -- see `app.services.financial_revision` and
    # `app.services.monthly_close.assert_close_runnable`'s docstring for why
    # this must happen before any state is read, not after.
    locked_revision = lock_household_financial_revision(db, household_id=household_id)
    if locked_revision != expected_financial_revision:
        raise StaleRevisionError(
            "A revisão financeira do household mudou desde o preview usado nesta "
            "requisição; gere um novo preview antes de aplicar."
        )

    # Reread every transaction for this household and recompute the expected
    # competence from the canonical service again -- never trust the
    # preview's snapshot of "candidate" as still true.
    candidates_by_id = {
        candidate.transaction_id: candidate
        for candidate in find_card_competence_candidates(db, household_id=household_id)
    }
    missing = tuple(tid for tid in unique_ids if tid not in candidates_by_id)
    if missing:
        raise NoLongerCandidateError(
            "Uma ou mais transações selecionadas não são mais candidatas válidas "
            "(id inexistente, de outro household, já corrigida ou fora do escopo).",
            transaction_ids=missing,
        )
    blocked = tuple(
        {"transaction_id": tid, "reasons": list(candidates_by_id[tid].blocked_reasons)}
        for tid in unique_ids
        if candidates_by_id[tid].blocked
    )
    if blocked:
        raise BlockedCandidateError(
            "Uma ou mais transações selecionadas estão bloqueadas (duplicidade ou "
            "fechamento mensal trusted) e exigem resolução humana antes do reparo.",
            blocked=blocked,
        )

    batch_id = str(uuid.uuid4())
    applied: list[dict[str, Any]] = []
    periods: set[str] = set()
    for tid in unique_ids:
        candidate = candidates_by_id[tid]
        transaction = db.get(Transaction, tid)
        # Defensive re-check: `find_card_competence_candidates` already
        # scopes by household_id, so this can only fail if something else
        # deleted the row between the two reads in this same transaction.
        if transaction is None or transaction.household_id != household_id:
            raise NoLongerCandidateError(
                "Uma ou mais transações selecionadas não são mais candidatas válidas.",
                transaction_ids=(tid,),
            )
        before_competence = transaction.competence
        # Only `competence` (and the ORM's own `updated_at`) changes --
        # every other column listed in the Work Order stays untouched.
        transaction.competence = candidate.competence_expected
        periods.add(before_competence)
        periods.add(candidate.competence_expected)
        applied.append(
            {
                "transaction_id": tid,
                "account_id": candidate.account_id,
                "competence_before": before_competence,
                "competence_after": candidate.competence_expected,
            }
        )
        _write_audit_event(
            db,
            household_id=household_id,
            user_id=user_id,
            event_type=REPAIR_EVENT_TYPE,
            entity_type="transaction",
            entity_id=tid,
            before_state={"competence": before_competence},
            after_state={"competence": candidate.competence_expected},
            details={
                "account_id": candidate.account_id,
                "booked_at": candidate.booked_at.isoformat(),
                "classification_source": candidate.classification_source,
            },
            reason=reason,
            trace_id=batch_id,
        )

    _write_audit_event(
        db,
        household_id=household_id,
        user_id=user_id,
        event_type=REPAIR_BATCH_EVENT_TYPE,
        entity_type="card_competence_repair_batch",
        entity_id=batch_id,
        details={
            "transaction_ids": list(unique_ids),
            "count": len(unique_ids),
            "periods": sorted(periods),
        },
        reason=reason,
        trace_id=batch_id,
    )
    db.flush()

    # Recompute the canonical snapshot of every period this batch touched
    # (old and new competence of every applied transaction), the same
    # idempotent, checksum-guarded path `/dashboard`, `/reports` and
    # `/credit-cards/summary` already call on every read -- `force=True` so
    # no `current` row for an affected period is left stale even in the
    # (practically impossible) case the payload happened to checksum-match.
    for period in sorted(periods):
        build_snapshot(db, household_id=household_id, period=period, generated_by=user_id, force=True)

    return RepairApplyResult(
        batch_id=batch_id,
        applied=tuple(applied),
        periods_recomputed=tuple(sorted(periods)),
        financial_revision=current_household_financial_revision(db, household_id=household_id),
    )


def rollback_card_competence_repair(
    db: Session,
    *,
    household_id: str,
    transaction_ids: tuple[str, ...],
    expected_financial_revision: int,
    reason: str,
    user_id: str,
) -> RepairRollbackResult:
    """Logical rollback of a prior repair, using only the persisted
    `AuditEvent` audit trail as evidence of "what competence this
    transaction had before the repair" -- never inference, never a second
    competence calculation. Atomic and race-safe, mirroring
    `apply_card_competence_repair`; see its docstring and the module
    docstring's rollback bullet.
    """

    if not transaction_ids:
        raise CardCompetenceRepairError("Nenhuma transação selecionada para reverter.")
    unique_ids = tuple(dict.fromkeys(transaction_ids))

    locked_revision = lock_household_financial_revision(db, household_id=household_id)
    if locked_revision != expected_financial_revision:
        raise StaleRevisionError(
            "A revisão financeira do household mudou desde a leitura usada nesta "
            "requisição; releia o estado atual antes de reverter."
        )

    plan: dict[str, tuple[str, str]] = {}  # transaction_id -> (before_competence, current_competence)
    missing: list[str] = []
    for tid in unique_ids:
        transaction = db.scalar(
            select(Transaction).where(Transaction.id == tid, Transaction.household_id == household_id)
        )
        if transaction is None:
            missing.append(tid)
            continue
        last_repair = db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.household_id == household_id,
                AuditEvent.event_type == REPAIR_EVENT_TYPE,
                AuditEvent.entity_type == "transaction",
                AuditEvent.entity_id == tid,
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        )
        if (
            last_repair is None
            or not last_repair.before_state
            or "competence" not in last_repair.before_state
            or not last_repair.after_state
            or "competence" not in last_repair.after_state
        ):
            # No repair audit trail for this transaction -- nothing to
            # revert, and never inferred from current state.
            missing.append(tid)
            continue
        expected_current = last_repair.after_state["competence"]
        if transaction.competence != expected_current:
            # The transaction moved since the repair this audit trail
            # describes (a later repair, a later rollback, or some other
            # mutation) -- fail closed rather than overwrite that change.
            missing.append(tid)
            continue
        plan[tid] = (last_repair.before_state["competence"], expected_current)

    if missing:
        raise NoLongerCandidateError(
            "Uma ou mais transações selecionadas não têm um reparo revertível no "
            "estado atual (nunca reparada, já revertida, ou alterada desde o "
            "reparo).",
            transaction_ids=tuple(missing),
        )

    batch_id = str(uuid.uuid4())
    reverted: list[dict[str, Any]] = []
    periods: set[str] = set()
    for tid, (before_competence, current_competence) in plan.items():
        transaction = db.get(Transaction, tid)
        transaction.competence = before_competence
        periods.add(current_competence)
        periods.add(before_competence)
        reverted.append(
            {
                "transaction_id": tid,
                "competence_before_rollback": current_competence,
                "competence_after_rollback": before_competence,
            }
        )
        _write_audit_event(
            db,
            household_id=household_id,
            user_id=user_id,
            event_type=ROLLBACK_EVENT_TYPE,
            entity_type="transaction",
            entity_id=tid,
            before_state={"competence": current_competence},
            after_state={"competence": before_competence},
            reason=reason,
            trace_id=batch_id,
        )

    _write_audit_event(
        db,
        household_id=household_id,
        user_id=user_id,
        event_type=ROLLBACK_BATCH_EVENT_TYPE,
        entity_type="card_competence_repair_batch",
        entity_id=batch_id,
        details={
            "transaction_ids": list(plan.keys()),
            "count": len(plan),
            "periods": sorted(periods),
        },
        reason=reason,
        trace_id=batch_id,
    )
    db.flush()

    for period in sorted(periods):
        build_snapshot(db, household_id=household_id, period=period, generated_by=user_id, force=True)

    return RepairRollbackResult(
        batch_id=batch_id,
        reverted=tuple(reverted),
        periods_recomputed=tuple(sorted(periods)),
        financial_revision=current_household_financial_revision(db, household_id=household_id),
    )


def serialize_candidate(candidate: CardCompetenceCandidate) -> dict[str, Any]:
    return {
        "transaction_id": candidate.transaction_id,
        "account_id": candidate.account_id,
        "account_name": candidate.account_name,
        "booked_at": candidate.booked_at.isoformat(),
        "occurred_at": candidate.occurred_at.isoformat() if candidate.occurred_at else None,
        "amount": str(candidate.amount),
        "classification_source": candidate.classification_source,
        "competence_current": candidate.competence_current,
        "competence_expected": candidate.competence_expected,
        "blocked": candidate.blocked,
        "blocked_reasons": list(candidate.blocked_reasons),
    }


__all__ = [
    "IN_SCOPE_CLASSIFICATION_SOURCES",
    "REPAIR_BATCH_EVENT_TYPE",
    "REPAIR_EVENT_TYPE",
    "ROLLBACK_BATCH_EVENT_TYPE",
    "ROLLBACK_EVENT_TYPE",
    "BlockedCandidateError",
    "CardCompetenceCandidate",
    "CardCompetenceRepairError",
    "NoLongerCandidateError",
    "RepairApplyResult",
    "RepairRollbackResult",
    "StaleRevisionError",
    "apply_card_competence_repair",
    "find_card_competence_candidates",
    "preview_card_competence_repair",
    "rollback_card_competence_repair",
    "serialize_candidate",
]
