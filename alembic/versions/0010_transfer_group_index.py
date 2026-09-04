"""Index Transaction.transfer_group_id (manual go-live flows).

Revision ID: 0010
Revises: 0009

`transfer_group_id` has existed on `transactions` since migration 0004, but
no code path ever populated it -- a real double-entry transfer between two
of the household's own accounts was not implemented. The manual go-live
slice (`docs/WORK_ORDER_MANUAL_FINANCIAL_FLOWS_GO_LIVE.md`) starts writing
it (`app.api._create_manual_transfer`) to pair a transfer's origin and
destination legs, and reads it back to delete both legs together
(`app.api.delete_manual_transaction`). This migration only adds the index
that lookup needs; it changes no existing column, value or row.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_transactions_transfer_group_id"
_TABLE_NAME = "transactions"


def _live_indexes(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    # Additive only: a plain index on an existing, already-nullable column.
    # No column is added, dropped or retyped; no row is read or rewritten.
    if _INDEX_NAME not in _live_indexes(_TABLE_NAME):
        op.create_index(_INDEX_NAME, _TABLE_NAME, ["transfer_group_id"])


def downgrade() -> None:
    # Safe: dropping the index removes only a lookup structure, never data.
    if op.get_context().as_sql or _INDEX_NAME in _live_indexes(_TABLE_NAME):
        op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
