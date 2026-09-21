"""SMTP sender config configurable via the Settings UI: smtp_sender_configs
table.

Revision ID: 0028
Revises: 0027

MAIL-04 (`docs/WORK_ORDER_MAIL_04_UI_SMTP_CONFIG.md`, issue #110): purely
additive schema for the single-row, global SMTP sender configuration an
admin can save from the UI instead of only via `ALERT_SMTP_*` env vars.
`app_password_encrypted` stores the App Password Fernet-encrypted with the
dedicated `SMTP_ENCRYPTION_KEY` (`app.services.smtp_config`) -- never
plaintext, never reusing another domain's key.

Nothing here touches `notification_settings`, `notification_recipients`,
`notification_deliveries`, `notification_worker_heartbeats`, or any
financial table -- this migration only adds one new, empty table, so there
is nothing to backfill and no existing row whose semantics change. An
installation that never uses the new Settings UI SMTP panel keeps using
`ALERT_SMTP_*` exactly as before (`app.services.smtp_config
.resolve_effective_smtp_settings` falls back to it whenever this table has
no row, or its row is disabled/incomplete).
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009-0022, 0024-0027).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "smtp_sender_configs" not in live_tables:
        op.create_table(
            "smtp_sender_configs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("host", sa.String(255), nullable=True),
            sa.Column("port", sa.Integer(), nullable=False, server_default="587"),
            sa.Column("username", sa.String(255), nullable=True),
            sa.Column("app_password_encrypted", sa.Text(), nullable=True),
            sa.Column("from_email", sa.String(255), nullable=True),
            sa.Column("use_starttls", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
            sa.Column(
                "updated_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    """Unguarded, like 0022's downgrade: this table only ever holds one
    operator-managed configuration row an admin can re-enter from the
    Settings UI (or fall back to `ALERT_SMTP_*` env) -- it carries no
    financial fact and nothing else references it, so dropping it on
    downgrade loses no irreplaceable history. `notification_deliveries`/
    `AuditEvent` (the config *change* history, via `smtp_config.update`
    audit events) are untouched by this migration either way.

    Offline SQL generation has no live connection to check against --
    matches every other guarded/unguarded downgrade in this chain.
    """

    op.drop_table("smtp_sender_configs")
