"""Assistant action proposals: assistant_action_proposals table.

Revision ID: 0018
Revises: 0017

P0 #87, October Go-Live Slice 4 -- engineering review of PR #92 (blocker 1):
purely additive schema for the Assistente Financeiro's server-side typed-
action proposal, closing the gap where `POST /assistant/execute` accepted
`typed_action`/`payload`/`path_params`/`structured_interpretation` directly
from the HTTP caller instead of a proposal `POST /assistant/interpret`
itself already resolved deterministically.

`assistant_action_proposals` is single-use: `POST /assistant/interpret`
persists exactly one row whenever `build_typed_action_proposal` returns
`can_execute=True`; `POST /assistant/execute` accepts only that row's `id`
and reads every mutation-relevant field back from it -- see
`app.models.AssistantActionProposal` and `app.services.assistant_actions`
for the full contract. `consumed_action_event_id` is a nullable FK to
`assistant_action_events` (`ON DELETE SET NULL`, not `CASCADE`): losing the
narration row must never retroactively invalidate a proposal's own
consumed/idempotency bookkeeping.

Nothing here touches `transactions`, `obligations`, `card_invoices`,
`audit_events`, `assistant_action_events`, or any other existing table --
this migration only adds a new table.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "assistant_action_proposals" in _live_tables():
        return
    op.create_table(
        "assistant_action_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "household_id",
            sa.String(36),
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("original_message", sa.Text(), nullable=False),
        sa.Column("structured_interpretation", JSON_DOCUMENT, nullable=True),
        sa.Column("typed_action", sa.String(60), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("path_params", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_action_event_id",
            sa.String(36),
            sa.ForeignKey("assistant_action_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_assistant_action_proposals_household_id",
        "assistant_action_proposals",
        ["household_id"],
    )
    op.create_index(
        "ix_assistant_action_proposals_trace_id", "assistant_action_proposals", ["trace_id"]
    )
    op.create_index(
        "ix_assistant_action_proposals_typed_action", "assistant_action_proposals", ["typed_action"]
    )
    op.create_index(
        "ix_assistant_action_proposals_created_at", "assistant_action_proposals", ["created_at"]
    )


def downgrade() -> None:
    """Safe to drop unconditionally, without a row-count guard: a proposal
    is never itself a financial fact and never the only place a fact is
    recorded -- an unconsumed proposal is just a cached resolution the user
    can regenerate with the exact same `/assistant/interpret` call, and a
    consumed proposal's actual effect (the typed action's own domain write,
    plus `assistant_action_events`) lives entirely in already-existing
    tables this migration never touches. Unlike migration 0016's downgrade
    (guarded after the PR #92 review), losing this table loses no
    information any user or auditor could ever have relied on as
    permanent.
    """

    op.drop_index("ix_assistant_action_proposals_created_at", table_name="assistant_action_proposals")
    op.drop_index("ix_assistant_action_proposals_typed_action", table_name="assistant_action_proposals")
    op.drop_index("ix_assistant_action_proposals_trace_id", table_name="assistant_action_proposals")
    op.drop_index("ix_assistant_action_proposals_household_id", table_name="assistant_action_proposals")
    op.drop_table("assistant_action_proposals")
