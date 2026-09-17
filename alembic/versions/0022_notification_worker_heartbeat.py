"""Due-date e-mail alert worker heartbeat: notification_worker_heartbeats
table.

Revision ID: 0022
Revises: 0021

MAIL-03 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70): purely
additive schema for the single-row operational heartbeat
`app.cli.notification_worker.run_once()` writes on every pass and
`GET /notification-settings/status` reads, per the Work Order's
"Observabilidade" section ("última execução", "último erro sanitizado").

Nothing here touches `notification_settings`, `notification_recipients`,
`notification_deliveries`, `obligations`, `transactions`, `accounts`, or
any other existing table -- this migration only adds one new, empty
table, so there is nothing to backfill and no existing data whose
semantics change.
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017, 0018, 0019, 0020, 0021).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "notification_worker_heartbeats" not in live_tables:
        op.create_table(
            "notification_worker_heartbeats",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("worker_id", sa.String(120), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ok", sa.Boolean(), nullable=True),
            sa.Column("counts", sa.JSON(), nullable=True),
            sa.Column("last_error_code", sa.String(80), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    """Unguarded, unlike 0020/0021's downgrade: this table is a disposable
    operational cache the worker rebuilds from scratch on its very next
    pass (a fresh row with `started_at` set again) -- it carries no
    irreplaceable history (that lives in `notification_deliveries`/
    `AuditEvent`, both untouched by this migration), so dropping it loses
    nothing that cannot be reconstructed by the worker simply running
    again.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade
    in this chain; this one has nothing to guard.
    """

    op.drop_table("notification_worker_heartbeats")
