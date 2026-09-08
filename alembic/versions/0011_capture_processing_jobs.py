"""Add capture_processing_jobs (async OCR/audio worker queue).

Revision ID: 0011
Revises: 0010

Fase 3 item 1 (`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md`): durable,
auditable job state for asynchronous OCR (scanned image/PDF) and Whisper
transcription processing of a `capture_drafts` upload -- see
`app.models.CaptureProcessingJob`'s docstring for the full design rationale
(single row per draft, atomic claim, fail-closed crash recovery).

Purely additive: a brand-new table with a `UNIQUE` foreign key into the
existing `capture_drafts` table (never the other direction), and an
optional foreign key into `documents`. No existing column is added,
dropped, widened or reinterpreted, and no existing row is read or
rewritten -- an installation already on `0010` upgrades with no data
migration. `capture_drafts.status` gains new string values (`queued`,
`processing`, `failed`) at the application layer only; the column itself
is untyped `String(30)` already (no `CHECK` constraint to widen), so this
migration touches nothing on that table.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_JSON_DOCUMENT = sa.JSON().with_variant(JSONB(), "postgresql")
_TABLE = "capture_processing_jobs"


def _live_tables() -> set[str]:
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE not in _live_tables():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("household_id", sa.String(36), nullable=False),
            sa.Column("capture_draft_id", sa.String(36), nullable=False),
            sa.Column("document_id", sa.String(36), nullable=True),
            sa.Column("job_type", sa.String(20), nullable=False),
            sa.Column("content_type", sa.String(100), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("claimed_by", sa.String(80), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(80), nullable=True),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["capture_draft_id"], ["capture_drafts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("capture_draft_id", name="uq_capture_jobs_capture_draft"),
            sa.UniqueConstraint("trace_id", name="uq_capture_jobs_trace_id"),
        )
        op.create_index("ix_capture_jobs_household_id", _TABLE, ["household_id"])
        op.create_index("ix_capture_jobs_capture_draft_id", _TABLE, ["capture_draft_id"])
        op.create_index("ix_capture_jobs_trace_id", _TABLE, ["trace_id"])
        op.create_index(
            "ix_capture_jobs_household_status", _TABLE, ["household_id", "status"]
        )
        op.create_index("ix_capture_jobs_status_created", _TABLE, ["status", "created_at"])


def downgrade() -> None:
    # Safe: capture_processing_jobs is pure processing-infrastructure state
    # (never a financial fact, never referenced by any other table's
    # foreign key), so dropping it cannot orphan anything and cannot lose
    # any Transaction/Obligation/PayrollRecord/Document/CaptureDraft data --
    # those all continue to exist exactly as they did before this
    # migration. A queued/failed job's proposal is only ever regenerated
    # from the original encrypted document, never destroyed by this.
    if _TABLE in _live_tables():
        op.drop_index("ix_capture_jobs_status_created", table_name=_TABLE)
        op.drop_index("ix_capture_jobs_household_status", table_name=_TABLE)
        op.drop_index("ix_capture_jobs_trace_id", table_name=_TABLE)
        op.drop_index("ix_capture_jobs_capture_draft_id", table_name=_TABLE)
        op.drop_index("ix_capture_jobs_household_id", table_name=_TABLE)
        op.drop_table(_TABLE)
