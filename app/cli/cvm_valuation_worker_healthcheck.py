"""Docker `HEALTHCHECK` probe for the `cvm-worker` compose service (issue
#85, `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

Same shape as `app.cli.notification_worker_healthcheck`: no HTTP port to
probe, so this script reads the same heartbeat singleton row
`app.services.cvm_worker_status` writes on every pass and exits non-zero
when the worker looks stopped or stuck. Never prints a household id, a
valuation figure, or anything from `.env` -- only the sanitized status
word.

Exit codes (stdout is a short, sanitized status word for `docker inspect
--format='{{.State.Health.Log}}'`, never a stack trace):

    0  "ok"        -- a pass has completed within the freshness window.
    1  "stale"      -- the worker has run before, but not recently enough.
    1  "never_run"  -- no heartbeat row yet (expected for the first pass
                       after container start; `compose.yaml`'s
                       `--start-period` accounts for this).

The freshness window is `CVM_VALUATION_WORKER_POLL_SECONDS * 3` (minimum 30
minutes, looser than the notification worker's -- CVM publishes at most one
new quota per business day, so this worker's own poll interval is already
measured in hours, not minutes).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db import SessionLocal
from app.services.cvm_worker_status import heartbeat_snapshot

_MIN_FRESHNESS_SECONDS = 1800


def _freshness_window_seconds() -> int:
    return max(_MIN_FRESHNESS_SECONDS, get_settings().cvm_valuation_worker_poll_seconds * 3)


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
