"""WA-07: whatsapp_authorized_numbers.pending_capture_id.

Revision ID: 0027
Revises: 0026

Issue #79, `docs/WORK_ORDER_WA_07.md`: purely additive column -- one
nullable, `SET NULL`-on-delete foreign key from an authorized WhatsApp
number to the single `CaptureDraft` its next confirm/cancel text reply
resolves (see `app.models.WhatsAppAuthorizedNumber.pending_capture_id`'s own
docstring for the full contract). No existing column's type, nullability or
semantics change; no backfill needed (every existing row simply starts with
`pending_capture_id = NULL`, i.e. "nothing pending" -- exactly the state a
number that has never received WA-07 media already has).

Nothing here touches `transactions`, `obligations`, `capture_drafts` rows
themselves, `audit_events`, or any other existing table's data -- this
migration only adds one column and its index/FK to
`whatsapp_authorized_numbers`.
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_authorized_numbers"
_COLUMN = "pending_capture_id"
_FK_NAME = "fk_whatsapp_authorized_numbers_pending_capture_id"
_INDEX_NAME = "ix_whatsapp_authorized_numbers_pending_capture_id"


def _existing_columns() -> set[str]:
    # Offline SQL generation has no live connection to inspect -- same
    # established pattern as every prior migration in this chain
    # (e.g. 0026's `_live_tables`).
    if op.get_context().as_sql:
        return set()
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    # `batch_alter_table` (same pattern as 0025's `whatsapp_inbound_events`
    # change): SQLite cannot `ALTER TABLE ... ADD CONSTRAINT` a foreign key
    # onto an existing table outside of its copy-and-move batch mode, while
    # PostgreSQL (production) applies the exact same operations directly --
    # this is portable across both without a dialect branch.
    if _COLUMN in _existing_columns():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            _FK_NAME,
            "capture_drafts",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(_INDEX_NAME, [_COLUMN])


def downgrade() -> None:
    """Safe to drop unconditionally: `pending_capture_id` is a pure UX
    pointer for one channel's own confirm/cancel gate, never a financial
    fact and never the sole record of anything -- the `CaptureDraft` row it
    points at (and that row's own full audit trail) is untouched by this
    column existing or not. Losing it only means an in-flight WhatsApp media
    confirmation that has not yet been replied to would need to be resent;
    no data is lost, and no transaction/obligation/payroll record is
    affected either way.
    """

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_INDEX_NAME)
        batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        batch_op.drop_column(_COLUMN)
