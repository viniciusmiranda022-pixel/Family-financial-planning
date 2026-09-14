"""Assistant learning: entry_type_templates table.

Revision ID: 0017
Revises: 0016

P0 #87, October Go-Live Slice 4 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.6): purely additive schema for
the Assistente Financeiro's "Outra entrada"/"Outra saída" learning
mechanism. Copies `classification_rules`' proven lifecycle
(`observed -> suggested -> pending_acceptance -> active`, admin-gated
activation, evidence accumulation) rather than a second rule engine.

Nothing here touches `transactions`, `categories`, or any other financial
fact table -- a template only ever influences future presentation/reuse of
a label, never a financial invariant, which is also why this model is
deliberately absent from `app.models.FINANCIAL_REVISION_MODELS`.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _live_tables() -> set[str]:
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "entry_type_templates" in _live_tables():
        return
    op.create_table(
        "entry_type_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "household_id",
            sa.String(36),
            sa.ForeignKey("households.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("movement_type", sa.String(20), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("normalized_label", sa.String(100), nullable=False),
        sa.Column(
            "category_id",
            sa.String(36),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(30), nullable=False, server_default="observed"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "household_id",
            "normalized_label",
            "movement_type",
            name="uq_entry_type_template_household_label_movement",
        ),
    )
    op.create_index(
        "ix_entry_type_templates_household_id", "entry_type_templates", ["household_id"]
    )
    op.create_index(
        "ix_entry_type_templates_household_label",
        "entry_type_templates",
        ["household_id", "normalized_label"],
    )


def downgrade() -> None:
    """Safe to drop unconditionally: every row here is a learned
    presentation shortcut re-derivable from future confirmed corrections
    (same guarantee `classification_rules` already relies on) -- never a
    financial fact.
    """

    op.drop_index("ix_entry_type_templates_household_label", table_name="entry_type_templates")
    op.drop_index("ix_entry_type_templates_household_id", table_name="entry_type_templates")
    op.drop_table("entry_type_templates")
