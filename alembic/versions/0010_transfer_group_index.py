"""Add household+transfer_group_id index on transactions.

Revision ID: 0010
Revises: 0009

Go-live manual slice 2 (`docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md`):
`transactions.transfer_group_id` already exists since migration `0004`, but
was deliberately left unindexed until a feature actually queried by it --
see the engineering review on PR 47 and `tests/test_manual_financial_flows.py`'s
module docstring ("the associated transfer_group_id index belong[s] to
slices 2/3"). The structured transfer command introduced in this slice
(`app/services/transfers.py::create_internal_transfer`) looks both legs of
a transfer up by `(household_id, transfer_group_id)` on every delete/audit
lookup, so this index is now load-bearing.

Purely additive: a new index on an existing, already-nullable column.
No column is added, dropped or reinterpreted, no row is rewritten, and no
existing index is touched -- an installation already on `0009` upgrades
with no data migration and no lock beyond the index build itself.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_transaction_household_transfer_group"


def _live_indexes(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    if op.get_context().as_sql or _INDEX_NAME not in _live_indexes("transactions"):
        op.create_index(_INDEX_NAME, "transactions", ["household_id", "transfer_group_id"])


def downgrade() -> None:
    # Safe: dropping an index never touches row data, and no other object
    # (foreign key, view, constraint) depends on this index existing.
    if op.get_context().as_sql or _INDEX_NAME in _live_indexes("transactions"):
        op.drop_index(_INDEX_NAME, table_name="transactions")
