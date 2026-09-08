"""Crash-recovery reconciler for asynchronous OCR/audio capture jobs.

The normal path -- a request enqueues a job -- is processed inline by a
FastAPI `BackgroundTask` (`app.api._run_capture_job_background`) right
after the response is sent, and never needs this script. This exists only
for the job that outlived its request: a worker container killed/OOM'd/
restarted mid-processing leaves its job stuck in `processing` forever
unless something reclaims it -- see
`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md` item 5 ("recuperação de
worker") and `app.services.capture_worker` for the claiming semantics this
reuses verbatim (same claim function, same processing function -- no
second OCR/transcription engine).

Deliberately does not run as a persistent daemon/polling loop: this is a
single family/household deployment (`docs/ARCHITECTURE.md`, no Redis, no
broker -- see `app.services.capture_worker`'s module docstring), so a
`--once` invocation from cron/systemd-timer/manual operator action is
enough to reconcile whatever has gone stale since the last run, without
adding a long-running process to the container's lifecycle.

Usage:
    python -m app.cli.capture_worker [--limit N] [--stale-after-seconds N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.services import capture_worker as capture_worker_service

logger = logging.getLogger(__name__)


async def reconcile_once(
    *,
    limit: int,
    stale_after_seconds: int,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, int]:
    """One reconciliation pass: fail jobs that are stuck past their attempt
    budget, then reclaim+process everything still eligible. Returns counts
    for observability/testing; never raises for an individual job failure
    (each job's own processing failure is captured and recorded on that
    job, exactly as the inline background task does).

    `session_factory` defaults to the real `app.db.SessionLocal` -- tests
    inject their own isolated session factory instead of a global `get_db`
    dependency override, since this (like the inline background task it
    drives) always opens its own session rather than receiving one through
    FastAPI's dependency injection.
    """

    with session_factory() as db:
        failed_exhausted = capture_worker_service.fail_exhausted_stale_jobs(
            db, stale_after_seconds=stale_after_seconds
        )
        db.commit()
        job_ids = capture_worker_service.find_claimable_job_ids(
            db, limit=limit, stale_after_seconds=stale_after_seconds
        )

    # Imported lazily to avoid importing the (large) FastAPI router module
    # -- and everything it pulls in -- for callers that only need the
    # queue-inspection functions above.
    from app.api import _run_capture_job_background

    reclaimed = 0
    for job_id in job_ids:
        await _run_capture_job_background(job_id, None, session_factory=session_factory)
        reclaimed += 1

    logger.info(
        "capture_worker_reconcile",
        extra={
            "reclaimed": reclaimed,
            "failed_exhausted": failed_exhausted,
            "stale_after_seconds": stale_after_seconds,
        },
    )
    return {"reclaimed": reclaimed, "failed_exhausted": failed_exhausted}


def main(argv: list[str] | None = None) -> dict[str, int]:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Maximum jobs to reclaim in one pass")
    parser.add_argument(
        "--stale-after-seconds",
        type=int,
        default=settings.capture_job_stale_after_seconds,
        help="How long a job may sit in 'processing' before it is considered crashed",
    )
    args = parser.parse_args(argv)
    return asyncio.run(reconcile_once(limit=args.limit, stale_after_seconds=args.stale_after_seconds))


if __name__ == "__main__":
    result = main()
    print(result)
