"""Add monthly_financial_closes.financial_revision (run-to-trust TOCTOU barrier).

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _live_columns(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    # Additive, nullable: no backfill is possible (or needed) for a close
    # created before this column existed -- there is no historical
    # `HouseholdFinancialRevision` reading to reconstruct for it. Initially
    # `trust_monthly_close` treated `NULL` as "not applicable", skipping the
    # comparison; the Round 10 engineering review found that fail-open for a
    # pre-migration `review_required` row (every row this migration leaves
    # `NULL`), which could then reach `trusted` after deployment without ever
    # having a revision captured by `/run`. `trust_monthly_close` now treats
    # `NULL` as missing evidence and fails closed for any new promotion from
    # `review_required`, requiring a fresh `/run` -- see that function's
    # docstring. This migration itself does not change: still additive,
    # nullable, no destructive backfill; already-`trusted` rows are read-only
    # history `trust_monthly_close` never re-evaluates. See the engineering
    # review on PR 7, Round 8: "Persist the revision used by /run on the
    # close/run and require it to equal the locked revision before
    # trusting."
    if "financial_revision" not in _live_columns("monthly_financial_closes"):
        op.add_column(
            "monthly_financial_closes",
            sa.Column("financial_revision", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if op.get_context().as_sql or "financial_revision" in _live_columns("monthly_financial_closes"):
        op.drop_column("monthly_financial_closes", "financial_revision")
