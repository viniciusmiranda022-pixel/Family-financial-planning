"""Due-date e-mail alert configuration: notification_settings,
notification_recipients tables.

Revision ID: 0020
Revises: 0019

MAIL-00 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #67): purely
additive schema for the first slice of the due-date e-mail alerts epic
(#66). `notification_settings` holds one row per household (the global
enabled toggle, daily send time and timezone); `notification_recipients`
holds one or more e-mail addresses per household with their D-1/D0
preferences. Deliberately does *not* create `notification_deliveries`
(the outbox/idempotency table) -- that table belongs to MAIL-02
(`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, "Divisão em slices"), which
also introduces the scheduler that would actually write to it. Adding it
here now, unused, would anticipate a later slice's contract before its own
Work Order fixes it.

Nothing here touches `obligations`, `transactions`, `accounts`, or any
other existing table -- this migration only adds two new, empty tables, so
there is nothing to backfill and no existing data whose semantics change.
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017, 0018, 0019).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "notification_settings" not in live_tables:
        op.create_table(
            "notification_settings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("send_time_local", sa.String(5), nullable=False, server_default="08:00"),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Sao_Paulo"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_notification_settings_household_id", "notification_settings", ["household_id"]
        )

    if "notification_recipients" not in live_tables:
        op.create_table(
            "notification_recipients",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("email", sa.String(254), nullable=False),
            sa.Column("normalized_email", sa.String(254), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_d1", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_d0", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "household_id", "normalized_email", name="uq_notification_recipient_household_email"
            ),
        )
        op.create_index(
            "ix_notification_recipients_household_id", "notification_recipients", ["household_id"]
        )


def downgrade() -> None:
    """Guarded like migrations 0015/0016/0019's downgrades: a recipient row
    is the household's only record of who asked to be alerted and how, and
    is not reconstructible from anything else in the schema. Dropping it
    silently would destroy that configuration without warning.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade in
    this chain.
    """

    if not op.get_context().as_sql:
        recipient_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM notification_recipients")
        ).scalar()
        if recipient_count:
            raise RuntimeError(
                f"Downgrade de 0020 abortado: {recipient_count} linha(s) de "
                "notification_recipients (destinatários de alerta configurados) seriam "
                "perdidas silenciosamente. Exporte `SELECT * FROM notification_recipients` "
                "primeiro, preserve o resultado fora do banco, e só então repita o downgrade."
            )
        settings_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM notification_settings")
        ).scalar()
        if settings_count:
            raise RuntimeError(
                f"Downgrade de 0020 abortado: {settings_count} linha(s) de "
                "notification_settings seriam perdidas silenciosamente. Exporte "
                "`SELECT * FROM notification_settings` primeiro, preserve o resultado fora "
                "do banco, e só então repita o downgrade."
            )

    op.drop_index("ix_notification_recipients_household_id", table_name="notification_recipients")
    op.drop_table("notification_recipients")
    op.drop_index("ix_notification_settings_household_id", table_name="notification_settings")
    op.drop_table("notification_settings")
