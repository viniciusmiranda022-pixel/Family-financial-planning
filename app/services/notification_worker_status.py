"""Operational status/heartbeat for the due-date e-mail alert worker
(MAIL-03, `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70).

Two callers:

- `app.cli.notification_worker.run_once` writes the singleton heartbeat
  row at the start and end of every pass.
- `GET /notification-settings/status` (`app.api`) reads it, combined with
  `SmtpEmailAdapter.configured` and this household's own
  `NotificationDelivery` status counts, to answer the Work Order's
  "Observabilidade" checklist: envio desabilitado/sender não configurado/
  mensagem pendente/enviada/falhou/última execução do worker/último erro
  sanitizado.

Never exposes a secret or a recipient's e-mail address -- only ids,
booleans, timestamps and the same sanitized error codes
`app.services.email_delivery.EmailErrorCode` already defines. This module
never creates a `Transaction`/`Obligation` fact and never reads
`NotificationRecipient.email`; it is pure operational bookkeeping about
the worker process itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import NotificationDelivery, NotificationWorkerHeartbeat


def _aware_utc(value: datetime | None) -> datetime | None:
    """Same normalization `app.services.notification_scheduler._aware_utc`
    applies: PostgreSQL always returns an aware value for a
    `DateTime(timezone=True)` column; SQLite (tests) returns a naive one
    that this app only ever wrote as UTC to begin with. Without this, a
    timestamp read back from SQLite would serialize to an offset-less ISO
    string that a caller comparing it against an aware "now" (as
    `app.cli.notification_worker_healthcheck.check` does) cannot safely
    subtract."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)

# Fixed primary key, not `app.models.new_id()`: exactly one row must ever
# exist. Using a known constant (rather than "only ever insert once, by
# convention") means the primary key itself -- not application discipline
# -- is the barrier against two concurrent first-ever passes ("dois loops
# do worker rodando por erro operacional", Work Order "Concorrência e
# idempotência") each creating their own row.
_SINGLETON_ID = "notification_worker_heartbeat"
_WORKER_LABEL = "notification_worker"


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
        # Lost the race to a concurrent first-ever pass -- same
        # begin_nested()/IntegrityError discipline
        # `notification_scheduler.discover_due_deliveries` already uses for
        # its own unique-constraint race. The primary key guarantees the
        # other row exists now.
        row = db.get(NotificationWorkerHeartbeat, _SINGLETON_ID)
        if row is None:
            raise
        return row


def mark_run_started(db: Session, *, worker_id: str, now_utc: datetime | None = None) -> None:
    """Called once at the top of `run_once`, in its own short-lived
    session/transaction, before discovery starts. Leaves `finished_at`/
    `ok`/`counts`/`last_error_code` from the previous pass untouched --
    only `started_at`/`worker_id` move -- so the *last completed* result
    stays visible to an operator for the whole duration of the next pass
    instead of flashing to "unknown" the instant a new pass begins. A
    `started_at` that stays newer than `finished_at` for much longer than
    the configured poll interval is itself the "worker parado/travado"
    signal an operator (or a future container health check) can act on.

    Caller is responsible for `db.commit()`.
    """

    now = now_utc if now_utc is not None else datetime.now(UTC)
    row = _get_or_create(db)
    row.worker_id = worker_id
    row.started_at = now


def mark_run_finished(
    db: Session,
    *,
    ok: bool,
    counts: dict[str, int] | None,
    error_code: str | None,
    now_utc: datetime | None = None,
) -> None:
    """Called once at the very end of `run_once`, including from its
    exception handler when an unhandled error aborts the pass (`ok=False`,
    `error_code="worker_pass_exception"`) -- a crashed pass must never
    look, to an operator reading this row, like a pass that simply never
    ran. `counts` must already be the same ids/kind/error-code-only shape
    `run_once` returns -- never anything containing a recipient's e-mail
    address or SMTP payload.

    Caller is responsible for `db.commit()`.
    """

    now = now_utc if now_utc is not None else datetime.now(UTC)
    row = _get_or_create(db)
    row.finished_at = now
    row.ok = ok
    row.counts = counts
    row.last_error_code = error_code


def heartbeat_snapshot(db: Session) -> dict[str, Any] | None:
    """Read-only view for `GET /notification-settings/status`. Returns
    `None` if the worker has never completed a single pass yet (fresh
    install, migration just applied, container not started) -- the API
    surfaces that as an explicit "worker nunca executado" state, never as
    a fabricated success."""

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


_DELIVERY_STATUSES = ("pending", "sending", "sent", "failed", "canceled")


def delivery_status_counts(db: Session, *, household_id: str) -> dict[str, int]:
    """`pending`/`sending`/`sent`/`failed`/`canceled` counts scoped to this
    household only -- the Work Order's "entregas sent/failed"
    observability item, filtered by the same `household_id` isolation
    boundary every other read in this codebase applies."""

    rows = db.execute(
        select(NotificationDelivery.status, func.count())
        .where(NotificationDelivery.household_id == household_id)
        .group_by(NotificationDelivery.status)
    ).all()
    counts = dict(rows)
    return {status: counts.get(status, 0) for status in _DELIVERY_STATUSES}


__all__ = [
    "mark_run_started",
    "mark_run_finished",
    "heartbeat_snapshot",
    "delivery_status_counts",
]
