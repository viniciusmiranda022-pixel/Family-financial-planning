# Card cycles and obligation funding.
# Revision ID: 0013
# Revises: 0012

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _live_columns(table_name: str) -> set[str]:
    # Offline SQL generation (`alembic upgrade head --sql`, exercised by
    # tests/test_migrations.py::test_migrations_render_valid_postgresql_ddl_offline)
    # has no live connection to inspect -- matches the established pattern in
    # 0006/0009/0010/0011 (`op.get_context().as_sql`): render every
    # operation unconditionally instead of raising or silently skipping it.
    if op.get_context().as_sql:
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table_name)}


def _live_foreign_keys(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {fk.get("name") for fk in sa.inspect(op.get_bind()).get_foreign_keys(table_name) if fk.get("name")}


def upgrade() -> None:
    account_cols = _live_columns("accounts")
    if "card_closing_day" not in account_cols:
        op.add_column("accounts", sa.Column("card_closing_day", sa.Integer(), nullable=True))
    if "card_due_day" not in account_cols:
        op.add_column("accounts", sa.Column("card_due_day", sa.Integer(), nullable=True))

    obligation_cols = _live_columns("obligations")
    if "funding_source" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_source", sa.String(20), nullable=False, server_default="account"))
    if "funding_transaction_id" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_transaction_id", sa.String(36), nullable=True))
    if "funding_balance_observation_id" not in obligation_cols:
        op.add_column("obligations", sa.Column("funding_balance_observation_id", sa.String(36), nullable=True))

    fk_names = _live_foreign_keys("obligations")
    # SQLite has no `ALTER TABLE ADD CONSTRAINT` -- `batch_alter_table` is
    # Alembic's own documented cross-dialect answer (copy-and-move on
    # SQLite, a plain in-place ALTER on PostgreSQL/others), so this stays
    # one migration instead of a SQLite-only branch. Both foreign keys land
    # in the same batch: two separate batch blocks would each recreate the
    # whole table for no benefit.
    missing = {
        "fk_obligations_funding_transaction_id_transactions",
        "fk_obligations_funding_balance_observation_id",
    } - fk_names
    if missing:
        with op.batch_alter_table("obligations") as batch_op:
            if "fk_obligations_funding_transaction_id_transactions" in missing:
                batch_op.create_foreign_key(
                    "fk_obligations_funding_transaction_id_transactions",
                    "transactions", ["funding_transaction_id"], ["id"], ondelete="SET NULL",
                )
            if "fk_obligations_funding_balance_observation_id" in missing:
                batch_op.create_foreign_key(
                    "fk_obligations_funding_balance_observation_id",
                    "account_balance_observations", ["funding_balance_observation_id"], ["id"], ondelete="SET NULL",
                )


def downgrade() -> None:
    with op.batch_alter_table("obligations") as batch_op:
        batch_op.drop_constraint("fk_obligations_funding_balance_observation_id", type_="foreignkey")
        batch_op.drop_constraint("fk_obligations_funding_transaction_id_transactions", type_="foreignkey")
    op.drop_column("obligations", "funding_balance_observation_id")
    op.drop_column("obligations", "funding_transaction_id")
    op.drop_column("obligations", "funding_source")
    op.drop_column("accounts", "card_due_day")
    op.drop_column("accounts", "card_closing_day")
