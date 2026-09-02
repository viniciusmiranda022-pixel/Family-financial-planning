"""Add household_financial_revisions (monthly-close trust TOCTOU barrier).

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # Additive, non-destructive: a new table with no foreign keys pointing
    # into it yet, and no existing row anywhere is read or rewritten.
    # `app.services.financial_revision.bump_household_financial_revision`/
    # `lock_household_financial_revision` create a household's row lazily
    # (upsert) the first time it is touched, so no backfill is required --
    # see docs/INTEGRITY_IMPLEMENTATION_PLAN.md and the engineering review
    # on PR 7, Round 7 ("The trust transition must be serialized with every
    # financial source mutation that can affect the snapshot ... or use a
    # monotonic source revision validated inside the same transactional
    # barrier").
    if "household_financial_revisions" not in _live_tables():
        op.create_table(
            "household_financial_revisions",
            sa.Column("household_id", sa.String(36), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("household_id"),
        )


def downgrade() -> None:
    if op.get_context().as_sql or "household_financial_revisions" in _live_tables():
        op.drop_table("household_financial_revisions")
