"""Outbox/scheduler for due-date e-mail alerts: discovery, claim,
retry/backoff and cancellation-on-payment (MAIL-02,
`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #69).

This module only *observes* the canonical `Obligation` (`household_id`,
`active`, `status`, `due_date`) to decide whether a D-1/D0 alert event
exists; it never creates a `Transaction`, never edits `Obligation`, never
marks anything paid, and never recalculates a projection or recurrence --
those all remain exclusively owned by the financial engine and the
household's own actions. See the Work Order's "Regra fundamental" for the
enumerated list of things a worker built on this module must never do.

It also never sends anything itself: `app.services.email_delivery` is the
only SMTP transport and `app.services.notification_templates` is the only
renderer. The eventual caller (`app.cli.notification_worker`) is what wires
claim -> revalidate -> render -> send -> finalize together; this module
only owns the `NotificationDelivery` row's lifecycle.

Concurrency/idempotency design (mirrors
`app.services.capture_worker.claim_capture_job`/`finalize_capture_job`
deliberately, not by accident -- same single-PostgreSQL-instance,
no-Redis constraint, see that module's docstring):

- Discovery inserts a `pending` row per eligible
  (household, obligation, recipient, obligation_due_date, alert_kind)
  tuple, guarded by `uq_notification_delivery_event`
  (`app.models.NotificationDelivery`). A concurrent discovery race is
  handled with `db.begin_nested()` + `IntegrityError`, the same pattern
  `app.services.financial_integrity._persist_finding` already uses for its
  own unique-fingerprint race.
- Claiming is a `UPDATE ... WHERE status = :observed AND attempt_count =
  :observed_attempt_count` compare-and-swap: `rowcount == 0` means another
  worker (or a concurrent reclaim) already won this row, and the caller
  must not process it.
- Finalizing (`sent`/`canceled`/retry-or-`failed`) is the same CAS against
  `(status = 'sending', attempt_count = claimed_attempt)`, so a lease that
  was superseded by a reclaim can never overwrite the newer attempt's
  result -- exactly the race `finalize_capture_job` closes.
- A `sent` row is never reopened by anything in this module.

Local calendar-date arithmetic uses `zoneinfo.ZoneInfo`, not a fixed UTC
offset table: `app.services.notification_settings` restricts
`NotificationSettings.timezone` to a curated whitelist specifically
because the base `python:3.12-slim` image does not ship the OS `tzdata`
package required for `zoneinfo` to resolve IANA names. This module closes
that gap properly instead of hand-rolling offsets (which would silently
go stale if Brazil ever reintroduces DST, as it has multiple times
historically) by depending on the `tzdata` PyPI package (pure-Python IANA
data, no OS package needed) -- the approach Python's own `zoneinfo` docs
recommend for exactly this "minimal base image" situation. See the
Work Order comment in `pyproject.toml`'s `tzdata` dependency line.
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import NotificationDelivery, NotificationRecipient, NotificationSettings, Obligation

ALERT_KIND_D1 = "d1"
ALERT_KIND_D0 = "d0"

PENDING = "pending"
SENDING = "sending"
SENT = "sent"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL_STATUSES = {SENT, FAILED, CANCELED}

# `app.services.email_delivery.EmailErrorCode` classification: permanent
# configuration/authentication/recipient problems must not enter an
# aggressive retry loop ("falhas permanentes de configuração/autenticação
# não entram em loop agressivo" -- Work Order); everything else is treated
# as transient and gets bounded retry with backoff.
_PERMANENT_ERROR_CODES = frozenset({"not_configured", "smtp_auth_failed", "smtp_recipient_refused"})

CANCEL_REASON_PAID = "canceled_obligation_paid"
CANCEL_REASON_INACTIVE = "canceled_obligation_inactive"
CANCEL_REASON_RECIPIENT_GONE = "canceled_recipient_unavailable"


def worker_identity() -> str:
    """Same non-sensitive, observability-only label
    `capture_worker.worker_identity` uses -- never used for authorization
    or as a lock token, just recorded in `claimed_by` for diagnosis."""

    return f"{socket.gethostname()}:{os.getpid()}"


def _aware_utc(value: datetime | None) -> datetime | None:
    """Same normalization `app.services.capture_worker._aware_utc` applies:
    PostgreSQL always returns an aware value for a `DateTime(timezone=True)`
    column; SQLite (tests) returns a naive one that this app only ever
    wrote as UTC to begin with."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def local_now(timezone_name: str, *, now_utc: datetime | None = None) -> datetime:
    """The household's wall-clock time right now, in its configured
    timezone. `now_utc` is injectable for deterministic tests; production
    callers always omit it."""

    reference = now_utc if now_utc is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return reference.astimezone(ZoneInfo(timezone_name))


