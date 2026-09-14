"""Assistant action audit trail: assistant_action_events table.

Revision ID: 0016
Revises: 0015

P0 #87, October Go-Live Slice 4 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.5): purely additive schema for
the Assistente Financeiro's typed-action audit/undo trail.

`assistant_action_events` is 1:1 with the already-existing `audit_events`
table via `audit_event_id` (unique FK, `ON DELETE CASCADE`) -- it reuses
`AuditEvent.household_id`/`user_id`/`trace_id`/`created_at` instead of
duplicating them, exactly as proposed in the conflict-matrix plan. Every
typed action the Assistant executes writes exactly one `AuditEvent` (via
the existing `app.api.audit()` helper, unchanged) plus exactly one row
here narrating the Assistant-specific shape of that same action (original
message, Codex's structured interpretation, disambiguation Q&A, before/
after state, and undo bookkeeping).

Nothing here touches `transactions`, `obligations`, `card_invoices`, or any
other financial-fact table -- this migration only adds a new table.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "assistant_action_events" in _live_tables():
        return
    op.create_table(
        "assistant_action_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "audit_event_id",
            sa.String(36),
            sa.ForeignKey("audit_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_message", sa.Text(), nullable=False),
        sa.Column("structured_interpretation", JSON_DOCUMENT, nullable=True),
        sa.Column("disambiguation_qa", JSON_DOCUMENT, nullable=True),
        sa.Column("typed_action", sa.String(60), nullable=False),
        sa.Column("target_entity_type", sa.String(80), nullable=True),
        sa.Column("target_entity_ids", JSON_DOCUMENT, nullable=False),
        sa.Column("before_state", JSON_DOCUMENT, nullable=True),
        sa.Column("after_state", JSON_DOCUMENT, nullable=True),
        sa.Column("undoable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("non_reversible_reason", sa.Text(), nullable=True),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "undone_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "undo_audit_event_id",
            sa.String(36),
            sa.ForeignKey("audit_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_assistant_action_events_audit_event_id",
        "assistant_action_events",
        ["audit_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_assistant_action_events_typed_action", "assistant_action_events", ["typed_action"]
    )
    op.create_index(
        "ix_assistant_action_events_created_at", "assistant_action_events", ["created_at"]
    )


def downgrade() -> None:
    """Refuses (raises `RuntimeError`, changes nothing) when any row exists
    -- same guarded pattern as migration 0015's downgrade.

    Engineering review of PR #92 (blocker 3) corrected the original claim
    here that this table is always "safe to drop unconditionally" because
    it is "reconstructable from the paired `AuditEvent`". That is not true
    in general: the paired `audit_events.details`/`before_state`/
    `after_state` for an `assistant.execute` event does not, by itself,
    carry `original_message`, `structured_interpretation`,
    `disambiguation_qa`, or this table's own undo bookkeeping
    (`undoable`/`non_reversible_reason`/`undone_at`/`undone_by`/
    `undo_audit_event_id`) -- those live only here. Dropping this table
    while it holds rows would therefore silently destroy exactly the
    typed-action narration and undo trail the Work Order requires to be
    permanent (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md` item 6/7),
    which this downgrade previously did unconditionally.

    Export first if this ever fires in practice:

        SELECT * FROM assistant_action_events;

    keep that result outside the database, then retry the downgrade once
    it is safe to lose these rows.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade in
    this chain (0015).
    """

    if not op.get_context().as_sql:
        row_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM assistant_action_events")
        ).scalar()
        if row_count:
            raise RuntimeError(
                f"Downgrade de 0016 abortado: {row_count} linha(s) de "
                "assistant_action_events (narração de ação tipada + trilha de undo do "
                "Assistente Financeiro) seriam perdidas silenciosamente e não são "
                "totalmente reconstruíveis a partir de audit_events. Exporte "
                "`SELECT * FROM assistant_action_events` primeiro, preserve o resultado "
                "fora do banco, e só então repita o downgrade."
            )
    op.drop_index("ix_assistant_action_events_created_at", table_name="assistant_action_events")
    op.drop_index("ix_assistant_action_events_typed_action", table_name="assistant_action_events")
    op.drop_index("ix_assistant_action_events_audit_event_id", table_name="assistant_action_events")
    op.drop_table("assistant_action_events")
