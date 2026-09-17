"""Docker `HEALTHCHECK` probe for the `notification-worker` compose
service (MAIL-03, `docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70).

The worker (`app.cli.notification_worker`) has no HTTP port to probe --
unlike `app`'s own `/health` (`app/main.py`), it is a polling loop with no
listener. This script is the container-local equivalent: it reads the
same `NotificationWorkerHeartbeat` singleton row
`app.services.notification_worker_status` writes on every pass and exits
non-zero when the worker looks stopped or stuck, so `docker compose ps`/
`docker inspect` can surface "worker parado/falhando" without needing any
new network exposure, PII, or secret -- it never prints a recipient
address, a delivery id, or anything from `.env`.

Exit codes (stdout is a short, sanitized status word for `docker inspect
--format='{{.State.Health.Log}}'`, never a stack trace or exception
message):

    0  "ok"        -- a pass has completed within the freshness window.
    1  "stale"      -- the worker has run before, but not recently enough
                       (stuck loop, or the process died and the
                       `restart: unless-stopped` policy has not yet
                       brought a new one up cleanly).
    1  "never_run"  -- no heartbeat row yet. Expected for the first few
                       seconds after the container starts (the `--start-
                       period` in `compose.yaml` accounts for this); a
                       persistent `never_run` past that means the process
                       never completed even its first pass.

The freshness window is `NOTIFICATION_WORKER_POLL_SECONDS * 3` (minimum
5 minutes): loose enough that ordinary jitter (a slow SMTP timeout, a
larger-than-usual claimable batch) never flaps the container between
healthy/unhealthy, tight enough to still catch a genuinely stuck process
well before a human would otherwise notice missed D-1/D0 alerts.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db import SessionLocal
from app.services.notification_worker_status import heartbeat_snapshot

_MIN_FRESHNESS_SECONDS = 300


def _freshness_window_seconds() -> int:
    return max(_MIN_FRESHNESS_SECONDS, get_settings().notification_worker_poll_seconds * 3)


def check(*, now_utc: datetime | None = None) -> tuple[bool, str]:
    now = now_utc if now_utc is not None else datetime.now(UTC)
    with SessionLocal() as db:
        snapshot = heartbeat_snapshot(db)
    if snapshot is None:
        return False, "never_run"
    reference = snapshot["last_finished_at"] or snapshot["last_started_at"]
    reference_dt = datetime.fromisoformat(reference)
    age_seconds = (now - reference_dt).total_seconds()
    if age_seconds > _freshness_window_seconds():
        return False, "stale"
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    del argv
    healthy, status = check()
    print(status)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
