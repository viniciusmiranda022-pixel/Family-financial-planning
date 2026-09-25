"""Installment regressions after PR #115: transactions.installment_contracted_total.

Revision ID: 0029
Revises: 0028

Work Order (`docs/WORK_ORDER_INSTALLMENT_REGRESSIONS_PR115.md`, item 3):
"Preserve the user-stated contracted total when equal installments require
cent rounding." Splitting a stated total across N equal installments in
whole cents can lose a cent (`money(100 / 3) * 3 == 99.99`, not the stated
`100.00`) -- `card_invoice_lifecycle.serialize_invoice_purchase_line` and
`assistant_tools._tool_get_installments` both currently re-derive "compra
contratada" as `amount * installment_total`, which silently replaces the
stated fact with that rounded reconstruction (rebaseline §6.7: "a compra
contratada preserva o valor total").

Purely additive: one new nullable column, no backfill. Only the chat/typed-
action WRITE path (`app.services.assistant_actions._propose_create_transaction`)
populates it going forward, and only when the user actually stated a total
distinct from the per-installment amount; manual entry and document
capture/import leave it `NULL` exactly as before, and every existing row
keeps `NULL` -- no historical installment purchase is reinterpreted. Every
reader prefers this column when set and falls back to the pre-existing
`amount * installment_total` derivation when it is `NULL`, so behavior for
every row that predates this migration (or every row from a write path that
never sets it) is unchanged.
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _live_columns(table_name: str) -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain.
    if op.get_context().as_sql:
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "installment_contracted_total" not in _live_columns("transactions"):
        op.add_column(
            "transactions",
            sa.Column("installment_contracted_total", sa.Numeric(14, 2), nullable=True),
        )


def downgrade() -> None:
    """Unguarded: this column only ever caches a display convenience
    (rebaseline §6.7's "compra contratada" figure) alongside the always-
    canonical `amount`/`installment_current`/`installment_total`, never a
    fact those three do not already carry on their own -- dropping it only
    makes every reader fall back to the pre-existing `amount *
    installment_total` derivation it already had to support for rows this
    migration never touches. No `Transaction.amount`, `transaction_type`,
    `competence`, or `excluded` value is ever at risk from this downgrade.

    Offline SQL generation has no live connection to check against -- same
    pattern as `upgrade()`.
    """

    op.drop_column("transactions", "installment_contracted_total")
