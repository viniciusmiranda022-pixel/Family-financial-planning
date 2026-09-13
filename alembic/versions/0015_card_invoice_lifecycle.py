"""Card invoice lifecycle: card_invoices table, Transaction.card_invoice_id,
Transaction.refund_of_transaction_id.

Revision ID: 0015
Revises: 0014

P0 #87, October Go-Live Slice 2 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_2.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.1/§3.2): purely additive schema
for the card-invoice lifecycle (open -> closed -> partially_paid -> paid),
partial-payment principal carry-forward, and explicit refund/estorno
lineage.

- `card_invoices` is a brand-new table. Every row is derived, read-only,
  from already-existing `Transaction` history the moment
  `app.services.card_invoice_lifecycle` first syncs a given
  `(account, competence)` pair (explicitly, via `POST /card-invoices/sync`,
  `.../close`, `.../pay` or `.../divergence` -- this project's own "leitura
  preguiçosa no primeiro acesso" trigger, since no cron exists yet) --
  nothing here writes to `Transaction`, `Document`, `PayrollRecord`,
  `Commission` or `Obligation`.
- `transactions.card_invoice_id` (FK, nullable, `ON DELETE SET NULL`) links
  a purchase/payment `Transaction` to the `CardInvoice` it belongs to --
  same additive pattern as `linked_transaction_id` (migration 0004). An
  existing `Transaction` keeps `card_invoice_id IS NULL` until the first
  sync of its `(account, competence)` claims it; the link is set exactly
  once and never rewritten by a later sync.
- `transactions.refund_of_transaction_id` (FK, nullable, self-referential,
  `ON DELETE SET NULL`) records an explicit, human/Assistant-confirmed
  link from a refund transaction to the original purchase it neutralizes
  (rebaseline §6.6) -- never inferred, never written by this migration.

Downgrade drops both new columns and the new table -- safe for
`card_invoices`/`card_invoice_id` under the same rule every other
migration in this project follows: both are pure derived cache, always
reconstructible by resyncing from already-existing `Transaction` history
(`Transaction.amount`/`transaction_type`/`competence`/`excluded` are never
touched by this migration in either direction).

`refund_of_transaction_id` is different -- engineering review round
(2026-09-12, `BLOQUEIO DE MERGE` #5): it is an explicit, human/Assistant-
*confirmed* fact (rebaseline §6.6's estorno lineage), not a value this
project could recompute from anything else if lost. Downgrading with any
row already linked would silently discard that confirmed lineage, which
this project's migration discipline (`docs/OCTOBER_GO_LIVE_REBASELINE.md`
§52/§75: "nunca... transformação destrutiva silenciosa") forbids just as
much as deleting a `Transaction`. `downgrade()` therefore refuses (raises,
does not proceed) when any `refund_of_transaction_id` is still set --
export the lineage first (see the query in `downgrade()`'s docstring
below), then retry.
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation (`alembic upgrade head --sql`, exercised by
    # tests/test_migrations.py::test_migrations_render_valid_postgresql_ddl_offline)
    # has no live connection to inspect -- matches the established pattern
    # in 0006/0009/0010/0011/0013/0014 (`op.get_context().as_sql`): render
    # every operation unconditionally instead of raising or silently
    # skipping it.
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def _live_columns(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table_name)}


def _live_foreign_keys(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {fk.get("name") for fk in sa.inspect(op.get_bind()).get_foreign_keys(table_name) if fk.get("name")}


def upgrade() -> None:
    if "card_invoices" not in _live_tables():
        op.create_table(
            "card_invoices",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "account_id",
                sa.String(36),
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("competence", sa.String(7), nullable=False),
            sa.Column("opens_at", sa.Date(), nullable=True),
            sa.Column("closes_at", sa.Date(), nullable=True),
            sa.Column("due_date", sa.Date(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("declared_total", sa.Numeric(14, 2), nullable=True),
            sa.Column("computed_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("principal_carried_in", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("principal_carried_out", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("paid_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("trace_id", sa.String(36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("account_id", "competence", name="uq_card_invoice_account_competence"),
        )
        op.create_index("ix_card_invoice_household_id", "card_invoices", ["household_id"])
        op.create_index("ix_card_invoice_account_id", "card_invoices", ["account_id"])
        op.create_index(
            "ix_card_invoice_household_competence", "card_invoices", ["household_id", "competence"]
        )
        op.create_index("ix_card_invoice_trace_id", "card_invoices", ["trace_id"])

    transaction_cols = _live_columns("transactions")
    if "card_invoice_id" not in transaction_cols:
        op.add_column("transactions", sa.Column("card_invoice_id", sa.String(36), nullable=True))
    if "refund_of_transaction_id" not in transaction_cols:
        op.add_column("transactions", sa.Column("refund_of_transaction_id", sa.String(36), nullable=True))

    if "card_invoice_id" not in transaction_cols:
        op.create_index("ix_transaction_card_invoice_id", "transactions", ["card_invoice_id"])

    # SQLite has no `ALTER TABLE ADD CONSTRAINT` -- `batch_alter_table` is
    # Alembic's own documented cross-dialect answer (copy-and-move on
    # SQLite, a plain in-place ALTER on PostgreSQL/others), matching the
    # exact pattern already used by 0013 for `obligations`. Both foreign
    # keys land in the same batch: two separate batch blocks would each
    # recreate the whole table for no benefit.
    fk_names = _live_foreign_keys("transactions")
    missing = {
        "fk_transactions_card_invoice_id_card_invoices",
        "fk_transactions_refund_of_transaction_id_transactions",
    } - fk_names
    if missing:
        with op.batch_alter_table("transactions") as batch_op:
            if "fk_transactions_card_invoice_id_card_invoices" in missing:
                batch_op.create_foreign_key(
                    "fk_transactions_card_invoice_id_card_invoices",
                    "card_invoices",
                    ["card_invoice_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
            if "fk_transactions_refund_of_transaction_id_transactions" in missing:
                batch_op.create_foreign_key(
                    "fk_transactions_refund_of_transaction_id_transactions",
                    "transactions",
                    ["refund_of_transaction_id"],
                    ["id"],
                    ondelete="SET NULL",
                )


def downgrade() -> None:
    """Refuses (raises `RuntimeError`, changes nothing) when any
    `transactions.refund_of_transaction_id` is still set -- that column is
    a confirmed fact (rebaseline §6.6), never mere derived cache; see the
    module docstring. Export the lineage first:

        SELECT id, refund_of_transaction_id FROM transactions
        WHERE refund_of_transaction_id IS NOT NULL;

    keep that result outside the database, then retry the downgrade once it
    is safe to lose the column; reapply the links from the export after
    restoring 0015 (or an equivalent future migration). No other financial
    fact (`Transaction.amount`/`transaction_type`/`competence`/`excluded`,
    or any row's existence) is ever at risk from this downgrade either way
    -- only this explicit refund lineage, and only when at least one link
    has actually been confirmed.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches this migration's own `upgrade()`
    pattern (`_live_tables`/`_live_columns`) of never guessing in that mode.
    """

    if not op.get_context().as_sql:
        linked_refund_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM transactions WHERE refund_of_transaction_id IS NOT NULL")
        ).scalar()
        if linked_refund_count:
            raise RuntimeError(
                f"Downgrade de 0015 abortado: {linked_refund_count} vínculo(s) confirmado(s) de "
                "estorno (transactions.refund_of_transaction_id) seriam perdidos silenciosamente. "
                "Exporte a lineage primeiro com "
                "`SELECT id, refund_of_transaction_id FROM transactions WHERE "
                "refund_of_transaction_id IS NOT NULL`, preserve o resultado fora do banco, e só "
                "então repita o downgrade."
            )
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(
            "fk_transactions_refund_of_transaction_id_transactions", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_transactions_card_invoice_id_card_invoices", type_="foreignkey")
    op.drop_index("ix_transaction_card_invoice_id", table_name="transactions")
    op.drop_column("transactions", "refund_of_transaction_id")
    op.drop_column("transactions", "card_invoice_id")
    op.drop_table("card_invoices")
