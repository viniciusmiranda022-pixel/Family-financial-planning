#!/bin/sh
set -eu

# MAIL-03 (docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md, issue #70): the
# `notification-worker` compose service reuses this same image/entrypoint
# with `command: ["worker"]`, so it never runs `alembic upgrade head`
# itself -- it `depends_on: app: condition: service_healthy`, and `app`'s
# own startup (below) already ran migrations to head before its `/health`
# endpoint can report healthy, so running them a second time here would
# only be redundant, never a race the worker needs to resolve.
if [ "${1:-}" = "worker" ]; then
    shift
    exec python -m app.cli.notification_worker "$@"
fi

# Issue #85 (docs/WORK_ORDER_PRIVILEGE_DI_CVM.md): the `cvm-worker` compose
# service reuses this same image/entrypoint with `command: ["cvm-worker"]`,
# same reasoning as `worker` above -- `depends_on: app: condition:
# service_healthy` already guarantees migrations (0023 included) have run.
if [ "${1:-}" = "cvm-worker" ]; then
    shift
    exec python -m app.cli.cvm_valuation_worker "$@"
fi

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
