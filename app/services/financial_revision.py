"""Per-household monotonic revision counter used to close the
`trust_monthly_close` TOCTOU window.

See `app.models.HouseholdFinancialRevision` for the table's docstring, the
`before_flush` event listener that bumps it, and the engineering review this
responds to (PR 7, Round 7: "The trust transition must be serialized with
every financial source mutation that can affect the snapshot ... or use a
monotonic source revision validated inside the same transactional barrier").
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import HouseholdFinancialRevision


def bump_household_financial_revision(db: Session, *, household_id: str) -> None:
    """Increment `household_id`'s revision, creating the row starting at 1
    if it does not exist yet.

    Called only from the `before_flush` event listener in `app/models.py`
    for every mutation of a `FINANCIAL_REVISION_MODELS` row -- do not call
    this directly from an endpoint; add the model to
    `FINANCIAL_REVISION_MODELS` instead, so the event listener covers every
    future mutation of it automatically.

    Uses a single atomic `INSERT ... ON CONFLICT DO UPDATE` (not a `SELECT`
    followed by an `UPDATE` or `INSERT`) so two mutations racing to bump the
    same not-yet-existing row cannot both attempt to `INSERT` and collide on
    the primary key, and so this statement itself takes PostgreSQL's
    row-level write lock on the row for the rest of the current
    transaction -- the same row `lock_household_financial_revision` below
    later takes with `SELECT ... FOR UPDATE`, so a `trust_monthly_close`
    transaction already holding that lock blocks this upsert until it
    commits or rolls back.
    """

    bind = db.get_bind()
    insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert_fn(HouseholdFinancialRevision).values(household_id=household_id, revision=1)
    statement = statement.on_conflict_do_update(
        index_elements=[HouseholdFinancialRevision.household_id],
        set_={
            "revision": HouseholdFinancialRevision.revision + 1,
            "updated_at": func.now(),
        },
    )
    db.execute(statement)


def current_household_financial_revision(db: Session, *, household_id: str) -> int:
    """Read-only current value, no lock -- a household with no prior
    mutation has no row yet, which reads as revision `0`."""

    value = db.scalar(
        select(HouseholdFinancialRevision.revision).where(
            HouseholdFinancialRevision.household_id == household_id
        )
    )
    return int(value or 0)


def lock_household_financial_revision(db: Session, *, household_id: str) -> int:
    """Return the household's current revision, taking PostgreSQL's
    row-level write lock on it (`SELECT ... FOR UPDATE`) for the rest of the
    current transaction -- see `HouseholdFinancialRevision`'s docstring for
    why this is the TOCTOU barrier `trust_monthly_close` needs.

    A no-op lock on any other dialect (SQLite has no cross-connection
    row-level lock); the caller still gets the current revision value back
    for a same-transaction comparison later, which is the deterministic
    test-equivalent path -- see `HouseholdFinancialRevision`'s docstring.

    Ensures the row exists first via the same atomic upsert
    `bump_household_financial_revision` uses (inserting a starting-at-0 row
    on conflict-do-nothing instead of incrementing), so a household with no
    prior mutation still has a row to lock.
    """

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return current_household_financial_revision(db, household_id=household_id)

    statement = pg_insert(HouseholdFinancialRevision).values(household_id=household_id, revision=0)
    statement = statement.on_conflict_do_nothing(index_elements=[HouseholdFinancialRevision.household_id])
    db.execute(statement)
    value = db.scalar(
        select(HouseholdFinancialRevision.revision)
        .where(HouseholdFinancialRevision.household_id == household_id)
        .with_for_update()
    )
    return int(value or 0)


__all__ = [
    "bump_household_financial_revision",
    "current_household_financial_revision",
    "lock_household_financial_revision",
]
