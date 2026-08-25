"""Add auditable smart-capture drafts.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 creates metadata dynamically.  On a brand-new installation
    # the current model may therefore already include this table.
    if "capture_drafts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "capture_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("household_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("detected_type", sa.String(length=40), nullable=False, server_default="text"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="preview"),
        sa.Column("processor", sa.String(length=60), nullable=False, server_default="local_rules"),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("proposal_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_capture_drafts_household_id", "capture_drafts", ["household_id"])
    op.create_index(
        "ix_capture_household_created", "capture_drafts", ["household_id", "created_at"]
    )


def downgrade() -> None:
    if "capture_drafts" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("capture_drafts")
