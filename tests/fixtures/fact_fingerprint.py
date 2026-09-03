"""Generic, structurally-complete source-fact fingerprinting for regression tests.

`app.cli.backfill` must never mutate a source financial fact
(`Transaction`, `Document`, `PayrollRecord`, `Commission`, `Obligation`) --
see docs/FINANCIAL_RULES.md's "Backfill" section. A regression test that
proves this by hand-listing a subset of each model's columns can silently
stop proving it the moment a new column is added, or -- as the 2026-09-03
engineering review of PR 8 found -- can already be missing columns the
implementation actually mutates, without anyone noticing because the
fingerprint itself was never structurally checked against the model.

`fact_fingerprint()` derives the set of columns to compare directly from
each model's SQLAlchemy mapping instead: every mapped column except the
primary key (already the dict key each row is indexed by, in every caller)
and the `created_at`/`updated_at` audit timestamps (backfill-owned
bookkeeping, never a financial fact, and `updated_at` is expected to move
whenever the ORM touches a row even without a real content change). Every
other column -- including source/lineage fields such as `document_id`,
`fingerprint`, `source_priority`, `trace_id`, `classification_source`, the
duplicate-classification columns, etc. -- is included automatically, so a
mutation anywhere on the row is detected without hand-curating a tuple that
can silently drift out of sync with the model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

# Never part of the financial fact itself: `id` is the dict key every
# caller already indexes rows by, and the timestamps are audit bookkeeping
# columns the ORM/database own, not source data `app.cli.backfill` could
# meaningfully "mutate".
_EXCLUDED_COLUMNS = frozenset({"id", "created_at", "updated_at"})


def fact_fingerprint_columns(model: type) -> tuple[str, ...]:
    """Every mapped column of `model` except `id`/`created_at`/`updated_at`,
    in a stable (sorted) order."""

    return tuple(
        sorted(
            attribute.key
            for attribute in sa_inspect(model).mapper.column_attrs
            if attribute.key not in _EXCLUDED_COLUMNS
        )
    )


def _normalize(value: Any) -> Any:
    # `Decimal("1.00") == Decimal("1.0")` is already true, but comparing via
    # `str` keeps a mismatch's pytest diff readable (matches the scale the
    # column was written with) and avoids relying on `Decimal.__eq__`'s
    # cross-context behavior for the equality assertions callers make.
    if isinstance(value, Decimal):
        return str(value)
    return value


def fact_fingerprint(db: Session, model: type, *, household_id: str) -> dict[str, tuple[Any, ...]]:
    """A snapshot, keyed by row id, of every source-fact column of `model`
    for `household_id` -- see the module docstring for exactly which
    columns that is and why."""

    from sqlalchemy import select

    columns = fact_fingerprint_columns(model)
    rows = db.scalars(select(model).where(model.household_id == household_id)).all()
    return {
        row.id: tuple(_normalize(getattr(row, column)) for column in columns) for row in rows
    }
