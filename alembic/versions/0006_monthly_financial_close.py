"""Add monthly financial close lifecycle and finding acknowledgement reason.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


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
    if "monthly_financial_closes" not in tables:
        op.create_table(
            "monthly_financial_closes",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("household_id", sa.String(36), nullable=False),
            sa.Column("period", sa.String(7), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("snapshot_id", sa.String(36), nullable=True),
            sa.Column("integrity_run_id", sa.String(36), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_by", sa.String(36), nullable=True),
            sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reopened_by", sa.String(36), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
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
            sa.ForeignKeyConstraint(
                ["snapshot_id"], ["financial_snapshots.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["integrity_run_id"], ["integrity_runs.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(["closed_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["reopened_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id", "period", name="uq_monthly_financial_close_household_period"
            ),
        )
        for column in ("household_id", "period", "status", "snapshot_id", "integrity_run_id"):
            op.create_index(
                f"ix_monthly_financial_closes_{column}", "monthly_financial_closes", [column]
            )
        op.create_index(
            "ix_monthly_financial_closes_household_status",
            "monthly_financial_closes",
            ["household_id", "status"],
        )

    # Additive column: the "motivo obrigatório" audit trail for the human
    # `acknowledge` action, mirroring the already-existing `resolution_reason`
    # column that `resolve`/`ignore`/`false-positive` share (see
    # docs/FINANCIAL_RULES.md "Integridade persistente" and
    # docs/WORK_ORDER_PR7_INTEGRITY_UI_MONTHLY_CLOSE.md item 4). No existing
    # column doubled as an "acknowledgement reason", so this fills that gap
    # without repurposing `resolution_reason` (reserved for the terminal
    # resolve/ignore/false-positive actions) or touching any existing row.
    finding_columns = _live_columns("integrity_findings")
    if "acknowledgement_reason" not in finding_columns:
        op.add_column(
            "integrity_findings", sa.Column("acknowledgement_reason", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    finding_columns = _live_columns("integrity_findings")
    if op.get_context().as_sql or "acknowledgement_reason" in finding_columns:
        op.drop_column("integrity_findings", "acknowledgement_reason")

    tables = _live_tables()
    if op.get_context().as_sql or "monthly_financial_closes" in tables:
        op.drop_table("monthly_financial_closes")
