"""Human-driven monthly close lifecycle over an already-computed snapshot/run.

This module never invents or alters a financial fact -- every number it acts
on comes from `financial_integrity.execute_integrity_run` and
`financial_snapshots.build_snapshot`, the same deterministic engines every
other consumer uses. `trust_monthly_close` does call `build_snapshot` itself
(a checksum-stable, idempotent recompute -- see its call site below), but
only to detect whether the period's source data moved since `run`; it never
applies a different formula or accepts a fact `build_snapshot` did not
produce. The decision itself still reads only already-canonical fields
(`consolidated_integrity_status`, the current snapshot's `trusted_for_*`
flags) to decide whether the deterministic gates required by
docs/INTEGRITY_IMPLEMENTATION_PLAN.md section 9.4 and the PR 7 Work Order are
satisfied. See docs/INTEGRITY_IMPLEMENTATION_PLAN.md section 8.8 for the
`monthly_financial_closes` contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinancialSnapshot, MonthlyFinancialClose
from app.services.financial_integrity import consolidated_integrity_status
from app.services.financial_revision import (
    current_household_financial_revision,
    lock_household_financial_revision,
)
from app.services.financial_snapshots import build_snapshot

MONTHLY_CLOSE_STATUSES = ("open", "review_required", "trusted")

# Consolidated integrity statuses compatible with `trusted`: `healthy` (every
# applicable check passed) and `attention` (only WARNING-level, already
# quantified deviations -- see docs/INTEGRITY_IMPLEMENTATION_PLAN.md section
# 14.2). `unknown`, `review_required`, `critical` and `blocked` all represent
# an unproven or material pendency and must keep the close in
# `review_required` per the PR 7 Work Order acceptance criteria.
TRUSTABLE_INTEGRITY_STATUSES = frozenset({"healthy", "attention"})


class MonthlyCloseStateError(ValueError):
    """Raised when a monthly close action is attempted from an invalid state."""


class MonthlyCloseGateError(ValueError):
    """Raised when `trust` is attempted while a deterministic gate is open."""

    def __init__(self, message: str, *, reasons: tuple[str, ...]):
        super().__init__(message)
        self.reasons = reasons


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def get_monthly_close(
    db: Session, *, household_id: str, period: str
) -> MonthlyFinancialClose | None:
    return db.scalar(
        select(MonthlyFinancialClose).where(
            MonthlyFinancialClose.household_id == household_id,
            MonthlyFinancialClose.period == period,
        )
    )


def _current_snapshot(db: Session, *, household_id: str, period: str) -> FinancialSnapshot | None:
    return db.scalar(
        select(FinancialSnapshot).where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.period == period,
            FinancialSnapshot.snapshot_kind == "actual",
            FinancialSnapshot.status == "current",
        )
    )


def monthly_close_pending_summary(db: Session, *, household_id: str, period: str) -> dict[str, Any]:
    """Live read of why a period is/isn't ready for `trusted` -- never persisted."""

    status_now = consolidated_integrity_status(db, household_id=household_id, period=period)
    snapshot = _current_snapshot(db, household_id=household_id, period=period)
    return {
        "integrity_status": status_now["status"],
        "integrity_score": status_now.get("score"),
        "open_findings": status_now.get("open_findings", 0),
        "open_findings_by_severity": status_now.get("open_findings_by_severity", {}),
        "trusted_for_reports": bool(status_now.get("trusted_for_reports")),
        "trusted_for_projection": bool(status_now.get("trusted_for_projection")),
        "current_snapshot_id": snapshot.id if snapshot else None,
        "current_snapshot_trusted_for_reports": bool(snapshot.trusted_for_reports) if snapshot else False,
        "current_snapshot_trusted_for_projection": (
            bool(snapshot.trusted_for_projection) if snapshot else False
        ),
        "eligible_for_trust": status_now["status"] in TRUSTABLE_INTEGRITY_STATUSES
        and snapshot is not None
        and bool(snapshot.trusted_for_reports)
        and bool(snapshot.trusted_for_projection),
    }


