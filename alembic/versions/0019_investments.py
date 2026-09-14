"""Investments/assets and their valuation history: investments,
investment_valuations tables.

Revision ID: 0019
Revises: 0018

P0 #87, October Go-Live Slice 6 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.4): purely additive schema for
patrimonial assets/investments (including the Studio), keeping cost basis
(`historical_cost`), current value (`current_value`) and future projection
(`expected_receivable_value`) as three distinct columns so no consumer can
ever sum them into the investments-only subtotal by construction -- see
`app.services.investments.investments_summary`, which reads `current_value`
only.

`investments` holds the current state; `investment_valuations` is an
append-only history of every valuation update and contribution, mirroring
`account_balance_observations`' immutable-observation pattern -- a
correction never rewrites a prior row, it appends a new one and, when
undone, invalidates (never deletes) the row it is reversing.

There is no legacy `Investment`/`Studio` model or data anywhere in this
codebase to migrate (confirmed by inventory: `docs/` planning references
only). This migration therefore has nothing to backfill.

Nothing here touches `transactions`, `accounts`, `financial_profiles`, or
any other existing table -- this migration only adds two new tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017, 0018).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "investments" not in live_tables:
        op.create_table(
            "investments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column(
                "historical_cost",
                sa.Numeric(14, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "current_value",
                sa.Numeric(14, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column("expected_receivable_value", sa.Numeric(14, 2), nullable=True),
            sa.Column("last_updated_at", sa.Date(), nullable=False),
            sa.Column("expected_receipt_date", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_investments_household_id", "investments", ["household_id"])

    if "investment_valuations" not in live_tables:
        op.create_table(
            "investment_valuations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "investment_id",
                sa.String(36),
                sa.ForeignKey("investments.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("valuation_date", sa.Date(), nullable=False),
            sa.Column("historical_cost", sa.Numeric(14, 2), nullable=False),
            sa.Column("current_value", sa.Numeric(14, 2), nullable=False),
            sa.Column("expected_receivable_value", sa.Numeric(14, 2), nullable=True),
            sa.Column("contribution_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column(
                "funding_transaction_id",
                sa.String(36),
                sa.ForeignKey("transactions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column(
                "recorded_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "invalidated_by",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("invalidation_reason", sa.Text(), nullable=True),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_investment_valuations_household_id", "investment_valuations", ["household_id"]
        )
        op.create_index(
            "ix_investment_valuations_investment_id", "investment_valuations", ["investment_id"]
        )
        op.create_index(
            "ix_investment_valuations_trace_id", "investment_valuations", ["trace_id"]
        )
        op.create_index(
            "ix_investment_valuations_investment_date",
            "investment_valuations",
            ["household_id", "investment_id", "valuation_date"],
        )


def downgrade() -> None:
    """Guarded like migration 0016's downgrade, not unconditional like
    0018's: `investment_valuations` is the only durable record of every
    valuation/contribution event (`investments` itself only ever holds the
    *current* state) -- dropping it while it holds rows would silently
    destroy exactly the auditable history the Work Order requires to be
    permanent ("Manter histórico de avaliações/aportes",
    "não apagar histórico para representar estado atual"). `investments`
    is guarded too: even a single active asset with no valuation history
    yet (a household that just created one and has not recorded any event)
    still represents a real financial fact (Work Order acceptance criteria
    1/6 -- "Um investimento consegue armazenar...").

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade in
    this chain (0015, 0016).
    """

    if not op.get_context().as_sql:
        valuation_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM investment_valuations")
        ).scalar()
        if valuation_count:
            raise RuntimeError(
                f"Downgrade de 0019 abortado: {valuation_count} linha(s) de "
                "investment_valuations (histórico de avaliações/aportes de patrimônio) seriam "
                "perdidas silenciosamente e não são reconstruíveis a partir de investments. "
                "Exporte `SELECT * FROM investment_valuations` primeiro, preserve o resultado "
                "fora do banco, e só então repita o downgrade."
            )
        investment_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM investments")
        ).scalar()
        if investment_count:
            raise RuntimeError(
                f"Downgrade de 0019 abortado: {investment_count} linha(s) de investments "
                "(patrimônio/ativos cadastrados, incluindo eventual Studio) seriam perdidas "
                "silenciosamente. Exporte `SELECT * FROM investments` primeiro, preserve o "
                "resultado fora do banco, e só então repita o downgrade."
            )

    op.drop_index("ix_investment_valuations_investment_date", table_name="investment_valuations")
    op.drop_index("ix_investment_valuations_trace_id", table_name="investment_valuations")
    op.drop_index("ix_investment_valuations_investment_id", table_name="investment_valuations")
    op.drop_index("ix_investment_valuations_household_id", table_name="investment_valuations")
    op.drop_table("investment_valuations")
    op.drop_index("ix_investments_household_id", table_name="investments")
    op.drop_table("investments")
