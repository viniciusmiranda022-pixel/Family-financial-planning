"""Add the canonical FinancialSnapshot, its lineage and the central liquidity account.

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


def _live_columns(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    tables = _live_tables()

    financial_profile_columns = _live_columns("financial_profiles")
    if "central_liquidity_account_id" not in financial_profile_columns:
        column = sa.Column(
            "central_liquidity_account_id",
            sa.String(length=36),
            sa.ForeignKey(
                "accounts.id",
                ondelete="SET NULL",
                name="fk_financial_profiles_central_liquidity_account_id",
            ),
            nullable=True,
        )
        if not op.get_context().as_sql and op.get_bind().dialect.name == "sqlite":
            # SQLite cannot add a column carrying a foreign key with plain ALTER.
            with op.batch_alter_table("financial_profiles") as batch_op:
                batch_op.add_column(column)
        else:
            op.add_column("financial_profiles", column)

    if "financial_snapshots" not in tables:
        op.create_table(
            "financial_snapshots",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("period", sa.String(length=7), nullable=False),
            sa.Column("snapshot_kind", sa.String(length=16), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("integrity_status", sa.String(length=16), nullable=False),
            sa.Column("financial_rules_version", sa.String(length=20), nullable=False),
            sa.Column("calculation_version", sa.String(length=40), nullable=False),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column("checksum", sa.String(length=64), nullable=False),
            sa.Column("operating_income", sa.Numeric(14, 2), nullable=False),
            sa.Column("operating_expenses", sa.Numeric(14, 2), nullable=False),
            sa.Column("operating_result", sa.Numeric(14, 2), nullable=False),
            sa.Column("budget_usage", sa.Numeric(14, 2), nullable=False),
            sa.Column("bank_cash_in", sa.Numeric(14, 2), nullable=False),
            sa.Column("bank_cash_out", sa.Numeric(14, 2), nullable=False),
            sa.Column("bank_cash_result", sa.Numeric(14, 2), nullable=False),
            sa.Column("card_spend", sa.Numeric(14, 2), nullable=False),
            sa.Column("card_payments", sa.Numeric(14, 2), nullable=False),
            sa.Column("investments", sa.Numeric(14, 2), nullable=False),
            sa.Column("redemptions", sa.Numeric(14, 2), nullable=False),
            sa.Column("internal_transfers", sa.Numeric(14, 2), nullable=False),
            sa.Column("refunds", sa.Numeric(14, 2), nullable=False),
            sa.Column("commitments", sa.Numeric(14, 2), nullable=False),
            sa.Column("projected_balance", sa.Numeric(14, 2), nullable=True),
            sa.Column("opening_liquidity_balance", sa.Numeric(14, 2), nullable=True),
            sa.Column("investment_yield", sa.Numeric(14, 2), nullable=True),
            sa.Column("liquidity_used", sa.Numeric(14, 2), nullable=True),
            sa.Column("closing_liquidity_balance", sa.Numeric(14, 2), nullable=True),
            sa.Column("opening_uncovered_deficit", sa.Numeric(14, 2), nullable=True),
            sa.Column("closing_uncovered_deficit", sa.Numeric(14, 2), nullable=True),
            sa.Column("safety_floor", sa.Numeric(14, 2), nullable=True),
            sa.Column("distance_to_floor", sa.Numeric(14, 2), nullable=True),
            sa.Column("floor_breached", sa.Boolean(), nullable=True),
            sa.Column("opening_balance_source_type", sa.String(length=24), nullable=True),
            sa.Column("opening_balance_source_id", sa.String(length=36), nullable=True),
            sa.Column("source_count", sa.Integer(), nullable=False),
            sa.Column("supersedes_id", sa.String(length=36), nullable=True),
            sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
            sa.Column(
                "generated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["supersedes_id"],
                ["financial_snapshots.id"],
                ondelete="SET NULL",
                use_alter=True,
                name="fk_financial_snapshots_supersedes_id",
            ),
            sa.ForeignKeyConstraint(
                ["superseded_by_id"],
                ["financial_snapshots.id"],
                ondelete="SET NULL",
                use_alter=True,
                name="fk_financial_snapshots_superseded_by_id",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id",
                "period",
                "snapshot_kind",
                "version",
                name="uq_financial_snapshot_period_version",
            ),
        )
        op.create_index(
            "ix_financial_snapshots_household_id", "financial_snapshots", ["household_id"]
        )
        op.create_index(
            "ix_financial_snapshots_trace_id", "financial_snapshots", ["trace_id"]
        )
        op.create_index(
            "ix_financial_snapshots_household_period",
            "financial_snapshots",
            ["household_id", "period"],
        )
        op.create_index(
            "ix_financial_snapshots_household_status",
            "financial_snapshots",
            ["household_id", "status"],
        )

    if "financial_snapshot_lineage" not in tables:
        op.create_table(
            "financial_snapshot_lineage",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("metric_key", sa.String(length=40), nullable=False),
            sa.Column("entity_type", sa.String(length=40), nullable=False),
            sa.Column("entity_id", sa.String(length=80), nullable=True),
            sa.Column("document_id", sa.String(length=36), nullable=True),
            sa.Column("rule_id", sa.String(length=80), nullable=False),
            sa.Column("contribution", sa.Numeric(14, 2), nullable=True),
            sa.Column("source_role", sa.String(length=20), nullable=False),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["snapshot_id"], ["financial_snapshots.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_financial_snapshot_lineage_household_id",
            "financial_snapshot_lineage",
            ["household_id"],
        )
        op.create_index(
            "ix_financial_snapshot_lineage_snapshot_id",
            "financial_snapshot_lineage",
            ["snapshot_id"],
        )
        op.create_index(
            "ix_financial_snapshot_lineage_trace_id",
            "financial_snapshot_lineage",
            ["trace_id"],
        )
        op.create_index(
            "ix_financial_snapshot_lineage_household_snapshot",
            "financial_snapshot_lineage",
            ["household_id", "snapshot_id"],
        )
        op.create_index(
            "ix_financial_snapshot_lineage_snapshot_metric",
            "financial_snapshot_lineage",
            ["snapshot_id", "metric_key"],
        )


def downgrade() -> None:
    tables = _live_tables()
    if op.get_context().as_sql or "financial_snapshot_lineage" in tables:
        op.drop_table("financial_snapshot_lineage")
    if op.get_context().as_sql or "financial_snapshots" in tables:
        op.drop_table("financial_snapshots")

    financial_profile_columns = _live_columns("financial_profiles")
    if op.get_context().as_sql or "central_liquidity_account_id" in financial_profile_columns:
        if not op.get_context().as_sql and op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("financial_profiles") as batch_op:
                batch_op.drop_column("central_liquidity_account_id")
        else:
            op.drop_column("financial_profiles", "central_liquidity_account_id")
