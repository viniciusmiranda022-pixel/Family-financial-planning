# Add auditable obligation payment state.
# Revision ID: 0012
# Revises: 0011

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("obligations")}

    if "status" not in columns:
        op.add_column(
            "obligations",
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        )
    if "paid_at" not in columns:
        op.add_column("obligations", sa.Column("paid_at", sa.Date(), nullable=True))
    if "paid_transaction_id" not in columns:
        op.add_column(
            "obligations",
            sa.Column("paid_transaction_id", sa.String(36), nullable=True),
        )
    if "payment_transaction_created" not in columns:
        op.add_column(
            "obligations",
            sa.Column(
                "payment_transaction_created",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    inspector = sa.inspect(bind)
    foreign_keys = {
        fk.get("name")
        for fk in inspector.get_foreign_keys("obligations")
        if fk.get("name")
    }
    if "fk_obligations_paid_transaction_id_transactions" not in foreign_keys:
        op.create_foreign_key(
            "fk_obligations_paid_transaction_id_transactions",
            "obligations",
            "transactions",
            ["paid_transaction_id"],
            ["id"],
            ondelete="SET NULL",
        )

    inspector = sa.inspect(bind)
    uniques = {
        uc.get("name")
        for uc in inspector.get_unique_constraints("obligations")
        if uc.get("name")
    }
    if "uq_obligations_paid_transaction_id" not in uniques:
        op.create_unique_constraint(
            "uq_obligations_paid_transaction_id",
            "obligations",
            ["paid_transaction_id"],
        )

    op.execute(sa.text("UPDATE obligations SET status = 'pending' WHERE status IS NULL"))


def downgrade() -> None:
    op.drop_constraint("uq_obligations_paid_transaction_id", "obligations", type_="unique")
    op.drop_constraint(
        "fk_obligations_paid_transaction_id_transactions",
        "obligations",
        type_="foreignkey",
    )
    op.drop_column("obligations", "payment_transaction_created")
    op.drop_column("obligations", "paid_transaction_id")
    op.drop_column("obligations", "paid_at")
    op.drop_column("obligations", "status")
