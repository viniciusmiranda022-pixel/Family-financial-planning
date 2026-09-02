"""Human-driven monthly close lifecycle over an already-computed snapshot/run.

This module never recomputes a financial fact. `run_monthly_close_execute`
delegates entirely to `financial_integrity.execute_integrity_run` and
`financial_snapshots.build_snapshot`; `trust_monthly_close` only reads
already-canonical fields (`consolidated_integrity_status`, the current
snapshot's `trusted_for_*` flags) to decide whether the deterministic gates
required by docs/INTEGRITY_IMPLEMENTATION_PLAN.md section 9.4 and the PR 7
Work Order are satisfied. See docs/INTEGRITY_IMPLEMENTATION_PLAN.md section
8.8 for the `monthly_financial_closes` contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinancialSnapshot, MonthlyFinancialClose
from app.services.financial_integrity import consolidated_integrity_status

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
) -> MonthlyFinancialClose:
    """Link a just-completed run/snapshot pair. Always leaves status `review_required`.

    A run never promotes a close straight to `trusted` on its own -- the Work
    Order (item 7) requires "executar checks", "confiar" and "reabrir" to
    stay separate, deliberate actions, even when the run comes back clean.
    """

    close = get_monthly_close(db, household_id=household_id, period=period)
    if close is None:
        close = MonthlyFinancialClose(household_id=household_id, period=period, status="open")
        db.add(close)
    close.status = "review_required"
    close.snapshot_id = snapshot_id
    close.integrity_run_id = integrity_run_id
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
    current_snapshot = _current_snapshot(db, household_id=household_id, period=period)
    if current_snapshot is None:
        reasons.append("Nenhum snapshot financeiro atual para o período.")
    elif close.snapshot_id != current_snapshot.id:
        reasons.append(
            "Os dados do período mudaram desde a última execução "
            "(existe um snapshot mais recente); execute o fechamento novamente."
        )

    status_now = consolidated_integrity_status(db, household_id=household_id, period=period)
    if status_now["status"] not in TRUSTABLE_INTEGRITY_STATUSES:
        reasons.append(
            f"Status de integridade do período é '{status_now['status']}', incompatível com trusted."
        )
    if current_snapshot is not None and not (
        current_snapshot.trusted_for_reports and current_snapshot.trusted_for_projection
    ):
        reasons.append(
            "O snapshot atual não está com trusted_for_reports e trusted_for_projection habilitados."
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