def local_today(timezone_name: str, *, now_utc: datetime | None = None) -> date:
    return local_now(timezone_name, now_utc=now_utc).date()


def _parse_send_time(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)


def send_time_reached(settings_row: NotificationSettings, *, now_utc: datetime | None = None) -> bool:
    """Whether today's configured daily send time has already passed in the
    household's local timezone -- eligibility condition 7 in the Work
    Order ("o horário mínimo de disparo do dia já foi alcançado"). Gating
    *discovery* on this (rather than only gating send) is what makes
    same-day catch-up after a restart work for free: whenever the worker
    next runs, "now" is simply later than the threshold, so discovery
    creates the row and the very same pass can claim and send it."""

    now_local = local_now(settings_row.timezone, now_utc=now_utc)
    hour, minute = _parse_send_time(settings_row.send_time_local)
    return (now_local.hour, now_local.minute) >= (hour, minute)


def _eligible_obligations(db: Session, *, household_id: str, today: date) -> list[tuple[Obligation, str]]:
    """Every `(obligation, alert_kind)` pair currently eligible for this
    household under the Work Order's "Regras de data": `due_date ==
    today` is D0, `due_date == today + 1 dia` is D1. Paid (`status ==
    "paid"`) or deactivated (`active is False`) obligations are excluded
    at the source -- the same `Obligation.active.is_(True)` /
    `Obligation.status == "paid"` predicate pair `app/api.py` already uses
    for "is this obligation settled", not a second definition of paid."""

    tomorrow = today + timedelta(days=1)
    rows = db.scalars(
        select(Obligation).where(
            Obligation.household_id == household_id,
            Obligation.active.is_(True),
            Obligation.status != "paid",
            Obligation.due_date.in_((today, tomorrow)),
        )
    ).all()
    result: list[tuple[Obligation, str]] = []
    for obligation in rows:
        alert_kind = ALERT_KIND_D0 if obligation.due_date == today else ALERT_KIND_D1
        result.append((obligation, alert_kind))
    return result


def discover_due_deliveries(db: Session, *, now_utc: datetime | None = None) -> int:
    """Creates one `pending` `NotificationDelivery` row per newly-eligible
    (obligation, recipient, alert_kind) tuple, across every household with
    alerts enabled. Safe to call repeatedly and from multiple concurrent
    workers: `uq_notification_delivery_event` is the actual barrier, this
    function's `begin_nested()`/`IntegrityError` handling just avoids a
    duplicate-key error surfacing to the caller when two passes race on
    the same tuple. Returns how many rows this call itself created (a
    concurrent racer's rows are not counted here, but are of course also
    real deliveries).

    Caller is responsible for `db.commit()`.
    """

    created = 0
    max_attempts = get_settings().notification_delivery_max_attempts
    household_settings = db.scalars(
        select(NotificationSettings).where(NotificationSettings.enabled.is_(True))
    ).all()
    for settings_row in household_settings:
        if not send_time_reached(settings_row, now_utc=now_utc):
            continue
        today = local_today(settings_row.timezone, now_utc=now_utc)
        pairs = _eligible_obligations(db, household_id=settings_row.household_id, today=today)
        if not pairs:
            continue
        recipients = db.scalars(
            select(NotificationRecipient).where(
                NotificationRecipient.household_id == settings_row.household_id,
                NotificationRecipient.active.is_(True),
            )
        ).all()
        for obligation, alert_kind in pairs:
            for recipient in recipients:
                wants_it = recipient.notify_d1 if alert_kind == ALERT_KIND_D1 else recipient.notify_d0
                if not wants_it:
                    continue
                try:
                    with db.begin_nested():
                        db.add(
                            NotificationDelivery(
                                household_id=settings_row.household_id,
                                obligation_id=obligation.id,
                                recipient_id=recipient.id,
                                obligation_due_date=obligation.due_date,
                                alert_kind=alert_kind,
                                status=PENDING,
                                max_attempts=max_attempts,
                            )
                        )
                        db.flush()
                    created += 1
                except IntegrityError:
                    # Already discovered (this pass or a concurrent one) --
                    # the unique constraint is what actually matters here.
                    continue
    return created


