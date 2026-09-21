"""Due-date e-mail alert worker (MAIL-02/MAIL-03,
`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issues #69/#70).

Wires the pieces that are each individually owned elsewhere into the one
place the Work Order calls "processo/serviço separado": discovery, claim
and retry bookkeeping (`app.services.notification_scheduler`), rendering
(`app.services.notification_templates.render_due_date_alert`) and
transport (`app.services.email_delivery.SmtpEmailAdapter`). This module
adds no new decision logic of its own about *whether* to send -- it only
sequences calls into those modules and persists the outcome.

Two invocation shapes, matching the Work Order's "polling leve e barato"
worker plus this project's general preference (see
`app/cli/capture_worker.py`) for not adding a long-running daemon where a
single bounded pass will do:

    python -m app.cli.notification_worker --once
        One discovery+claim+send pass, then exit. What a cron/systemd-timer
        invocation, or a test, wants.

    python -m app.cli.notification_worker
        Loops `run_once()` forever, sleeping `--poll-seconds` between
        passes (default `notification_worker_poll_seconds`). What a
        dedicated long-running container/service wants. Restart-safe: a
        kill between passes loses nothing durable -- every delivery's
        state lives in `notification_deliveries`, and a `sending` row a
        killed pass abandoned is reclaimed by the next pass's stale check.

MAIL-03 wires the second invocation shape above into `compose.yaml` as
its own `notification-worker` service (see that file and
`docs/RUNBOOK_MAIL_ALERTS.md`) and adds the heartbeat this module writes
via `app.services.notification_worker_status` on every pass -- "última
execução"/"último erro sanitizado" for `GET /notification-settings/status`
to surface, per the Work Order's "Observabilidade" section.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AuditEvent, NotificationDelivery, NotificationRecipient, Obligation
from app.services import notification_scheduler as scheduler
from app.services import notification_worker_status as worker_status
from app.services.email_delivery import EmailDeliveryResult, OutboundEmail, SmtpEmailAdapter
from app.services.notification_templates import render_due_date_alert
from app.services.smtp_config import resolve_effective_smtp_settings

logger = logging.getLogger(__name__)


def _audit(
    db: Session,
    *,
    household_id: str,
    event_type: str,
    entity_id: str,
    details: dict[str, object] | None,
) -> None:
    """Same shape as `app.api.audit`, reimplemented locally with
    `user_id=None`: this is a system/background process, not a request
    acting on behalf of any household member, and `AuditEvent.user_id` is
    nullable exactly for cases like this (matches
    `app.services.card_competence_repair`'s own local reimplementation for
    the same circular-import reason -- `app.api` imports this package's
    siblings, so importing `app.api.audit` back from here would cycle).

    `details` must never carry a recipient's e-mail address or the SMTP
    payload -- only ids, the alert kind and a sanitized error code -- per
    the Work Order's "Privacidade e logs" section.
    """

    db.add(
        AuditEvent(
            household_id=household_id,
            user_id=None,
            event_type=event_type,
            entity_type="notification_delivery",
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            source="notification_worker",
        )
    )


def _cancel_reason_for_obligation(obligation: Obligation | None) -> str:
    if obligation is not None and obligation.status == "paid":
        return scheduler.CANCEL_REASON_PAID
    return scheduler.CANCEL_REASON_INACTIVE


def _process_claimed_delivery(
    db: Session,
    delivery: NotificationDelivery,
    *,
    adapter: SmtpEmailAdapter,
    max_attempts: int,
    retry_backoff_seconds: int,
    now_utc=None,
) -> tuple[str, str | None]:
    """Revalidates, sends (or cancels/retries/fails) exactly one already-
    claimed delivery, and audits the outcome. Returns `(outcome,
    error_code)`: `outcome` is one of `"sent"`, `"canceled"`,
    `"retry_scheduled"`, `"failed"`, or `"claim_lost"` (a concurrent
    reclaim/finalize won the row first -- see
    `app.services.notification_scheduler`'s CAS discipline); `error_code`
    is the sanitized `app.services.email_delivery.EmailErrorCode` (or
    cancellation reason) when the outcome is not `"sent"`/`"claim_lost"`,
    for `run_once` to surface as this pass's heartbeat
    `last_error_code` -- never a recipient address or raw SMTP text.

    Every branch below re-checks the canonical `Obligation`/
    `NotificationRecipient` fresh from `db` -- never trusts the state the
    discovery/claim pass observed earlier -- per the Work Order's
    "revalidar... imediatamente antes do envio".
    """

    if not scheduler.is_obligation_still_eligible(
        db, household_id=delivery.household_id, obligation_id=delivery.obligation_id
    ):
        obligation = db.scalar(
            select(Obligation).where(
                Obligation.id == delivery.obligation_id, Obligation.household_id == delivery.household_id
            )
        )
        reason = _cancel_reason_for_obligation(obligation)
        won = scheduler.finalize_canceled(
            db, delivery.id, claimed_attempt=delivery.attempt_count, reason_code=reason
        )
        if not won:
            db.rollback()
            return "claim_lost", None
        _audit(
            db,
            household_id=delivery.household_id,
            event_type="notification_delivery.canceled",
            entity_id=delivery.id,
            details={"alert_kind": delivery.alert_kind, "reason": reason},
        )
        db.commit()
        return "canceled", reason

    if not scheduler.is_recipient_still_eligible(
        db,
        household_id=delivery.household_id,
        recipient_id=delivery.recipient_id,
        alert_kind=delivery.alert_kind,
    ):
        won = scheduler.finalize_canceled(
            db,
            delivery.id,
            claimed_attempt=delivery.attempt_count,
            reason_code=scheduler.CANCEL_REASON_RECIPIENT_GONE,
        )
        if not won:
            db.rollback()
            return "claim_lost", None
        _audit(
            db,
            household_id=delivery.household_id,
            event_type="notification_delivery.canceled",
            entity_id=delivery.id,
            details={
                "alert_kind": delivery.alert_kind,
                "reason": scheduler.CANCEL_REASON_RECIPIENT_GONE,
            },
        )
        db.commit()
        return "canceled", scheduler.CANCEL_REASON_RECIPIENT_GONE

    recipient = db.scalar(
        select(NotificationRecipient).where(
            NotificationRecipient.id == delivery.recipient_id,
            NotificationRecipient.household_id == delivery.household_id,
        )
    )
    obligation = db.scalar(
        select(Obligation).where(
            Obligation.id == delivery.obligation_id, Obligation.household_id == delivery.household_id
        )
    )
    # Both were just proven present/eligible above in the same
    # transaction; `None` here would mean a concurrent hard-delete this
    # codebase never performs on either table (obligations/recipients are
    # deactivated/removed via their own soft paths, never dropped out from
    # under a live FK).
    rendered = render_due_date_alert(
        obligation_name=obligation.name,
        amount=obligation.amount,
        due_date=delivery.obligation_due_date,
        alert_kind=delivery.alert_kind,
        category=obligation.category,
    )
    result: EmailDeliveryResult = adapter.send(OutboundEmail(to_email=recipient.email, rendered=rendered))

    if result.ok:
        won = scheduler.finalize_sent(
            db,
            delivery.id,
            claimed_attempt=delivery.attempt_count,
            message_id=result.message_id,
            now_utc=now_utc,
        )
        if not won:
            db.rollback()
            return "claim_lost", None
        _audit(
            db,
            household_id=delivery.household_id,
            event_type="notification_delivery.sent",
            entity_id=delivery.id,
            details={"alert_kind": delivery.alert_kind, "recipient_id": delivery.recipient_id},
        )
        db.commit()
        return "sent", None

    won = scheduler.finalize_retry_or_fail(
        db,
        delivery.id,
        claimed_attempt=delivery.attempt_count,
        error_code=result.error_code or "smtp_send_failed",
        max_attempts=max_attempts,
        backoff_seconds=retry_backoff_seconds,
        now_utc=now_utc,
    )
    if not won:
        db.rollback()
        return "claim_lost", None
    # `populate_existing=True`: `finalize_retry_or_fail` issued a Core
    # `UPDATE`, and this same `delivery` instance is still tracked in this
    # session's identity map from the claim above -- force a fresh read of
    # the just-applied (not yet committed, but visible in this
    # transaction) status instead of depending on SQLAlchemy's
    # evaluate-synchronize heuristic to have kept it in sync. Same
    # defensive pattern `capture_worker.fail_exhausted_stale_jobs` uses
    # after its own Core update.
    refreshed = db.get(NotificationDelivery, delivery.id, populate_existing=True)
    outcome = "failed" if refreshed is not None and refreshed.status == scheduler.FAILED else "retry_scheduled"
    _audit(
        db,
        household_id=delivery.household_id,
        event_type=f"notification_delivery.{outcome}",
        entity_id=delivery.id,
        details={
            "alert_kind": delivery.alert_kind,
            "error_code": result.error_code,
            "attempt_count": delivery.attempt_count,
        },
    )
    db.commit()
    return outcome, result.error_code


def run_once(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    adapter: SmtpEmailAdapter | None = None,
    worker_id: str | None = None,
    limit: int = 100,
    now_utc=None,
) -> dict[str, int]:
    """One discovery+claim+send pass. Returns counts for observability and
    testing; never raises for an individual delivery's failure (captured
    on that delivery's own row, exactly like the capture-job worker).

    Every pass -- whether it completes normally or aborts on an unhandled
    exception -- records a `NotificationWorkerHeartbeat` row (MAIL-03,
    `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70,
    `app.services.notification_worker_status`): `started_at` before
    anything else runs, `finished_at`/`ok`/`counts`/`last_error_code` in a
    `finally` so a crash is recorded as `ok=False` instead of leaving the
    previous pass's result looking current forever.
    """

    settings = get_settings()
    worker_id = worker_id or scheduler.worker_identity()
    stale_after_seconds = settings.notification_delivery_stale_after_seconds

    with session_factory() as db:
        worker_status.mark_run_started(db, worker_id=worker_id, now_utc=now_utc)
        db.commit()

    counts = {
        "discovered": 0,
        "exhausted_failed": 0,
        "sent": 0,
        "canceled": 0,
        "retry_scheduled": 0,
        "failed": 0,
        "claim_lost": 0,
    }
    last_error_code: str | None = None
    ok = False
    try:
        with session_factory() as db:
            counts["exhausted_failed"] = scheduler.fail_exhausted_stale_deliveries(
                db, stale_after_seconds=stale_after_seconds, now_utc=now_utc
            )
            db.commit()
            counts["discovered"] = scheduler.discover_due_deliveries(db, now_utc=now_utc)
            db.commit()
            claimable_ids = scheduler.find_claimable_delivery_ids(
                db, limit=limit, stale_after_seconds=stale_after_seconds, now_utc=now_utc
            )

        if adapter is not None:
            active_adapter = adapter
        else:
            # MAIL-04: resolved fresh every pass (no caching) so a config
            # saved through the Settings UI mid-run takes effect on the
            # very next pass without a process restart -- the DB-vs-env
            # precedence decision itself lives in `resolve_effective_smtp_settings`,
            # never duplicated here.
            with session_factory() as db:
                active_adapter = SmtpEmailAdapter(settings=resolve_effective_smtp_settings(db))
        for delivery_id in claimable_ids:
            with session_factory() as db:
                delivery = scheduler.claim_delivery(
                    db,
                    delivery_id,
                    worker_id=worker_id,
                    stale_after_seconds=stale_after_seconds,
                    now_utc=now_utc,
                )
                if delivery is None:
                    counts["claim_lost"] += 1
                    continue
                outcome, error_code = _process_claimed_delivery(
                    db,
                    delivery,
                    adapter=active_adapter,
                    max_attempts=delivery.max_attempts,
                    retry_backoff_seconds=settings.notification_delivery_retry_backoff_seconds,
                    now_utc=now_utc,
                )
                counts[outcome] = counts.get(outcome, 0) + 1
                if error_code is not None:
                    last_error_code = error_code
        ok = True
    finally:
        with session_factory() as db:
            worker_status.mark_run_finished(
                db,
                ok=ok,
                counts=dict(counts),
                error_code=last_error_code if ok else (last_error_code or "worker_pass_exception"),
                now_utc=now_utc,
            )
            db.commit()

    logger.info("notification_worker_pass", extra=counts)
    return counts


def main(argv: list[str] | None = None) -> dict[str, int] | None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="Maximum deliveries to claim in one pass")
    parser.add_argument(
        "--once", action="store_true", help="Run a single pass and exit instead of looping forever"
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=settings.notification_worker_poll_seconds,
        help="Seconds to sleep between passes when not running --once",
    )
    args = parser.parse_args(argv)

    if args.once:
        return run_once(limit=args.limit)

    while True:
        run_once(limit=args.limit)
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    result = main()
    if result is not None:
        print(result)
