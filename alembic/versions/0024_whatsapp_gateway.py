"""WA-01: whatsapp_authorized_numbers, whatsapp_inbound_events,
whatsapp_rate_limit_buckets tables.

Revision ID: 0024
Revises: 0023

Issue #73, `docs/WORK_ORDER_WA_01.md`: purely additive schema for the
WhatsApp webhook gateway's transport/authorization layer. No existing
table is touched -- there is no legacy phone-number/WhatsApp data anywhere
in this codebase to migrate (WA-00, `docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`,
confirms only documentation was added before this PR). This migration has
nothing to backfill.

Nothing here creates, reads or writes `transactions`, `accounts`,
`obligations`, `card_invoices`, or any other financial table.
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain.
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "whatsapp_authorized_numbers" not in live_tables:
        op.create_table(
            "whatsapp_authorized_numbers",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("phone_hash", sa.String(64), nullable=False),
            sa.Column("phone_encrypted", sa.Text(), nullable=False),
            sa.Column("phone_last4", sa.String(4), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column(
                "created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "deactivated_by",
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
        op.create_index(
            "ix_whatsapp_authorized_numbers_household_id",
            "whatsapp_authorized_numbers",
            ["household_id"],
        )
        op.create_index(
            "ix_whatsapp_authorized_numbers_user_id", "whatsapp_authorized_numbers", ["user_id"]
        )
        op.create_index(
            "ix_whatsapp_authorized_numbers_phone_hash", "whatsapp_authorized_numbers", ["phone_hash"]
        )
        # Partial unique indexes: only one *active* row may exist per
        # user/phone_hash -- an ambiguous phone->user mapping would be an
        # authorization defect (`docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`
        # §4.3), not just a data-hygiene one. A deactivated row is excluded
        # so a number/user can be relinked later without deleting history.
        op.create_index(
            "uq_whatsapp_authorized_numbers_active_user",
            "whatsapp_authorized_numbers",
            ["user_id"],
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active"),
        )
        op.create_index(
            "uq_whatsapp_authorized_numbers_active_phone_hash",
            "whatsapp_authorized_numbers",
            ["phone_hash"],
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active"),
        )

    if "whatsapp_inbound_events" not in live_tables:
        op.create_table(
            "whatsapp_inbound_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("provider_message_id", sa.String(128), nullable=False),
            sa.Column("sender_phone_hash", sa.String(64), nullable=False),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column(
                "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.UniqueConstraint(
                "provider_message_id", name="uq_whatsapp_inbound_events_message_id"
            ),
        )
        op.create_index(
            "ix_whatsapp_inbound_events_sender_phone_hash",
            "whatsapp_inbound_events",
            ["sender_phone_hash"],
        )
        op.create_index(
            "ix_whatsapp_inbound_events_household_id", "whatsapp_inbound_events", ["household_id"]
        )
        op.create_index(
            "ix_whatsapp_inbound_events_received_at", "whatsapp_inbound_events", ["received_at"]
        )

    if "whatsapp_rate_limit_buckets" not in live_tables:
        op.create_table(
            "whatsapp_rate_limit_buckets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("bucket_key", sa.String(64), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.UniqueConstraint("bucket_key", name="uq_whatsapp_rate_limit_buckets_key"),
        )


def downgrade() -> None:
    """`whatsapp_authorized_numbers` is guarded like `notification_recipients`
    (migration 0020): it is the only record of which admin authorized which
    user's WhatsApp number, not reconstructible from anything else.
    `whatsapp_inbound_events`/`whatsapp_rate_limit_buckets` are pure
    operational telemetry (receipt/rate-limit bookkeeping, no message
    content, no financial fact) -- same unguarded treatment as
    `notification_worker_heartbeats` (migration 0022)."""

    if not op.get_context().as_sql:
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM whatsapp_authorized_numbers")
        ).scalar()
        if count:
            raise RuntimeError(
                f"Downgrade de 0024 abortado: {count} linha(s) de whatsapp_authorized_numbers "
                "(vínculo número->usuário autorizado por um admin) seriam perdidas silenciosamente "
                "e não são reconstruíveis. Exporte `SELECT * FROM whatsapp_authorized_numbers` "
                "primeiro, preserve o resultado fora do banco, e só então repita o downgrade."
            )

    op.drop_table("whatsapp_rate_limit_buckets")

    op.drop_index("ix_whatsapp_inbound_events_received_at", table_name="whatsapp_inbound_events")
    op.drop_index("ix_whatsapp_inbound_events_household_id", table_name="whatsapp_inbound_events")
    op.drop_index("ix_whatsapp_inbound_events_sender_phone_hash", table_name="whatsapp_inbound_events")
    op.drop_table("whatsapp_inbound_events")

    op.drop_index(
        "uq_whatsapp_authorized_numbers_active_phone_hash", table_name="whatsapp_authorized_numbers"
    )
    op.drop_index(
        "uq_whatsapp_authorized_numbers_active_user", table_name="whatsapp_authorized_numbers"
    )
    op.drop_index(
        "ix_whatsapp_authorized_numbers_phone_hash", table_name="whatsapp_authorized_numbers"
    )
    op.drop_index("ix_whatsapp_authorized_numbers_user_id", table_name="whatsapp_authorized_numbers")
    op.drop_index(
        "ix_whatsapp_authorized_numbers_household_id", table_name="whatsapp_authorized_numbers"
    )
    op.drop_table("whatsapp_authorized_numbers")
