"""Initial auditable financial schema.

Revision ID: 0001
Revises:

This revision is intentionally self-contained. Never import the live ORM metadata
from a historical migration: doing so makes a fresh database depend on whichever
models happen to exist when the migration is executed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_household_id", "users", ["household_id"])
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("institution", sa.String(length=80), nullable=False),
        sa.Column("account_type", sa.String(length=30), nullable=False),
        sa.Column("owner_label", sa.String(length=80), nullable=False),
        sa.Column("last_four", sa.String(length=4), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "name", name="uq_account_household_name"),
    )
    op.create_index("ix_accounts_household_id", "accounts", ["household_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("cash_cap", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("essential", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "name", name="uq_category_household_name"),
    )
    op.create_index("ix_categories_household_id", "categories", ["household_id"])

    op.create_table(
        "commissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("expected_date", sa.Date(), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=7, scale=6), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_commissions_household_id", "commissions", ["household_id"])

    op.create_table(
        "financial_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("monthly_salary_net", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("monthly_cash_cap", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("emergency_floor", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("food_allowance", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("meal_allowance_daily", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("workdays_month", sa.Integer(), nullable=False),
        sa.Column("investment_name", sa.String(length=120), nullable=False),
        sa.Column("investment_balance", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column(
            "investment_gross_annual_rate",
            sa.Numeric(precision=9, scale=8),
            nullable=False,
        ),
        sa.Column(
            "investment_income_tax_rate",
            sa.Numeric(precision=9, scale=8),
            nullable=False,
        ),
        sa.Column("projection_end", sa.Date(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", name="uq_financial_profile_household"),
    )
    op.create_index("ix_financial_profiles_household_id", "financial_profiles", ["household_id"])

    op.create_table(
        "obligations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("recurrence_months", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_obligations_household_id", "obligations", ["household_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_household_id", "audit_events", ["household_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("document_type", sa.String(length=40), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("encrypted_path", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "sha256", name="uq_document_household_sha"),
    )
    op.create_index("ix_documents_household_id", "documents", ["household_id"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])

    op.create_table(
        "payroll_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("person_name", sa.String(length=120), nullable=False),
        sa.Column("competence", sa.Date(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("payroll_kind", sa.String(length=30), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("deductions", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("net_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("payroll_loan", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payroll_records_household_id", "payroll_records", ["household_id"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("category_id", sa.String(length=36), nullable=True),
        sa.Column("booked_at", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("normalized_description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("transaction_type", sa.String(length=30), nullable=False),
        sa.Column("owner_label", sa.String(length=80), nullable=False),
        sa.Column("card_last_four", sa.String(length=4), nullable=True),
        sa.Column("installment_current", sa.Integer(), nullable=True),
        sa.Column("installment_total", sa.Integer(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("possible_duplicate", sa.Boolean(), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_fingerprint", "transactions", ["household_id", "fingerprint"])
    op.create_index("ix_transaction_household_date", "transactions", ["household_id", "booked_at"])
    op.create_index("ix_transactions_household_id", "transactions", ["household_id"])
    op.create_index("ix_transactions_normalized_description", "transactions", ["normalized_description"])

    op.create_table(
        "review_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("transaction_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_items_household_id", "review_items", ["household_id"])


def downgrade() -> None:
    op.drop_table("review_items")
    op.drop_table("transactions")
    op.drop_table("payroll_records")
    op.drop_table("documents")
    op.drop_table("audit_events")
    op.drop_table("obligations")
    op.drop_table("financial_profiles")
    op.drop_table("commissions")
    op.drop_table("categories")
    op.drop_table("accounts")
    op.drop_table("users")
    op.drop_table("households")
