"""CVM daily fund valuation worker (issue #85,
`docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

Wires `app.services.cvm_client` (fetch the fund's latest official quota)
and `app.services.privilege_valuation` (idempotent ingestion + per-household
valuation) into the one daily pass the Work Order's "Scheduling" section
calls for: "checks for a newer official quota once per day. Weekends/
holidays with no new quota are normal and must not generate artificial
yield." This module adds no financial decision logic of its own -- it only
sequences calls into those modules and persists the outcome, exactly like
`app.cli.notification_worker` does for MAIL-02/MAIL-03.

Ships **disabled by default** (`cvm_valuation_enabled=false`,
`app/config.py`): `CvmClient.configured` is `False` until an operator opts
in, specifically because this feature's exact CVM column schema could not
be verified against a live response from the sandboxed environment that
authored it (`docs/PRIVILEGE_DI_CVM_INGESTION_INVESTIGATION.md`). A pass
run while disabled is a deliberate, cheap no-op (heartbeat still records
`ok=True`, `counts={"quote_ingested": 0, ...}`) -- never a crash, never
something an operator has to specifically avoid running.

Two invocation shapes, matching `app.cli.notification_worker`:

    python -m app.cli.cvm_valuation_worker --once
        One ingest+valuation pass, then exit. What a cron/systemd-timer
        invocation, or a test, wants.

    python -m app.cli.cvm_valuation_worker
        Loops `run_once()` forever, sleeping `--poll-seconds` between
        passes (default `cvm_valuation_worker_poll_seconds` -- hours, not
        minutes: CVM publishes at most one new quota per business day).
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
from app.models import AuditEvent, FundUnitPosition
from app.services import cvm_worker_status as worker_status
from app.services import privilege_valuation as valuation
from app.services.cvm_client import CvmClient
from app.services.privilege_valuation import TRACKED_FUND_CNPJ

logger = logging.getLogger(__name__)


def _audit(
    db: Session,
    *,
    household_id: str,
    event_type: str,
    entity_id: str,
    details: dict[str, object] | None,
) -> None:
    """Same local-reimplementation shape as `app.cli.notification_worker
    ._audit` (`user_id=None` -- a system/background process, not a request
    acting on behalf of a household member; avoids the same `app.api`
    circular import). `details` carries only ids/dates/sanitized error
    codes -- never a raw CVM response body."""

    db.add(
        AuditEvent(
            household_id=household_id,
            user_id=None,
            event_type=event_type,
            entity_type="fund_valuation",
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            source="cvm_valuation_worker",
        )
    )


def _households_with_position_evidence(db: Session, *, fund_cnpj: str) -> list[str]:
    return list(
        db.scalars(
            select(FundUnitPosition.household_id)
            .where(FundUnitPosition.fund_cnpj == fund_cnpj)
            .distinct()
        ).all()
    )


def run_once(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    client: CvmClient | None = None,
    fund_cnpj: str = TRACKED_FUND_CNPJ,
    now_utc=None,
) -> dict[str, int]:
    """One ingest+valuation pass. Returns counts for observability and
    testing; a per-household valuation failure is caught and counted, never
    allowed to abort the whole pass (matches
    `app.cli.notification_worker.run_once`'s per-item isolation). Every
    pass -- whether it completes normally or aborts on an unhandled
    exception -- records a heartbeat row via `app.services.cvm_worker_status`
    (`started_at` before anything else, `finished_at`/`ok`/`counts`/
    `last_error_code` in a `finally`), so a crashed pass is visible as such
    instead of looking like the previous pass never happened.
    """

    active_client = client or CvmClient()

    with session_factory() as db:
        worker_status.mark_run_started(db, now_utc=now_utc)
        db.commit()

    counts = {
        "quote_ingested": 0,
        "quote_unavailable": 0,
        "ok": 0,
        "already_valued": 0,
        "no_quote_available": 0,
        "no_position_evidence": 0,
        "valuation_error": 0,
    }
    last_error_code: str | None = None
    ok = False
    try:
        with session_factory() as db:
            ingest_result = valuation.ingest_latest_quote(
                db, fund_cnpj=fund_cnpj, client=active_client, now_utc=now_utc
            )
            db.commit()
            if ingest_result.quote is not None:
                counts["quote_ingested"] = 1
            else:
                counts["quote_unavailable"] = 1
                last_error_code = ingest_result.error_code
                logger.info(
                    "cvm_valuation_worker_quote_unavailable",
                    extra={"fund_cnpj": fund_cnpj, "error_code": ingest_result.error_code},
                )

        with session_factory() as db:
            household_ids = _households_with_position_evidence(db, fund_cnpj=fund_cnpj)

        for household_id in household_ids:
            try:
                with session_factory() as db:
                    account = valuation.resolve_privilege_account(db, household_id=household_id)
                    observed_balance, observed_balance_as_of = (None, None)
                    if account is not None:
                        observed_balance, observed_balance_as_of = valuation.resolve_observed_balance(
                            db, household_id=household_id, account_id=account.id
                        )
                    outcome = valuation.run_valuation_for_household(
                        db,
                        household_id=household_id,
                        fund_cnpj=fund_cnpj,
                        account_id=account.id if account is not None else None,
                        observed_balance=observed_balance,
                        observed_balance_as_of=observed_balance_as_of,
                    )
                    counts[outcome.status] = counts.get(outcome.status, 0) + 1
                    if outcome.status == "ok" and outcome.valuation is not None:
                        _audit(
                            db,
                            household_id=household_id,
                            event_type="fund_valuation.computed",
                            entity_id=outcome.valuation.id,
                            details={
                                "fund_cnpj": fund_cnpj,
                                "quota_reference_date": outcome.valuation.quota_reference_date,
                                "gross_value": outcome.valuation.gross_value,
                                "reconciliation_diff": outcome.valuation.reconciliation_diff,
                            },
                        )
                    db.commit()
            except Exception:  # noqa: BLE001 -- one household must never abort the whole pass
                counts["valuation_error"] += 1
                last_error_code = "valuation_pass_exception"
                logger.exception(
                    "cvm_valuation_worker_household_exception", extra={"household_id": household_id}
                )
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

    logger.info("cvm_valuation_worker_pass", extra=counts)
    return counts


def main(argv: list[str] | None = None) -> dict[str, int] | None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Run a single pass and exit instead of looping forever"
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=settings.cvm_valuation_worker_poll_seconds,
        help="Seconds to sleep between passes when not running --once",
    )
    args = parser.parse_args(argv)

    if args.once:
        return run_once()

    while True:
        run_once()
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    result = main()
    if result is not None:
        print(result)
