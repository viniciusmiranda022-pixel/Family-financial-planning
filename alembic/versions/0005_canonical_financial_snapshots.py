"""Add immutable canonical financial snapshots and metric lineage.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _live_tables() -> set[str]:
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _live_tables()
    if "financial_snapshots" not in tables:
        op.create_table(
            "financial_snapshots",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("household_id", sa.String(36), nullable=False),
            sa.Column("period", sa.String(7), nullable=False),
            sa.Column("snapshot_kind", sa.String(20), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            *[
                sa.Column(name, sa.Numeric(14, 2), nullable=False)
                for name in (
                    "operating_income",
                    "operating_expenses",
                    "operating_result",
                    "bank_cash_in",
                    "bank_cash_out",
                    "bank_cash_result",
                    "investments",
                    "redemptions",
                    "internal_transfers",
                    "card_spend",
                    "card_payments",
                    "refunds",
                    "opening_liquidity_balance",
                    "investment_yield",
                    "liquidity_used",
                    "closing_liquidity_balance",
                    "opening_uncovered_deficit",
                    "closing_uncovered_deficit",
                    "safety_floor",
                    "distance_to_floor",
                    "budget_cap",
                    "budget_usage",
                    "budget_remaining",
                    "commitments",
                    "projected_balance",
                )
            ],
            sa.Column("source_count", sa.Integer(), nullable=False),
            sa.Column("payload", JSON_DOCUMENT, nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("calculation_version", sa.String(40), nullable=False),
            sa.Column("financial_rules_version", sa.String(20), nullable=False),
            sa.Column("integrity_status", sa.String(30), nullable=False),
            sa.Column("trusted_for_reports", sa.Boolean(), nullable=False),
            sa.Column("trusted_for_projection", sa.Boolean(), nullable=False),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_by_id", sa.String(36), nullable=True),
            sa.Column(
                "generated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("generated_by", sa.String(36), nullable=True),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.ForeignKeyConstraint(["generated_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["superseded_by_id"], ["financial_snapshots.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id",
                "period",
                "snapshot_kind",
                "version",
                name="uq_financial_snapshot_version",
            ),
        )
        for column in ("household_id", "checksum", "trace_id"):
            op.create_index(
                f"ix_financial_snapshots_{column}", "financial_snapshots", [column]
            )
        op.create_index(
            "ix_financial_snapshots_household_period_status",
            "financial_snapshots",
            ["household_id", "period", "status"],
        )
    if "financial_snapshot_lineage" not in tables:
        op.create_table(
            "financial_snapshot_lineage",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("household_id", sa.String(36), nullable=False),
            sa.Column("snapshot_id", sa.String(36), nullable=False),
            sa.Column("metric_key", sa.String(80), nullable=False),
            sa.Column("entity_type", sa.String(80), nullable=False),
            sa.Column("entity_id", sa.String(36), nullable=False),
            sa.Column("document_id", sa.String(36), nullable=True),
            sa.Column("rule_id", sa.String(80), nullable=False),
            sa.Column("contribution", sa.Numeric(14, 2), nullable=True),
            sa.Column("source_role", sa.String(24), nullable=False),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["snapshot_id"], ["financial_snapshots.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("household_id", "snapshot_id", "trace_id"):
            op.create_index(
                f"ix_financial_snapshot_lineage_{column}",
                "financial_snapshot_lineage",
                [column],
            )
        op.create_index(
            "ix_financial_snapshot_lineage_metric",
            "financial_snapshot_lineage",
            ["snapshot_id", "metric_key"],
        )


def downgrade() -> None:
    tables = _live_tables()
    if not tables or "financial_snapshot_lineage" in tables:
        op.drop_table("financial_snapshot_lineage")
    if not tables or "financial_snapshots" in tables:
        op.drop_table("financial_snapshots")
