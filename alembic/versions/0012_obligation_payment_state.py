# Add auditable obligation payment state.
# Revision ID: 0012
# Revises: 0011

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
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
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _live_foreign_keys(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {fk.get("name") for fk in sa.inspect(op.get_bind()).get_foreign_keys(table_name) if fk.get("name")}


def _live_unique_constraints(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {
        uc.get("name")
        for uc in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if uc.get("name")
    }


def upgrade() -> None:
    columns = _live_columns("obligations")

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

    foreign_keys = _live_foreign_keys("obligations")
    # SQLite has no `ALTER TABLE ADD CONSTRAINT` -- `batch_alter_table` is
    # Alembic's own documented cross-dialect answer (copy-and-move on
    # SQLite, a plain in-place ALTER on PostgreSQL/others), so this remains
    # one migration instead of a SQLite-only branch.
    if "fk_obligations_paid_transaction_id_transactions" not in foreign_keys:
        with op.batch_alter_table("obligations") as batch_op:
            batch_op.create_foreign_key(
                "fk_obligations_paid_transaction_id_transactions",
                "transactions",
                ["paid_transaction_id"],
                ["id"],
                ondelete="SET NULL",
            )

    uniques = _live_unique_constraints("obligations")
    if "uq_obligations_paid_transaction_id" not in uniques:
        with op.batch_alter_table("obligations") as batch_op:
            batch_op.create_unique_constraint(
                "uq_obligations_paid_transaction_id",
                ["paid_transaction_id"],
            )

    op.execute(sa.text("UPDATE obligations SET status = 'pending' WHERE status IS NULL"))


def downgrade() -> None:
    with op.batch_alter_table("obligations") as batch_op:
        batch_op.drop_constraint("uq_obligations_paid_transaction_id", type_="unique")
        batch_op.drop_constraint(
            "fk_obligations_paid_transaction_id_transactions",
            type_="foreignkey",
        )
    op.drop_column("obligations", "payment_transaction_created")
    op.drop_column("obligations", "paid_transaction_id")
    op.drop_column("obligations", "paid_at")
    op.drop_column("obligations", "status")
