"""Add local MFA TOTP: users.session_version, mfa_factors, mfa_recovery_codes.

Revision ID: 0014
Revises: 0013

Fase 4 item 3 (`docs/WORK_ORDER_LOCAL_MFA_TOTP.md`): purely additive schema
for mandatory local TOTP.

- `users.session_version` is a new `NOT NULL` column with `server_default
  '1'` -- every existing row (including one written before this migration
  ran, on a live database) gets `1` with no separate backfill statement,
  and no existing column is widened, renamed or reinterpreted.
- `mfa_factors` and `mfa_recovery_codes` are brand-new tables. Nothing
  backfills a row into either for existing users: an existing user simply
  has no `mfa_factors` row until they complete enrollment, which is
  exactly the "usuário existente sem MFA entra em enrollment obrigatório"
  requirement -- the *application* layer decides that from the row's
  absence, not this migration.
- No transaction/document/snapshot/financial table is touched.

Downgrade drops both new tables and the new column. `mfa_recovery_codes`
is dropped first (its FK depends on `mfa_factors`). This is safe for a
real deployment only under the same rule every other migration in this
project follows: downgrading a schema that already has rows discards
those rows -- here, TOTP factors and recovery codes, never a financial
fact -- so it must not run against a production database without first
following the rollback documented in `docs/WORK_ORDER_LOCAL_MFA_TOTP.md`
("Rollback").
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_FACTORS_TABLE = "mfa_factors"
_RECOVERY_TABLE = "mfa_recovery_codes"


def _live_tables() -> set[str]:
    # Offline SQL generation (`alembic upgrade head --sql`, exercised by
    # tests/test_migrations.py::test_migrations_render_valid_postgresql_ddl_offline)
    # has no live connection to inspect -- matches the established pattern
    # in 0006/0009/0010/0011/0013 (`op.get_context().as_sql`): render every
    # operation unconditionally instead of raising or silently skipping it.
    if op.get_context().as_sql:
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def _live_columns(table_name: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "session_version" not in _live_columns("users"):
        op.add_column(
            "users",
            sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
        )

    live_tables = _live_tables()

    if _FACTORS_TABLE not in live_tables:
        op.create_table(
            _FACTORS_TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("secret_encrypted", sa.Text(), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("pending_secret_encrypted", sa.Text(), nullable=True),
            sa.Column("pending_setup_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("pending_setup_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_accepted_timestep", sa.Integer(), nullable=True),
            sa.Column("pending_last_accepted_timestep", sa.Integer(), nullable=True),
            sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", name="uq_mfa_factors_user_id"),
        )
        op.create_index("ix_mfa_factors_user_id", _FACTORS_TABLE, ["user_id"])

    if _RECOVERY_TABLE not in live_tables:
        op.create_table(
            _RECOVERY_TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("factor_id", sa.String(36), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["factor_id"], [f"{_FACTORS_TABLE}.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_mfa_recovery_codes_factor_id", _RECOVERY_TABLE, ["factor_id"])
        op.create_index("ix_mfa_recovery_codes_code_hash", _RECOVERY_TABLE, ["code_hash"])


def downgrade() -> None:
    live_tables = _live_tables()
    if _RECOVERY_TABLE in live_tables:
        op.drop_index("ix_mfa_recovery_codes_code_hash", table_name=_RECOVERY_TABLE)
        op.drop_index("ix_mfa_recovery_codes_factor_id", table_name=_RECOVERY_TABLE)
        op.drop_table(_RECOVERY_TABLE)
    if _FACTORS_TABLE in live_tables:
        op.drop_index("ix_mfa_factors_user_id", table_name=_FACTORS_TABLE)
        op.drop_table(_FACTORS_TABLE)
    if "session_version" in _live_columns("users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("session_version")
