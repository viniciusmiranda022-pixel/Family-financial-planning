"""Operational status/heartbeat for the CVM daily fund valuation worker
(issue #85, `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

Deliberately reuses `app.models.NotificationWorkerHeartbeat` -- the same
table MAIL-03 introduced -- rather than adding a second heartbeat table via
a new migration: the model is already generic operational bookkeeping (a
`worker_id` label plus `started_at`/`finished_at`/`ok`/`counts`/
`last_error_code`), not notification-specific, and its primary key is
already how `app.services.notification_worker_status` disambiguates "the"
row from any future one -- a second, distinct singleton id
(`_SINGLETON_ID` below) for this worker coexists safely in the same table
(confirmed: every read/write in that sibling module keys off its own
`_SINGLETON_ID` via `db.get(Model, id)`, never a bare "first row" query).

This module intentionally does **not** import
`app.services.notification_worker_status` and parametrize it -- copying the
few functions this worker actually needs (no `delivery_status_counts`
equivalent; a CVM valuation pass has no per-household delivery queue to
summarize) matches this codebase's established preference for a small,
independent module over a shared one threaded through with an extra
parameter (see `app.cli.notification_worker`'s own local `_audit`
reimplementation instead of importing `app.api.audit`, for the same reason:
small and explicit beats a cross-cutting abstraction for one caller).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import NotificationWorkerHeartbeat

_SINGLETON_ID = "cvm_valuation_worker_heartbeat"
_WORKER_LABEL = "cvm_valuation_worker"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _get_or_create(db: Session) -> NotificationWorkerHeartbeat:
    row = db.get(NotificationWorkerHeartbeat, _SINGLETON_ID)
    if row is not None:
        return row
    try:
        with db.begin_nested():
            row = NotificationWorkerHeartbeat(id=_SINGLETON_ID, worker_id=_WORKER_LABEL)
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        row = db.get(NotificationWorkerHeartbeat, _SINGLETON_ID)
        if row is None:
            raise
        return row


def mark_run_started(db: Session, *, now_utc: datetime | None = None) -> None:
    """Caller is responsible for `db.commit()`."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    row = _get_or_create(db)
    row.worker_id = _WORKER_LABEL
    row.started_at = now


def mark_run_finished(
    db: Session,
    *,
    ok: bool,
    counts: dict[str, int] | None,
    error_code: str | None,
    now_utc: datetime | None = None,
) -> None:
    """Caller is responsible for `db.commit()`."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    row = _get_or_create(db)
    row.finished_at = now
    row.ok = ok
    row.counts = counts
    row.last_error_code = error_code


def heartbeat_snapshot(db: Session) -> dict[str, Any] | None:
    row = db.get(NotificationWorkerHeartbeat, _SINGLETON_ID)
    if row is None or row.started_at is None:
        return None
    started_at = _aware_utc(row.started_at)
    finished_at = _aware_utc(row.finished_at)
    return {
        "last_started_at": started_at.isoformat(),
        "last_finished_at": finished_at.isoformat() if finished_at else None,
        "last_run_ok": row.ok,
        "last_error_code": row.last_error_code,
        "last_counts": row.counts,
    }


__all__ = ["mark_run_started", "mark_run_finished", "heartbeat_snapshot"]