def find_claimable_delivery_ids(
    db: Session, *, limit: int, stale_after_seconds: int, now_utc: datetime | None = None
) -> list[str]:
    """Any `pending` row whose `next_attempt_at` has arrived (or was never
    set -- a first attempt), plus any `sending` row stuck past
    `stale_after_seconds` (its worker crashed) that still has attempts
    left. Mirrors `capture_worker.find_claimable_job_ids` exactly."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    rows = db.scalars(
        select(NotificationDelivery.id)
        .where(
            (
                (NotificationDelivery.status == PENDING)
                & (
                    NotificationDelivery.next_attempt_at.is_(None)
                    | (NotificationDelivery.next_attempt_at <= now)
                )
            )
            | (
                (NotificationDelivery.status == SENDING)
                & (NotificationDelivery.claimed_at.is_not(None))
                & (NotificationDelivery.claimed_at < stale_cutoff)
                & (NotificationDelivery.attempt_count < NotificationDelivery.max_attempts)
            )
        )
        .order_by(NotificationDelivery.created_at)
        .limit(limit)
    ).all()
    return list(rows)


def claim_delivery(
    db: Session,
    delivery_id: str,
    *,
    worker_id: str,
    stale_after_seconds: int,
    now_utc: datetime | None = None,
) -> NotificationDelivery | None:
    """Atomically claim `delivery_id` if it is `pending` and due (or
    `sending` but abandoned past `stale_after_seconds`). Returns `None` if
    this call lost the race or the row is not currently claimable --
    mirrors `capture_worker.claim_capture_job`'s CAS discipline exactly,
    including why `attempt_count` (not just `status`) must be part of the
    predicate: the reclaim transition is `sending` -> `sending`, so
    `status` alone would let a second reclaimer match the same stale
    pre-image after the first one already committed."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    delivery = db.get(NotificationDelivery, delivery_id)
    if delivery is None or delivery.status in TERMINAL_STATUSES:
        return None
    claimed_at = _aware_utc(delivery.claimed_at)
    next_attempt_at = _aware_utc(delivery.next_attempt_at)
    claimable = (delivery.status == PENDING and (next_attempt_at is None or next_attempt_at <= now)) or (
        delivery.status == SENDING and (claimed_at is None or claimed_at < stale_cutoff)
    )
    if not claimable:
        return None
    observed_status = delivery.status
    observed_attempt_count = delivery.attempt_count
    result = db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == observed_status,
            NotificationDelivery.attempt_count == observed_attempt_count,
        )
        .values(
            status=SENDING,
            attempt_count=NotificationDelivery.attempt_count + 1,
            claimed_at=now,
            claimed_by=worker_id,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.refresh(delivery)
    return delivery


def is_obligation_still_eligible(db: Session, *, household_id: str, obligation_id: str) -> bool:
    """Revalidation "imediatamente antes do envio": re-reads the canonical
    `Obligation` fresh and returns whether it is still active and unpaid.
    Never mutates it -- this is a read-only check the worker uses to decide
    whether to cancel instead of send."""

    obligation = db.scalar(
        select(Obligation).where(Obligation.id == obligation_id, Obligation.household_id == household_id)
    )
    if obligation is None:
        return False
    return bool(obligation.active) and obligation.status != "paid"


def is_recipient_still_eligible(
    db: Session, *, household_id: str, recipient_id: str | None, alert_kind: str
) -> bool:
    """A recipient may be deactivated or have its D-1/D0 preference toggled
    off after a delivery was queued for it; re-check right before sending
    so a preference change takes effect immediately instead of only for
    future events."""

    if recipient_id is None:
        return False
    recipient = db.scalar(
        select(NotificationRecipient).where(
            NotificationRecipient.id == recipient_id,
            NotificationRecipient.household_id == household_id,
        )
    )
    if recipient is None or not recipient.active:
        return False
    return recipient.notify_d1 if alert_kind == ALERT_KIND_D1 else recipient.notify_d0


def finalize_sent(
    db: Session,
    delivery_id: str,
    *,
    claimed_attempt: int,
    message_id: str | None,
    now_utc: datetime | None = None,
) -> bool:
    """Marks `delivery_id` `sent`, only if it is still the exact attempt
    this caller claimed -- see the module docstring for why the CAS on
    `attempt_count` matters. Returns whether this call actually won."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    result = db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == SENDING,
            NotificationDelivery.attempt_count == claimed_attempt,
        )
        .values(status=SENT, sent_at=now, message_id=message_id, last_error_code=None)
    )
    return result.rowcount == 1


def finalize_canceled(
    db: Session, delivery_id: str, *, claimed_attempt: int, reason_code: str
) -> bool:
    """Marks `delivery_id` `canceled` -- the obligation was paid/deactivated,
    or the recipient became ineligible, between discovery/claim and send.
    Never touches the obligation or any payment; purely this row's own
    terminal state (Work Order, "Cancelamento por pagamento")."""

    result = db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == SENDING,
            NotificationDelivery.attempt_count == claimed_attempt,
        )
        .values(status=CANCELED, last_error_code=reason_code)
    )
    return result.rowcount == 1


def finalize_retry_or_fail(
    db: Session,
    delivery_id: str,
    *,
    claimed_attempt: int,
    error_code: str,
    max_attempts: int,
    backoff_seconds: int,
    now_utc: datetime | None = None,
) -> bool:
    """After a failed send attempt, decides between a bounded retry
    (back to `pending` with an exponential-backoff `next_attempt_at`) and
    a terminal `failed`: permanent errors (bad/missing credentials, SMTP
    host not configured, recipient refused) never retry regardless of
    attempts left ("não entram em loop agressivo"); everything else
    retries until `claimed_attempt >= max_attempts`. A `sent` delivery is
    never reachable from here (the CAS predicate is `status == 'sending'`),
    so this can never un-send something already sent."""

    now = now_utc if now_utc is not None else datetime.now(UTC)
    permanent = error_code in _PERMANENT_ERROR_CODES
    if permanent or claimed_attempt >= max_attempts:
        values = {"status": FAILED, "last_error_code": error_code}
    else:
        delay = backoff_seconds * (2 ** (claimed_attempt - 1))
        values = {
            "status": PENDING,
            "last_error_code": error_code,
            "next_attempt_at": now + timedelta(seconds=delay),
        }
    result = db.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == SENDING,
            NotificationDelivery.attempt_count == claimed_attempt,
        )
        .values(**values)
    )
    return result.rowcount == 1


def fail_exhausted_stale_deliveries(
    db: Session, *, stale_after_seconds: int, now_utc: datetime | None = None
) -> int:
    """Explicitly fails any `sending` delivery stuck past
    `stale_after_seconds` that has already used all of its attempts, so it
    never sits in `sending` forever misrepresenting an in-flight worker.
    Terminal counterpart to `find_claimable_delivery_ids`, which stops
    offering such a row for reclaim once attempts are exhausted. Mirrors
    `capture_worker.fail_exhausted_stale_jobs`'s CAS-by-observed-attempts
    discipline, so a slow-but-alive worker that finishes and finalizes
    just after this runs still wins cleanly instead of being clobbered.

    Caller is responsible for `db.commit()`.
    """

    now = now_utc if now_utc is not None else datetime.now(UTC)
    cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    stuck = db.execute(
        select(NotificationDelivery.id, NotificationDelivery.attempt_count).where(
            NotificationDelivery.status == SENDING,
            NotificationDelivery.claimed_at.is_not(None),
            NotificationDelivery.claimed_at < cutoff,
            NotificationDelivery.attempt_count >= NotificationDelivery.max_attempts,
        )
    ).all()
    failed_count = 0
    for delivery_id, observed_attempt_count in stuck:
        result = db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == delivery_id,
                NotificationDelivery.status == SENDING,
                NotificationDelivery.attempt_count == observed_attempt_count,
            )
            .values(status=FAILED, last_error_code="worker_timeout_exhausted")
        )
        if result.rowcount == 1:
            failed_count += 1
    return failed_count


__all__ = [
    "ALERT_KIND_D0",
    "ALERT_KIND_D1",
    "PENDING",
    "SENDING",
    "SENT",
    "FAILED",
    "CANCELED",
    "CANCEL_REASON_PAID",
    "CANCEL_REASON_INACTIVE",
    "CANCEL_REASON_RECIPIENT_GONE",
    "worker_identity",
    "local_now",
    "local_today",
    "send_time_reached",
    "discover_due_deliveries",
    "find_claimable_delivery_ids",
    "claim_delivery",
    "is_obligation_still_eligible",
    "is_recipient_still_eligible",
    "finalize_sent",
    "finalize_canceled",
    "finalize_retry_or_fail",
    "fail_exhausted_stale_deliveries",
]
