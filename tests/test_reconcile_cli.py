"""Regression coverage for `app.cli.reconcile` -- P0 #87, October Go-Live
Slice 8 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_8.md` §1,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §4).

Mirrors `tests/test_backfill.py`'s coverage shape for the same reasons:
non-mutation of source financial facts, idempotency across repeated runs,
`--dry-run` leaving nothing persisted, household isolation, and -- specific
to this command -- that a confirmed-balance divergence is reported, never
masked or synthetically corrected (rebaseline §5.1).
"""

import os
from datetime import date
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "reconcile-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("MFA_ENCRYPTION_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.cli.reconcile import reconcile_household, run_reconciliation  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountBalanceObservation,
    Commission,
    Document,
    FinancialSnapshot,
    Household,
    Obligation,
    PayrollRecord,
    Transaction,
)
from tests.fixtures.fact_fingerprint import fact_fingerprint  # noqa: E402
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _source_fact_fingerprint(db: Session, household_id: str) -> dict[str, object]:
    return {
        "transactions": fact_fingerprint(db, Transaction, household_id=household_id),
        "documents": fact_fingerprint(db, Document, household_id=household_id),
        "payroll": fact_fingerprint(db, PayrollRecord, household_id=household_id),
        "commissions": fact_fingerprint(db, Commission, household_id=household_id),
        "obligations": fact_fingerprint(db, Obligation, household_id=household_id),
    }


def test_reconcile_never_mutates_source_financial_facts() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        before = _source_fact_fingerprint(db, synthetic.household.id)

        reconcile_household(
            db, synthetic.household, period_from=None, period_to=synthetic.period, as_of=date(2026, 9, 15)
        )
        db.commit()

        after = _source_fact_fingerprint(db, synthetic.household.id)
        assert before == after


def test_reconcile_is_idempotent_across_two_runs() -> None:
    """Re-running the report for the same household/period twice must
    reproduce the exact same reconciliation figures and must not grow the
    number of `FinancialSnapshot` rows a second time (the underlying
    `build_snapshot` is already checksum-idempotent; this proves the
    reporting command does not defeat that by forcing a rebuild)."""

    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()

        first = reconcile_household(
            db, synthetic.household, period_from=None, period_to=synthetic.period, as_of=date(2026, 9, 15)
        )
        db.commit()
        snapshot_count_after_first = db.scalar(
            select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == synthetic.household.id)
        )
        snapshot_ids_after_first = set(
            db.scalars(
                select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == synthetic.household.id)
            ).all()
        )

        second = reconcile_household(
            db, synthetic.household, period_from=None, period_to=synthetic.period, as_of=date(2026, 9, 15)
        )
        db.commit()
        snapshot_ids_after_second = set(
            db.scalars(
                select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == synthetic.household.id)
            ).all()
        )

        assert snapshot_ids_after_first == snapshot_ids_after_second
        assert first.to_dict()["periods"] == second.to_dict()["periods"]
        assert first.to_dict()["invoices"] == second.to_dict()["invoices"]
        assert snapshot_count_after_first is not None


def test_reconcile_dry_run_persists_nothing() -> None:
    """Same contract as `app.cli.backfill --dry-run`: the report is
    produced, but the session is rolled back afterwards instead of
    committed, so no `FinancialSnapshot`/`CardInvoice` row survives."""

    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()

        snapshots_before = db.scalar(
            select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == synthetic.household.id)
        )
        assert snapshots_before is None

        summary = run_reconciliation(
            db,
            household_names=[synthetic.household.name],
            period_from=None,
            period_to=synthetic.period,
            as_of=date(2026, 9, 15),
        )
        assert summary["households_processed"] == 1
        db.rollback()

        snapshots_after = db.scalar(
            select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == synthetic.household.id)
        )
        assert snapshots_after is None


def test_reconcile_household_isolation() -> None:
    with Session(_engine()) as db:
        household_a = build_synthetic_household(db, name="Família Reconciliação A", period="2026-06")
        household_b = build_synthetic_household(db, name="Família Reconciliação B", period="2026-06")
        db.commit()

        summary = run_reconciliation(
            db,
            household_names=[household_a.household.name],
            period_from=None,
            period_to="2026-06",
            as_of=date(2026, 9, 15),
        )
        db.commit()

        assert summary["households_processed"] == 1
        assert summary["households"][0]["household_id"] == household_a.household.id

        # Household B was never targeted -- it has no snapshot at all.
        snapshot_b = db.scalar(
            select(FinancialSnapshot.id).where(FinancialSnapshot.household_id == household_b.household.id)
        )
        assert snapshot_b is None


def test_reconcile_discloses_confirmed_balance_divergence_without_masking_it() -> None:
    """rebaseline §5.1: a confirmed balance that diverges from the
    reconstruction is reported as `"divergente"`, the closing figure is
    anchored on the confirmed observation (never the reconstruction), and
    INV-023/INV-024 both stay PASS -- because the divergence was disclosed,
    not because it was hidden or synthetically corrected."""

    with Session(_engine()) as db:
        household = Household(name="Família Divergência Confirmada")
        db.add(household)
        db.flush()
        from app.models import FinancialProfile

        db.add(FinancialProfile(household_id=household.id))
        checking = Account(household_id=household.id, name="Conta Corrente", account_type="checking")
        privilege = Account(household_id=household.id, name="Privilège DI", account_type="investment")
        db.add_all([checking, privilege])
        db.flush()
        from app.models import Category

        category = Category(household_id=household.id, name="Mercado")
        db.add(category)
        db.flush()
        db.add(
            Transaction(
                household_id=household.id,
                account_id=checking.id,
                category_id=category.id,
                booked_at=date(2026, 8, 5),
                occurred_at=date(2026, 8, 5),
                competence="2026-08",
                description="Salário",
                normalized_description="SALARIO",
                amount=Decimal("4000.00"),
                transaction_type="income",
                fingerprint="a" * 64,
                source_priority=50,
                confidence=Decimal("1"),
                reviewed=True,
            )
        )
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=privilege.id,
                amount=Decimal("9650.00"),
                as_of_date=date(2026, 8, 20),
                observation_type="point_in_time",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="reconcile-divergence-test",
            )
        )
        db.commit()

        report = reconcile_household(
            db, household, period_from="2026-08", period_to="2026-08", as_of=date(2026, 9, 15)
        )
        db.commit()

        assert len(report.periods) == 1
        period = report.periods[0]
        assert period.observed_balance == 9650.0
        assert period.status == "divergente"
        # Sovereign: the closing figure is the confirmed observation, never
        # the (lower) from-scratch reconstruction.
        assert period.closing_liquidity_balance == 9650.0
        # Disclosed, not masked -- both invariants stay PASS.
        assert period.inv023_status == "pass"
        assert period.inv024_status == "pass"
        assert report.unresolved_divergences == 1
