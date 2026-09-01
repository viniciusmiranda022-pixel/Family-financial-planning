"""Add the persistent Financial Integrity Engine core.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
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
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    tables = _live_tables()

    if "integrity_runs" not in tables:
        op.create_table(
            "integrity_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("scope_entity_type", sa.String(length=80), nullable=True),
            sa.Column("scope_entity_id", sa.String(length=80), nullable=True),
            sa.Column("period", sa.String(length=7), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("trigger", sa.String(length=20), nullable=False),
            sa.Column("financial_rules_version", sa.String(length=20), nullable=False),
            sa.Column("calculation_version", sa.String(length=40), nullable=False),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("summary", JSON_DOCUMENT, nullable=True),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_integrity_runs_household_id",
            "integrity_runs",
            ["household_id"],
        )
        op.create_index(
            "ix_integrity_runs_trace_id",
            "integrity_runs",
            ["trace_id"],
            unique=True,
        )
        op.create_index(
            "ix_integrity_runs_household_status",
            "integrity_runs",
            ["household_id", "status"],
        )
        op.create_index(
            "ix_integrity_runs_household_period",
            "integrity_runs",
            ["household_id", "period"],
        )

    if "integrity_findings" not in tables:
        op.create_table(
            "integrity_findings",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("invariant_id", sa.String(length=12), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("financial_rules_version", sa.String(length=20), nullable=False),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=24), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("check_status", sa.String(length=16), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=80), nullable=True),
            sa.Column("period", sa.String(length=7), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("expected_amount", sa.Numeric(precision=14, scale=2), nullable=True),
            sa.Column("actual_amount", sa.Numeric(precision=14, scale=2), nullable=True),
            sa.Column("difference_amount", sa.Numeric(precision=14, scale=2), nullable=True),
            sa.Column("expected", JSON_DOCUMENT, nullable=True),
            sa.Column("actual", JSON_DOCUMENT, nullable=True),
            sa.Column("metadata", JSON_DOCUMENT, nullable=False),
            sa.Column("recommended_action", sa.Text(), nullable=True),
            sa.Column(
                "first_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "last_seen_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("occurrence_count", sa.Integer(), nullable=False),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(length=36), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(length=36), nullable=True),
            sa.Column("resolution_reason", sa.Text(), nullable=True),
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
            sa.ForeignKeyConstraint(
                ["acknowledged_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["resolved_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["run_id"], ["integrity_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id",
                "fingerprint",
                name="uq_integrity_finding_household_fingerprint",
            ),
        )
        op.create_index(
            "ix_integrity_findings_household_id",
            "integrity_findings",
            ["household_id"],
        )
        op.create_index(
            "ix_integrity_findings_run_id", "integrity_findings", ["run_id"]
        )
        op.create_index(
            "ix_integrity_findings_trace_id", "integrity_findings", ["trace_id"]
        )
        for suffix, columns in (
            ("status", ["household_id", "status"]),
            ("severity", ["household_id", "severity"]),
            ("period", ["household_id", "period"]),
            ("invariant", ["household_id", "invariant_id"]),
            ("fingerprint", ["household_id", "fingerprint"]),
        ):
            op.create_index(
                f"ix_integrity_findings_household_{suffix}",
                "integrity_findings",
                columns,
            )

    audit_columns = _live_columns("audit_events")
    for column in (
        sa.Column("before_state", JSON_DOCUMENT, nullable=True),
        sa.Column("after_state", JSON_DOCUMENT, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("request_id", sa.String(length=80), nullable=True),
    ):
        if column.name not in audit_columns:
            op.add_column("audit_events", column)
    if not op.get_context().as_sql:
        index_names = {
            index["name"]
            for index in sa.inspect(op.get_bind()).get_indexes("audit_events")
        }
    else:
        index_names = set()
    if "ix_audit_events_trace_id" not in index_names:
        op.create_index(
            "ix_audit_events_trace_id", "audit_events", ["trace_id"]
        )


def downgrade() -> None:
    tables = _live_tables()
    if op.get_context().as_sql or "audit_events" in tables:
        audit_columns = _live_columns("audit_events")
        if op.get_context().as_sql or "trace_id" in audit_columns:
            op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
        for column_name in (
            "request_id",
            "source",
            "trace_id",
            "reason",
            "after_state",
            "before_state",
        ):
            if op.get_context().as_sql or column_name in audit_columns:
                op.drop_column("audit_events", column_name)
    if op.get_context().as_sql or "integrity_findings" in tables:
        op.drop_table("integrity_findings")
    if op.get_context().as_sql or "integrity_runs" in tables:
        op.drop_table("integrity_runs")
