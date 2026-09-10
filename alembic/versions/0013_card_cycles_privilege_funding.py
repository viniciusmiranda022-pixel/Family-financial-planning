# Card cycles and obligation funding.
# Revision ID: 0013
# Revises: 0012

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    account_cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "card_closing_day" not in account_cols:
        op.add_column("accounts", sa.Column("card_closing_day", sa.Integer(), nullable=True))
    if "card_due_day" not in account_cols:
        op.add_column("accounts", sa.Column("card_due_day", sa.Integer(), nullable=True))

    obligation_cols = {c["name"] for c in inspector.get_columns("obligations")}
    if "funding_source" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_source", sa.String(20), nullable=False, server_default="account"))
    if "funding_transaction_id" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_transaction_id", sa.String(36), nullable=True))
    if "funding_balance_observation_id" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_balance_observation_id", sa.String(36), nullable=True))

    inspector = sa.inspect(bind)
    fk_names = {fk.get("name") for fk in inspector.get_foreign_keys("obligations") if fk.get("name")}
    if "fk_obligations_funding_transaction_id_transactions" not in fk_names:
        op.create_foreign_key(
            "fk_obligations_funding_transaction_id_transactions",
            "obligations", "transactions", ["funding_transaction_id"], ["id"], ondelete="SET NULL",
        )
    if "fk_obligations_funding_balance_observation_id" not in fk_names:
        op.create_foreign_key(
            "fk_obligations_funding_balance_observation_id",
            "obligations", "account_balance_observations", ["funding_balance_observation_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint("fk_obligations_funding_balance_observation_id", "obligations", type_="foreignkey")
    op.drop_constraint("fk_obligations_funding_transaction_id_transactions", "obligations", type_="foreignkey")
    op.drop_column("obligations", "funding_balance_observation_id")
    op.drop_column("obligations", "funding_transaction_id")
    op.drop_column("obligations", "funding_source")
    op.drop_column("accounts", "card_due_day")
    op.drop_column("accounts", "card_closing_day")