def serialize_monthly_close(
    close: MonthlyFinancialClose | None, *, db: Session, household_id: str, period: str
) -> dict[str, Any]:
    pending = monthly_close_pending_summary(db, household_id=household_id, period=period)
    if close is None:
        return {
            "id": None,
            "period": period,
            "status": "open",
            "snapshot_id": None,
            "integrity_run_id": None,
            "closed_at": None,
            "closed_by": None,
            "reopened_at": None,
            "reopened_by": None,
            "reason": None,
            "pending": pending,
        }
    return {
        "id": close.id,
        "period": close.period,
        "status": close.status,
        "snapshot_id": close.snapshot_id,
        "integrity_run_id": close.integrity_run_id,
        "closed_at": _iso(close.closed_at),
        "closed_by": close.closed_by,
        "reopened_at": _iso(close.reopened_at),
        "reopened_by": close.reopened_by,
        "reason": close.reason,
        "created_at": _iso(close.created_at),
        "updated_at": _iso(close.updated_at),
        "pending": pending,
    }


def upsert_monthly_close_after_run(
    db: Session,
    *,
    household_id: str,
    period: str,
    snapshot_id: str,
    integrity_run_id: str,
    financial_revision: int | None = None,
) -> MonthlyFinancialClose:
    """Link a just-completed run/snapshot pair. Always leaves status `review_required`.

    A run never promotes a close straight to `trusted` on its own -- the Work
    Order (item 7) requires "executar checks", "confiar" and "reabrir" to
    stay separate, deliberate actions, even when the run comes back clean.

    `financial_revision` (PR 7, Round 8) should be the household's
    `HouseholdFinancialRevision.revision` as of the end of the run that
    produced `integrity_run_id`/`snapshot_id`, captured under
    `lock_household_financial_revision` -- see
    `MonthlyFinancialClose.financial_revision`'s docstring and
    `trust_monthly_close`'s use of it. Defaults to `None` (no comparison at
    trust time) for callers that manufacture a close outside the real `run`
    endpoint -- see the parameter's docstring on the model.
    """

    close = get_monthly_close(db, household_id=household_id, period=period)
    if close is None:
        close = MonthlyFinancialClose(household_id=household_id, period=period, status="open")
        db.add(close)
    close.status = "review_required"
    close.snapshot_id = snapshot_id
    close.integrity_run_id = integrity_run_id
    close.financial_revision = financial_revision
    db.flush()
    return close


def assert_close_runnable(db: Session, *, household_id: str, period: str) -> None:
    close = get_monthly_close(db, household_id=household_id, period=period)
    if close is not None and close.status == "trusted":
        raise MonthlyCloseStateError(
            "Fechamento já está trusted; reabra (reopen) antes de executar novamente."
        )


