"""Add backfill_runs (auditable manifest for app.cli.backfill).

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_JSON_DOCUMENT = sa.JSON().with_variant(JSONB(), "postgresql")


def _live_tables() -> set[str]:
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # Additive only: a brand-new table with no foreign key pointing into it
    # from any existing table, so no existing row anywhere is read or
    # rewritten by this migration. `app.cli.backfill` creates its own rows
    # lazily the first time it runs -- no historical backfill invocation
    # exists to reconstruct, so there is nothing to backfill for this table
    # itself. See docs/WORK_ORDER_PR8_FINANCIAL_SAFETY_CI_BACKFILL_FINAL_DOCS.md
    # scope item 5 ("registrar run, versões, duração, resultado e
    # rastreabilidade suficiente para auditoria").
    if "backfill_runs" not in _live_tables():
        op.create_table(
            "backfill_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="running"),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("household_scope", _JSON_DOCUMENT, nullable=True),
            sa.Column("period_from", sa.String(7), nullable=True),
            sa.Column("period_to", sa.String(7), nullable=True),
            sa.Column("financial_rules_version", sa.String(20), nullable=False),
            sa.Column("calculation_version", sa.String(40), nullable=False),
            sa.Column("app_version", sa.String(40), nullable=True),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("summary", _JSON_DOCUMENT, nullable=True),
            sa.Column("error_code", sa.String(80), nullable=True),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.Column("triggered_by", sa.String(36), nullable=True),
            sa.ForeignKeyConstraint(["triggered_by"], ["users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("trace_id", name="uq_backfill_runs_trace_id"),
        )
        op.create_index("ix_backfill_runs_status", "backfill_runs", ["status"])
        op.create_index("ix_backfill_runs_trace_id", "backfill_runs", ["trace_id"])


def downgrade() -> None:
    # Safe: `backfill_runs` is a pure audit manifest with nothing else
    # referencing it (no other table has a foreign key into it), so dropping
    # it cannot orphan any other row. It never carries a source financial
    # fact -- Transaction/Document/PayrollRecord/Commission/Obligation are
    # never written to by app.cli.backfill -- so downgrading only removes the
    # backfill process's own run history, not any financial evidence.
    if op.get_context().as_sql or "backfill_runs" in _live_tables():
        op.drop_index("ix_backfill_runs_trace_id", table_name="backfill_runs")
        op.drop_index("ix_backfill_runs_status", table_name="backfill_runs")
        op.drop_table("backfill_runs")
