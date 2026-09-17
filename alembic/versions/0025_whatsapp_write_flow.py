"""WA-03: whatsapp_inbound_events gains trace_id (correlate a receipt with
its Tool Layer audit trail) and a widened status column (new outcome
values: processed/needs_clarification/unsupported_content/error).

Revision ID: 0025
Revises: 0024

Issue #75, `docs/WORK_ORDER_WA_03.md`. Purely additive/widening: `trace_id`
is a new nullable column (existing rows simply have it `NULL`, exactly the
"never has message content or PII" contract migration 0024 already
established) and widening `status` from `String(20)` to `String(24)` cannot
truncate or reinterpret any existing value -- every value WA-01 ever wrote
(`accepted`/`unauthorized`/`rate_limited`) is well under both lengths.
Reversible: `downgrade()` drops the new column and narrows `status` back,
which is safe precisely because no WA-03 status value exceeds 20 characters
either (`needs_clarification`/`unsupported_content` are 19 characters each)
-- a downgrade after WA-03 has been live loses no data by truncation, only
the `trace_id` correlation itself, which is operational telemetry, not a
financial fact or an unrecoverable authorization record (same class of
"unguarded downgrade" as 0024's own `whatsapp_inbound_events` treatment).
"""

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _live_columns(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    columns = _live_columns("whatsapp_inbound_events")

    with op.batch_alter_table("whatsapp_inbound_events") as batch_op:
        if "trace_id" not in columns:
            batch_op.add_column(sa.Column("trace_id", sa.String(64), nullable=True))
        batch_op.alter_column("status", existing_type=sa.String(20), type_=sa.String(24))


def downgrade() -> None:
    with op.batch_alter_table("whatsapp_inbound_events") as batch_op:
        batch_op.alter_column("status", existing_type=sa.String(24), type_=sa.String(20))
        batch_op.drop_column("trace_id")
