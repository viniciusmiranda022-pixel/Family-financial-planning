"""Due-date e-mail alert outbox: notification_deliveries table.

Revision ID: 0021
Revises: 0020

MAIL-02 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #69): purely
additive schema for the outbox/idempotency table MAIL-00's migration
(0020) deliberately left out. One row represents one `Obligation` x
`NotificationRecipient` x `obligation_due_date` x `alert_kind` ("d1"/"d0")
event; the unique constraint below is the database-level barrier against
two successful deliveries ever being created for the same logical event,
per the Work Order's "barreira final contra duas entregas" requirement.

Nothing here touches `obligations`, `notification_settings`,
`notification_recipients`, `transactions`, `accounts`, or any other
existing table -- this migration only adds one new, empty table, so there
is nothing to backfill and no existing data whose semantics change.
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017, 0018, 0019, 0020).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "notification_deliveries" not in live_tables:
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "obligation_id",
                sa.String(36),
                sa.ForeignKey("obligations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "recipient_id",
                sa.String(36),
                sa.ForeignKey("notification_recipients.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("obligation_due_date", sa.Date(), nullable=False),
            sa.Column("alert_kind", sa.String(2), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_by", sa.String(80), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(80), nullable=True),
            sa.Column("message_id", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "household_id",
                "obligation_id",
                "recipient_id",
                "obligation_due_date",
                "alert_kind",
                name="uq_notification_delivery_event",
            ),
        )
        op.create_index(
            "ix_notification_deliveries_household_id", "notification_deliveries", ["household_id"]
        )
        op.create_index(
            "ix_notification_deliveries_status_next_attempt",
            "notification_deliveries",
            ["status", "next_attempt_at"],
        )


def downgrade() -> None:
    """Guarded like 0020's downgrade: a delivery row is the household's
    only record of which alerts were sent/attempted/failed for which
    obligation, and is not reconstructible from anything else in the
    schema (it is not a financial fact so nothing else derives it).
    Dropping it silently would destroy that operational audit trail
    without warning.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade
    in this chain.
    """

    if not op.get_context().as_sql:
        delivery_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM notification_deliveries")
        ).scalar()
        if delivery_count:
            raise RuntimeError(
                f"Downgrade de 0021 abortado: {delivery_count} linha(s) de "
                "notification_deliveries (trilha de entregas de alerta) seriam perdidas "
                "silenciosamente. Exporte `SELECT * FROM notification_deliveries` primeiro, "
                "preserve o resultado fora do banco, e só então repita o downgrade."
            )

    op.drop_index(
        "ix_notification_deliveries_status_next_attempt", table_name="notification_deliveries"
    )
    op.drop_index("ix_notification_deliveries_household_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
