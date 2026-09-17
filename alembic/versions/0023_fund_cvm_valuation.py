"""CVM daily fund valuation: fund_reference_quotes, fund_unit_positions,
fund_valuations tables.

Revision ID: 0023
Revises: 0022

Issue #85, `docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`: purely additive schema for
deterministic, auditable daily valuation of the Itaú Privilège DI fund (and
any future fund) from official CVM quotas, kept strictly distinct from the
existing confirmed-balance mechanism (`account_balance_observations`) and
from `investments`/`investment_valuations` -- see the three new models'
docstrings in `app/models.py` for why this is a separate, additive,
display/reconciliation-only slice rather than a change to either existing
table.

`fund_reference_quotes` is global (not household-scoped): the CVM quota for
a fund/date is the same fact for every household holding that fund.
`fund_unit_positions` and `fund_valuations` are household-scoped, append-only
evidence/history tables mirroring `account_balance_observations`' and
`investment_valuations`' "a correction appends a new row, never rewrites"
contract.

There is no legacy CVM/fund-quota data or model anywhere in this codebase
to migrate (confirmed by inventory -- `docs/PRIVILEGE_DI_CVM_INGESTION_INVESTIGATION.md`
and grep for CVM/CNPJ/quota terms found nothing prior to this PR). This
migration therefore has nothing to backfill.

Nothing here touches `transactions`, `accounts`, `account_balance_observations`,
`investments`, `financial_profiles`, or any other existing table -- this
migration only adds three new tables.
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _live_tables() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain (0006,
    # 0009, 0010, 0011, 0013, 0014, 0015, 0016, 0017, 0018, 0019).
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    live_tables = _live_tables()

    if "fund_reference_quotes" not in live_tables:
        op.create_table(
            "fund_reference_quotes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("fund_cnpj", sa.String(20), nullable=False),
            sa.Column("quota_reference_date", sa.Date(), nullable=False),
            sa.Column("quota_value", sa.Numeric(20, 10), nullable=False),
            sa.Column("net_worth", sa.Numeric(18, 2), nullable=True),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("source_url", sa.String(500), nullable=False),
            sa.Column("ingestion_batch", sa.String(20), nullable=False),
            sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                "fund_cnpj", "quota_reference_date", name="uq_fund_reference_quotes_cnpj_date"
            ),
        )
        op.create_index("ix_fund_reference_quotes_fund_cnpj", "fund_reference_quotes", ["fund_cnpj"])

    if "fund_unit_positions" not in live_tables:
        op.create_table(
            "fund_unit_positions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("fund_cnpj", sa.String(20), nullable=False),
            sa.Column("units_held", sa.Numeric(24, 8), nullable=False),
            sa.Column("effective_date", sa.Date(), nullable=False),
            sa.Column("evidence_type", sa.String(40), nullable=False),
            sa.Column("delta_units", sa.Numeric(24, 8), nullable=True),
            sa.Column(
                "evidence_document_id",
                sa.String(36),
                sa.ForeignKey("documents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "evidence_balance_observation_id",
                sa.String(36),
                sa.ForeignKey("account_balance_observations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "recorded_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
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
        op.create_index("ix_fund_unit_positions_household_id", "fund_unit_positions", ["household_id"])
        op.create_index("ix_fund_unit_positions_fund_cnpj", "fund_unit_positions", ["fund_cnpj"])
        op.create_index("ix_fund_unit_positions_trace_id", "fund_unit_positions", ["trace_id"])
        op.create_index(
            "ix_fund_unit_positions_household_fund_date",
            "fund_unit_positions",
            ["household_id", "fund_cnpj", "effective_date"],
        )

    if "fund_valuations" not in live_tables:
        op.create_table(
            "fund_valuations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "household_id",
                sa.String(36),
                sa.ForeignKey("households.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("fund_cnpj", sa.String(20), nullable=False),
            sa.Column(
                "account_id",
                sa.String(36),
                sa.ForeignKey("accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "quote_id",
                sa.String(36),
                sa.ForeignKey("fund_reference_quotes.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "position_id",
                sa.String(36),
                sa.ForeignKey("fund_unit_positions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("quota_reference_date", sa.Date(), nullable=False),
            sa.Column("quota_value", sa.Numeric(20, 10), nullable=False),
            sa.Column("units_held", sa.Numeric(24, 8), nullable=False),
            sa.Column("gross_value", sa.Numeric(14, 2), nullable=False),
            sa.Column("previous_gross_value", sa.Numeric(14, 2), nullable=True),
            sa.Column("variation_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("variation_pct", sa.Numeric(9, 6), nullable=True),
            sa.Column("observed_balance", sa.Numeric(14, 2), nullable=True),
            sa.Column("observed_balance_as_of", sa.Date(), nullable=True),
            sa.Column("reconciliation_diff", sa.Numeric(14, 2), nullable=True),
            sa.Column("provider", sa.String(40), nullable=False),
            sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valuation_version", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error_code", sa.String(60), nullable=True),
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
            sa.UniqueConstraint(
                "household_id",
                "fund_cnpj",
                "quota_reference_date",
                name="uq_fund_valuations_household_fund_date",
            ),
        )
        op.create_index("ix_fund_valuations_household_id", "fund_valuations", ["household_id"])
        op.create_index("ix_fund_valuations_fund_cnpj", "fund_valuations", ["fund_cnpj"])
        op.create_index("ix_fund_valuations_trace_id", "fund_valuations", ["trace_id"])
        op.create_index(
            "ix_fund_valuations_household_fund_date",
            "fund_valuations",
            ["household_id", "fund_cnpj", "quota_reference_date"],
        )


def downgrade() -> None:
    """Guarded exactly like migration 0019's downgrade: `fund_valuations` and
    `fund_unit_positions` are durable evidence/history (a valuation is never
    recomputable from nothing -- it requires the exact quote and position
    that produced it; a position is the only record of *why* a household is
    believed to hold N units). `fund_reference_quotes` is guarded too even
    though it is re-fetchable in principle from CVM -- CVM's open-data
    portal only guarantees the last twelve months of files stay published,
    so an old quote row already ingested might not be re-fetchable by the
    time someone runs this downgrade.

    Offline SQL generation (`alembic downgrade --sql`) has no live
    connection to check against -- matches every other guarded downgrade in
    this chain (0015, 0016, 0019).
    """

    if not op.get_context().as_sql:
        valuation_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM fund_valuations")
        ).scalar()
        if valuation_count:
            raise RuntimeError(
                f"Downgrade de 0023 abortado: {valuation_count} linha(s) de fund_valuations "
                "(histórico de valorização diária do Privilège DI/CVM) seriam perdidas "
                "silenciosamente e não são reconstruíveis a partir de fund_reference_quotes/"
                "fund_unit_positions isoladamente. Exporte `SELECT * FROM fund_valuations` "
                "primeiro, preserve o resultado fora do banco, e só então repita o downgrade."
            )
        position_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM fund_unit_positions")
        ).scalar()
        if position_count:
            raise RuntimeError(
                f"Downgrade de 0023 abortado: {position_count} linha(s) de fund_unit_positions "
                "(evidência de quantidade de cotas/units_held) seriam perdidas silenciosamente "
                "e não são reconstruíveis. Exporte `SELECT * FROM fund_unit_positions` primeiro, "
                "preserve o resultado fora do banco, e só então repita o downgrade."
            )
        quote_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM fund_reference_quotes")
        ).scalar()
        if quote_count:
            raise RuntimeError(
                f"Downgrade de 0023 abortado: {quote_count} linha(s) de fund_reference_quotes "
                "(cotas oficiais CVM já ingeridas) seriam perdidas silenciosamente e podem não "
                "estar mais disponíveis para reingestão (CVM só garante os últimos doze meses "
                "de arquivos publicados). Exporte `SELECT * FROM fund_reference_quotes` primeiro, "
                "preserve o resultado fora do banco, e só então repita o downgrade."
            )

    op.drop_index("ix_fund_valuations_household_fund_date", table_name="fund_valuations")
    op.drop_index("ix_fund_valuations_trace_id", table_name="fund_valuations")
    op.drop_index("ix_fund_valuations_fund_cnpj", table_name="fund_valuations")
    op.drop_index("ix_fund_valuations_household_id", table_name="fund_valuations")
    op.drop_table("fund_valuations")

    op.drop_index("ix_fund_unit_positions_household_fund_date", table_name="fund_unit_positions")
    op.drop_index("ix_fund_unit_positions_trace_id", table_name="fund_unit_positions")
    op.drop_index("ix_fund_unit_positions_fund_cnpj", table_name="fund_unit_positions")
    op.drop_index("ix_fund_unit_positions_household_id", table_name="fund_unit_positions")
    op.drop_table("fund_unit_positions")

    op.drop_index("ix_fund_reference_quotes_fund_cnpj", table_name="fund_reference_quotes")
    op.drop_table("fund_reference_quotes")
