"""Add reconciliation, balance evidence, duplicate groups and learned rules.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
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


def _live_indexes(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    tables = _live_tables()

    if "document_reconciliations" not in tables:
        op.create_table(
            "document_reconciliations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("parser_name", sa.String(length=80), nullable=False),
            sa.Column("parser_version", sa.String(length=40), nullable=False),
            sa.Column("formula_id", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("period", sa.String(length=7), nullable=True),
            sa.Column("declared_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("reconstructed_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("difference", sa.Numeric(14, 2), nullable=True),
            sa.Column("opening_balance", sa.Numeric(14, 2), nullable=True),
            sa.Column("closing_balance_declared", sa.Numeric(14, 2), nullable=True),
            sa.Column("closing_balance_calculated", sa.Numeric(14, 2), nullable=True),
            sa.Column("credits_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("debits_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("purchases_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("fees_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("refunds_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("payments_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("tolerance", sa.Numeric(14, 2), nullable=False),
            sa.Column("coverage", JSON_DOCUMENT, nullable=False),
            sa.Column("evidence", JSON_DOCUMENT, nullable=False),
            sa.Column("reason", sa.String(length=120), nullable=True),
            sa.Column(
                "reconciled_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_document_reconciliations_document_id",
            "document_reconciliations",
            ["document_id"],
        )
        op.create_index(
            "ix_document_reconciliations_household_id",
            "document_reconciliations",
            ["household_id"],
        )
        op.create_index(
            "ix_document_reconciliations_trace_id",
            "document_reconciliations",
            ["trace_id"],
        )
        op.create_index(
            "ix_document_reconciliations_household_document",
            "document_reconciliations",
            ["household_id", "document_id"],
        )

    if "account_balance_observations" not in tables:
        op.create_table(
            "account_balance_observations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("as_of_date", sa.Date(), nullable=False),
            sa.Column("observation_type", sa.String(length=24), nullable=False),
            sa.Column("source", sa.String(length=30), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=True),
            sa.Column("confirmed_by", sa.String(length=36), nullable=True),
            sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
            sa.Column("supersedes_id", sa.String(length=36), nullable=True),
            sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("invalidated_by", sa.String(length=36), nullable=True),
            sa.Column("invalidation_reason", sa.Text(), nullable=True),
            sa.Column("trace_id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["account_id"], ["accounts.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["confirmed_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["invalidated_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["superseded_by_id"],
                ["account_balance_observations.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["supersedes_id"],
                ["account_balance_observations.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_account_balance_observations_account_id",
            "account_balance_observations",
            ["account_id"],
        )
        op.create_index(
            "ix_account_balance_observations_household_id",
            "account_balance_observations",
            ["household_id"],
        )
        op.create_index(
            "ix_account_balance_observations_trace_id",
            "account_balance_observations",
            ["trace_id"],
        )
        op.create_index(
            "ix_account_balance_observations_account_date",
            "account_balance_observations",
            ["household_id", "account_id", "as_of_date"],
        )

    if "duplicate_groups" not in tables:
        op.create_table(
            "duplicate_groups",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("group_key", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
            sa.Column("resolution", sa.String(length=40), nullable=True),
            sa.Column("canonical_transaction_id", sa.String(length=36), nullable=True),
            sa.Column("signals", JSON_DOCUMENT, nullable=False),
            sa.Column("rule_version", sa.String(length=40), nullable=False),
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
                ["canonical_transaction_id"],
                ["transactions.id"],
                ondelete="SET NULL",
                use_alter=True,
                name="fk_duplicate_group_canonical_transaction",
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["resolved_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id",
                "group_key",
                name="uq_duplicate_group_household_key",
            ),
        )
        op.create_index(
            "ix_duplicate_groups_household_id",
            "duplicate_groups",
            ["household_id"],
        )
        op.create_index(
            "ix_duplicate_groups_household_status",
            "duplicate_groups",
            ["household_id", "status"],
        )

    transaction_columns = _live_columns("transactions")
    columns = (
        sa.Column("occurred_at", sa.Date(), nullable=True),
        sa.Column("competence", sa.String(length=7), nullable=True),
        sa.Column(
            "classification_source",
            sa.String(length=30),
            server_default="legacy",
            nullable=False,
        ),
        sa.Column("classification_version", sa.String(length=40), nullable=True),
        sa.Column(
            "canonical_status",
            sa.String(length=24),
            server_default="unassigned",
            nullable=False,
        ),
        sa.Column(
            "duplicate_group_id",
            sa.String(length=36),
            sa.ForeignKey(
                "duplicate_groups.id",
                ondelete="SET NULL",
                name="fk_transactions_duplicate_group_id",
            ),
            nullable=True,
        ),
        sa.Column(
            "linked_transaction_id",
            sa.String(length=36),
            sa.ForeignKey(
                "transactions.id",
                ondelete="SET NULL",
                name="fk_transactions_linked_transaction_id",
            ),
            nullable=True,
        ),
        sa.Column("transfer_group_id", sa.String(length=36), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=True),
        sa.Column(
            "source_priority",
            sa.Integer(),
            server_default="50",
            nullable=False,
        ),
    )
    missing_columns = tuple(column for column in columns if column.name not in transaction_columns)
    if not op.get_context().as_sql and op.get_bind().dialect.name == "sqlite":
        # SQLite cannot add a column carrying a foreign key with plain ALTER.
        # Batch mode rebuilds the table while preserving every existing row.
        with op.batch_alter_table("transactions") as batch_op:
            for column in missing_columns:
                batch_op.add_column(column)
    else:
        for column in missing_columns:
            op.add_column("transactions", column)
    transaction_indexes = _live_indexes("transactions")
    for name, index_columns in (
        ("ix_transactions_duplicate_group_id", ["duplicate_group_id"]),
        ("ix_transactions_trace_id", ["trace_id"]),
        (
            "ix_transaction_household_competence",
            ["household_id", "competence"],
        ),
    ):
        if name not in transaction_indexes:
            op.create_index(name, "transactions", index_columns)

    if "duplicate_group_members" not in tables:
        op.create_table(
            "duplicate_group_members",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("group_id", sa.String(length=36), nullable=False),
            sa.Column("transaction_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False),
            sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
            sa.Column("signals", JSON_DOCUMENT, nullable=False),
            sa.Column("source_priority", sa.Integer(), nullable=False),
            sa.Column("excluded_by_policy", sa.Boolean(), nullable=False),
            sa.Column(
                "added_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["group_id"], ["duplicate_groups.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["transaction_id"], ["transactions.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "group_id",
                "transaction_id",
                name="uq_duplicate_group_member_transaction",
            ),
        )
        op.create_index(
            "ix_duplicate_group_members_group_id",
            "duplicate_group_members",
            ["group_id"],
        )
        op.create_index(
            "ix_duplicate_group_members_household_id",
            "duplicate_group_members",
            ["household_id"],
        )
        op.create_index(
            "ix_duplicate_group_members_transaction_id",
            "duplicate_group_members",
            ["transaction_id"],
        )

    if "classification_rules" not in tables:
        op.create_table(
            "classification_rules",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("household_id", sa.String(length=36), nullable=False),
            sa.Column("normalized_merchant", sa.String(length=500), nullable=False),
            sa.Column("category_id", sa.String(length=36), nullable=False),
            sa.Column("movement_type", sa.String(length=30), nullable=False),
            sa.Column("confirmation_count", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_from_correction", sa.Boolean(), nullable=False),
            sa.Column(
                "last_confirmed_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_by", sa.String(length=36), nullable=True),
            sa.Column("evidence", JSON_DOCUMENT, nullable=False),
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
                ["accepted_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["category_id"], ["categories.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["household_id"], ["households.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "household_id",
                "normalized_merchant",
                "category_id",
                "movement_type",
                name="uq_classification_rule_consistent_choice",
            ),
        )
        op.create_index(
            "ix_classification_rules_category_id",
            "classification_rules",
            ["category_id"],
        )
        op.create_index(
            "ix_classification_rules_household_id",
            "classification_rules",
            ["household_id"],
        )
        op.create_index(
            "ix_classification_rules_household_merchant",
            "classification_rules",
            ["household_id", "normalized_merchant"],
        )


def downgrade() -> None:
    tables = _live_tables()
    for table_name in ("classification_rules", "duplicate_group_members"):
        if op.get_context().as_sql or table_name in tables:
            op.drop_table(table_name)

    transaction_columns = _live_columns("transactions")
    transaction_indexes = _live_indexes("transactions")
    for index_name in (
        "ix_transaction_household_competence",
        "ix_transactions_trace_id",
        "ix_transactions_duplicate_group_id",
    ):
        if op.get_context().as_sql or index_name in transaction_indexes:
            op.drop_index(index_name, table_name="transactions")
    column_names = (
        "source_priority",
        "trace_id",
        "transfer_group_id",
        "linked_transaction_id",
        "duplicate_group_id",
        "canonical_status",
        "classification_version",
        "classification_source",
        "competence",
        "occurred_at",
    )
    if not op.get_context().as_sql and op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("transactions") as batch_op:
            for column_name in column_names:
                if column_name in transaction_columns:
                    batch_op.drop_column(column_name)
    else:
        for column_name in column_names:
            if op.get_context().as_sql or column_name in transaction_columns:
                op.drop_column("transactions", column_name)

    for table_name in (
        "duplicate_groups",
        "account_balance_observations",
        "document_reconciliations",
    ):
        if op.get_context().as_sql or table_name in tables:
            op.drop_table(table_name)
