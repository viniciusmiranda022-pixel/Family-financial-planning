"""WA-05: assistant_conversation_states table.

Revision ID: 0026
Revises: 0025

Issue #77, `docs/WORK_ORDER_WA_05.md`: purely additive schema for short,
structured, server-side conversational continuity
(`app.services.assistant_conversation_context`) -- one row per
`(household_id, user_id, conversation_id)`. Never a second audit trail and
never a financial record: see `app.models.AssistantConversationState` for
the full contract (why it only ever stores already-sanitized plain chat
text plus already-allowlisted read-only tool calls, and why the write
flow's own authorization is never resolved from here).

Nothing here touches `transactions`, `obligations`, `card_invoices`,
`audit_events`, `assistant_action_proposals`, `assistant_action_events`,
or any other existing table -- this migration only adds a new table, so
there is nothing to backfill and no existing data whose semantics change.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain.
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "assistant_conversation_states" in _live_tables():
        return
    op.create_table(
        "assistant_conversation_states",
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
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("turns", JSON_DOCUMENT, nullable=False),
        sa.Column("last_steps", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "household_id",
            "user_id",
            "conversation_id",
            name="uq_assistant_conversation_states_identity",
        ),
    )
    op.create_index(
        "ix_assistant_conversation_states_household_id",
        "assistant_conversation_states",
        ["household_id"],
    )
    op.create_index(
        "ix_assistant_conversation_states_user_id",
        "assistant_conversation_states",
        ["user_id"],
    )
    op.create_index(
        "ix_assistant_conversation_states_expires_at",
        "assistant_conversation_states",
        ["expires_at"],
    )


def downgrade() -> None:
    """Safe to drop unconditionally, without a row-count guard: a
    conversation-state row is never a financial fact, never the sole
    record of anything auditable (the underlying `AuditEvent`/
    `AssistantActionEvent` trail this Tool Layer round already produces is
    untouched by this table), and is designed to be lost/expired routinely
    (sliding TTL) without loss of any real fact -- losing this table only
    means the next follow-up message loses its short-term conversational
    memory and, worst case, asks a clarifying question it could otherwise
    have skipped.
    """

    op.drop_index(
        "ix_assistant_conversation_states_expires_at", table_name="assistant_conversation_states"
    )
    op.drop_index("ix_assistant_conversation_states_user_id", table_name="assistant_conversation_states")
    op.drop_index(
        "ix_assistant_conversation_states_household_id", table_name="assistant_conversation_states"
    )
    op.drop_table("assistant_conversation_states")