def trust_monthly_close(
    db: Session, *, household_id: str, period: str, user_id: str
) -> MonthlyFinancialClose:
    close = get_monthly_close(db, household_id=household_id, period=period)
    if close is None or close.status == "open":
        raise MonthlyCloseStateError(
            "Execute o fechamento (POST .../run) antes de marcar como trusted."
        )
    if close.status == "trusted":
        raise MonthlyCloseStateError("Fechamento já está trusted.")

    reasons: list[str] = []

    # Serialize this trust transition with every financial-source mutation
    # that can affect the period's snapshot -- see
    # `HouseholdFinancialRevision`'s docstring (`app/models.py`) and the
    # engineering review on PR 7, Round 7: "source mutation endpoints do not
    # share the snapshot advisory lock ... a concurrent transaction can
    # commit after snapshot source reads but before close.status='trusted'
    # commits." On PostgreSQL, this blocks until any in-flight mutation
    # transaction for this household commits or rolls back (and blocks any
    # such mutation that starts afterward, until this transaction ends), so
    # the rebuild below and the `trusted` write at the end of this function
    # cannot be interleaved by a concurrent source mutation. `revision_at_lock`
    # is compared again immediately before the final `trusted` write -- a
    # same-transaction check that is a no-op-by-construction on PostgreSQL
    # (nothing could have bumped it while the lock is held) and the actual
    # enforcement mechanism on SQLite, where the lock above is a no-op --
    # see `lock_household_financial_revision`'s docstring.
    revision_at_lock = lock_household_financial_revision(db, household_id=household_id)

    # Require the revision this close's *run* observed to still match the
    # one just locked -- closes the gap the revision barrier above and the
    # snapshot-identity check below still miss together: a source mutation
    # that changes what a *different* period's projection/gate would
    # recompute to (e.g. editing `installment_total` on a transaction booked
    # in an earlier period, which changes `_future_installments()`'s
    # next-month projection) bumps `HouseholdFinancialRevision` without
    # necessarily changing *this* period's own snapshot checksum, so the
    # rebuild below stays a no-op and the run's stored `healthy` status is
    # never re-evaluated. `close.financial_revision` is the revision `run`
    # itself locked and observed (see `MonthlyFinancialClose`'s docstring);
    # if the barrier's current value has moved since, the run's gates were
    # evaluated against data that is no longer current, and this trust
    # attempt must be rejected -- exactly the case a
    # `financial_revision is None` close (created before this column
    # existed, or manufactured directly by a test) cannot be judged on, so
    # it is skipped rather than unconditionally blocked. See the
    # engineering review on PR 7, Round 8: "Persist the revision used by
    # /run on the close/run and require it to equal the locked revision
    # before trusting."
    if close.financial_revision is not None and close.financial_revision != revision_at_lock:
        reasons.append(
            "Dados financeiros do household mudaram desde a última execução do "
            "fechamento (run); execute o fechamento novamente."
        )

    # Recompute the period's canonical snapshot before trusting it -- exactly
    # what `GET /dashboard`/`GET /reports` already do on every read via
    # `build_snapshot`. This is a no-op (returns the existing `current` row,
    # no new version) when nothing about the period's source data changed
    # since `run`. If a transaction, document, import or profile mutation
    # happened after `run` and before `trust` with no intervening rebuild,
    # comparing `close.snapshot_id` to a merely-read `_current_snapshot()`
    # would miss it entirely: nothing had rebuilt the "current" row yet, so
    # it would still equal `close.snapshot_id` despite the underlying data
    # having moved. Recomputing here is what actually detects that gap --
    # the checksum changes, a new `current` snapshot is created, and the
    # identity check below blocks `trust` on stale data instead of only
    # catching a divergence some unrelated read already happened to surface.
    # See the engineering review on PR 7 ("trust pode aceitar snapshot stale
    # após mutação de fonte") and its regression test.
    current_snapshot = build_snapshot(
        db, household_id=household_id, period=period, generated_by=user_id
    )
    if close.snapshot_id != current_snapshot.id:
        reasons.append(
            "Os dados do período mudaram desde a última execução "
            "(existe um snapshot mais recente); execute o fechamento novamente."
        )

    status_now = consolidated_integrity_status(db, household_id=household_id, period=period)
    if status_now["status"] not in TRUSTABLE_INTEGRITY_STATUSES:
        reasons.append(
            f"Status de integridade do período é '{status_now['status']}', incompatível com trusted."
        )
    if not (current_snapshot.trusted_for_reports and current_snapshot.trusted_for_projection):
        reasons.append(
            "O snapshot atual não está com trusted_for_reports e trusted_for_projection habilitados."
        )

    # Re-validate the barrier right before committing `trusted`: on
    # PostgreSQL this can never actually differ (the lock above has been
    # held continuously since before the rebuild, so no mutation could have
    # bumped the revision in between); on SQLite/tests, where the lock is a
    # no-op, this is the check that actually catches a mutation that slipped
    # in during this function's own execution -- see
    # `lock_household_financial_revision`'s docstring.
    revision_now = current_household_financial_revision(db, household_id=household_id)
    if revision_now != revision_at_lock:
        reasons.append(
            "Dados financeiros do household mudaram durante a validação de trusted; "
            "execute o fechamento novamente."
        )

    if reasons:
        raise MonthlyCloseGateError(
            "Gates determinísticos não satisfeitos para trusted.", reasons=tuple(reasons)
        )

    close.status = "trusted"
    close.closed_at = datetime.now(UTC)
    close.closed_by = user_id
    db.flush()
    return close


def reopen_monthly_close(
    db: Session, *, household_id: str, period: str, user_id: str, reason: str
) -> MonthlyFinancialClose:
    close = get_monthly_close(db, household_id=household_id, period=period)
    if close is None or close.status != "trusted":
        raise MonthlyCloseStateError("Somente um fechamento trusted pode ser reaberto.")
    close.status = "review_required"
    close.reopened_at = datetime.now(UTC)
    close.reopened_by = user_id
    close.reason = reason
    db.flush()
    return close


__all__ = [
    "MONTHLY_CLOSE_STATUSES",
    "TRUSTABLE_INTEGRITY_STATUSES",
    "MonthlyCloseGateError",
    "MonthlyCloseStateError",
    "assert_close_runnable",
    "get_monthly_close",
    "monthly_close_pending_summary",
    "reopen_monthly_close",
    "serialize_monthly_close",
    "trust_monthly_close",
    "upsert_monthly_close_after_run",
]
