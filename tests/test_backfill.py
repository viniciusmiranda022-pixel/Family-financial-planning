"""Regression coverage for `app.cli.backfill` (PR 8 Work Order scope items 5-6).

Covers: idempotency (running twice reproduces the same derived state),
non-mutation of source financial facts, the untrusted/`unknown` legacy
balance path, dry-run leaving nothing persisted except its own manifest,
and multi-household scope.
"""

import os
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "backfill-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.cli.backfill import process_household, run_backfill  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Commission,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    DuplicateGroupMember,
    FinancialSnapshot,
    Household,
    IntegrityFinding,
    IntegrityRun,
    Obligation,
    PayrollRecord,
    Transaction,
)
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _source_fact_fingerprint(db: Session, household_id: str) -> dict[str, object]:
    """A snapshot of every column `app.cli.backfill` must never change.

    Includes the duplicate-classification columns (`canonical_status`,
    `possible_duplicate`, `excluded`, `duplicate_group_id`): a 2026-09-03
    engineering review found that the backfill *did* mutate them through
    `register_transaction_duplicates` while this fingerprint omitted them,
    so the byte-for-byte assertions below did not actually detect the
    mutation. See docs/WORK_ORDER_PR8_...: the backfill "não pode modificar
    silenciosamente Transaction... ou fatos financeiros de origem".
    """

    transactions = {
        row.id: (
            row.booked_at,
            row.description,
            str(row.amount),
            row.transaction_type,
            row.account_id,
            row.category_id,
            row.canonical_status,
            row.possible_duplicate,
            row.excluded,
            row.duplicate_group_id,
        )
        for row in db.scalars(select(Transaction).where(Transaction.household_id == household_id)).all()
    }
    documents = {
        row.id: (row.original_name, row.document_type, row.sha256, row.encrypted_path)
        for row in db.scalars(select(Document).where(Document.household_id == household_id)).all()
    }
    payroll = {
        row.id: (row.person_name, str(row.net_amount), row.competence)
        for row in db.scalars(select(PayrollRecord).where(PayrollRecord.household_id == household_id)).all()
    }
    commissions = {
        row.id: (str(row.gross_amount), row.status, row.expected_date)
        for row in db.scalars(select(Commission).where(Commission.household_id == household_id)).all()
    }
    obligations = {
        row.id: (row.name, str(row.amount), row.due_date)
        for row in db.scalars(select(Obligation).where(Obligation.household_id == household_id)).all()
    }
    return {
        "transactions": transactions,
        "documents": documents,
        "payroll": payroll,
        "commissions": commissions,
        "obligations": obligations,
    }


def test_backfill_never_mutates_source_financial_facts() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        before = _source_fact_fingerprint(db, synthetic.household.id)

        process_household(db, synthetic.household, period_from=None, period_to=None)
        db.commit()

        after = _source_fact_fingerprint(db, synthetic.household.id)
        assert before == after


def test_backfill_reports_the_duplicate_pair_as_derived_evidence_without_mutating_it() -> None:
    """The backfill must surface the legacy duplicate pair as review-ready
    evidence (`DuplicateGroup`/`DuplicateGroupMember`, discoverable through
    `GET /duplicate-groups`) while leaving the `Transaction` rows themselves
    exactly as they were -- see the 2026-09-03 review of this exact
    behavior in the PR history and docs/FINANCIAL_RULES.md's "Backfill"
    section.
    """

    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()

        first, second = synthetic.duplicate_pair
        first_before = (
            first.canonical_status,
            first.possible_duplicate,
            first.excluded,
            first.duplicate_group_id,
        )
        second_before = (
            second.canonical_status,
            second.possible_duplicate,
            second.excluded,
            second.duplicate_group_id,
        )
        assert first_before == ("unassigned", False, False, None)
        assert second_before == ("unassigned", False, False, None)

        report = process_household(db, synthetic.household, period_from=None, period_to=None)
        db.commit()

        assert report.documents_reconciled_unknown == 3
        assert report.duplicate_candidates_examined == len(synthetic.transactions)

        db.refresh(first)
        db.refresh(second)
        # The source `Transaction` rows are byte-for-byte unchanged: the
        # backfill must never apply a duplicate classification automatically.
        assert (
            first.canonical_status,
            first.possible_duplicate,
            first.excluded,
            first.duplicate_group_id,
        ) == first_before
        assert (
            second.canonical_status,
            second.possible_duplicate,
            second.excluded,
            second.duplicate_group_id,
        ) == second_before

        # The pair is nonetheless surfaced as derived evidence, in the same
        # group, ready for human review via `POST /duplicate-groups/{id}/resolve`.
        members = db.scalars(
            select(DuplicateGroupMember).where(
                DuplicateGroupMember.transaction_id.in_((first.id, second.id))
            )
        ).all()
        assert len(members) == 2
        group_ids = {member.group_id for member in members}
        assert len(group_ids) == 1
        group = db.get(DuplicateGroup, group_ids.pop())
        assert group is not None
        assert group.status == "open"

        reconciliations = db.scalars(
            select(DocumentReconciliation).where(
                DocumentReconciliation.household_id == synthetic.household.id
            )
        ).all()
        assert len(reconciliations) == 3
        assert all(item.status == "unknown" for item in reconciliations)

        snapshot = db.scalar(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == synthetic.household.id,
                FinancialSnapshot.period == synthetic.period,
            )
        )
        assert snapshot is not None
        assert snapshot.payload["balance_evidence_trusted"] is False


def test_backfill_is_idempotent_across_two_runs() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        household_id = synthetic.household.id

        process_household(db, synthetic.household, period_from=None, period_to=None)
        db.commit()

        def _counts() -> dict[str, int]:
            return {
                "snapshots": int(
                    db.scalar(
                        select(func.count(FinancialSnapshot.id)).where(
                            FinancialSnapshot.household_id == household_id
                        )
                    )
                ),
                "reconciliations": int(
                    db.scalar(
                        select(func.count(DocumentReconciliation.id)).where(
                            DocumentReconciliation.household_id == household_id
                        )
                    )
                ),
                "duplicate_groups": int(
                    db.scalar(
                        select(func.count(DuplicateGroup.id)).where(
                            DuplicateGroup.household_id == household_id
                        )
                    )
                ),
                "open_findings": int(
                    db.scalar(
                        select(func.count(IntegrityFinding.id)).where(
                            IntegrityFinding.household_id == household_id,
                            IntegrityFinding.status == "open",
                        )
                    )
                ),
            }

        after_first = _counts()
        before = _source_fact_fingerprint(db, household_id)

        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()

        after_second = _counts()
        after = _source_fact_fingerprint(db, household_id)

        # Derived, deduplicated facts must be byte-identical across reruns.
        assert after_first == after_second
        # Source facts are still untouched.
        assert before == after

        # A third run must land on the exact same steady state too -- not
        # just "run 1 happens to equal run 2" (see the ordering rationale in
        # `_backfill_snapshots_and_period_integrity`'s docstring: a snapshot
        # built before its own period's integrity findings settle would
        # only converge one run later, which run 1 vs run 2 alone would not
        # catch if their roles were swapped).
        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()
        assert _counts() == after_second
        assert _source_fact_fingerprint(db, household_id) == after

        # The audit trail itself is allowed -- expected -- to keep growing.
        run_count = int(
            db.scalar(select(func.count(IntegrityRun.id)).where(IntegrityRun.household_id == household_id))
        )
        assert run_count >= 3


def test_backfill_is_idempotent_over_a_multi_period_range() -> None:
    """Same as `test_backfill_is_idempotent_across_two_runs`, but forcing a
    range that includes empty periods before/after the one with real
    transactions -- exercises `build_snapshot`'s opening-balance
    carry-forward between consecutive periods across reruns.
    """

    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db, period="2026-06")
        db.commit()
        household_id = synthetic.household.id

        def _snapshot_count() -> int:
            return int(
                db.scalar(
                    select(func.count(FinancialSnapshot.id)).where(
                        FinancialSnapshot.household_id == household_id
                    )
                )
            )

        household = db.get(Household, household_id)
        process_household(db, household, period_from="2026-04", period_to="2026-08")
        db.commit()
        first_count = _snapshot_count()
        assert first_count == 5

        household = db.get(Household, household_id)
        process_household(db, household, period_from="2026-04", period_to="2026-08")
        db.commit()
        assert _snapshot_count() == first_count


def test_dry_run_persists_nothing() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        db.commit()
        household_id = synthetic.household.id
        before = _source_fact_fingerprint(db, household_id)

        summary = run_backfill(
            db,
            household_names=[synthetic.household.name],
            period_from=None,
            period_to=None,
            dry_run=True,
        )
        assert summary["households_processed"] == 1
        assert summary["households"][0]["documents_reconciled_unknown"] == 3
        # The in-session report shows what *would* change...
        assert summary["state_after"]["financial_snapshots"] >= 1
        db.rollback()

        # ...but nothing survives the rollback: same source facts, and zero
        # derived rows persisted.
        after = _source_fact_fingerprint(db, household_id)
        assert before == after
        assert (
            int(
                db.scalar(
                    select(func.count(FinancialSnapshot.id)).where(
                        FinancialSnapshot.household_id == household_id
                    )
                )
            )
            == 0
        )
        assert (
            int(
                db.scalar(
                    select(func.count(DocumentReconciliation.id)).where(
                        DocumentReconciliation.household_id == household_id
                    )
                )
            )
            == 0
        )
        assert (
            int(
                db.scalar(
                    select(func.count(DuplicateGroup.id)).where(
                        DuplicateGroup.household_id == household_id
                    )
                )
            )
            == 0
        )


def test_backfill_processes_only_named_households() -> None:
    with Session(_engine()) as db:
        first = build_synthetic_household(db, name="Família Um")
        second = build_synthetic_household(db, name="Família Dois")
        db.commit()

        summary = run_backfill(
            db,
            household_names=["Família Um"],
            period_from=None,
            period_to=None,
            dry_run=False,
        )
        db.commit()

        assert summary["households_processed"] == 1
        assert summary["households"][0]["household_name"] == "Família Um"
        assert (
            int(
                db.scalar(
                    select(func.count(FinancialSnapshot.id)).where(
                        FinancialSnapshot.household_id == first.household.id
                    )
                )
            )
            == 1
        )
        assert (
            int(
                db.scalar(
                    select(func.count(FinancialSnapshot.id)).where(
                        FinancialSnapshot.household_id == second.household.id
                    )
                )
            )
            == 0
        )


def test_period_range_defaults_to_transaction_span() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db, period="2026-03")
        db.commit()

        report = process_household(db, synthetic.household, period_from=None, period_to=None)
        assert report.periods == ["2026-03"]


def test_explicit_period_range_is_respected() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db, period="2026-03")
        db.commit()

        report = process_household(
            db, synthetic.household, period_from="2026-01", period_to="2026-04"
        )
        assert report.periods == ["2026-01", "2026-02", "2026-03", "2026-04"]


def test_household_with_no_transactions_is_a_safe_no_op() -> None:
    with Session(_engine()) as db:
        household = Household(name="Família Vazia")
        db.add(household)
        db.flush()
        checking = Account(household_id=household.id, name="Conta", account_type="checking")
        db.add(checking)
        db.commit()

        report = process_household(db, household, period_from=None, period_to=None)
        assert report.periods == []
        assert report.snapshots_touched == 0
        assert report.documents_reconciled_unknown == 0
        assert report.duplicate_candidates_examined == 0


def test_legacy_investment_balance_without_observation_stays_unknown_after_backfill() -> None:
    """The exact case the Work Order names: a legacy balance with no
    reliable effective date must stay `unknown`/untrusted -- the backfill
    must never infer a date or fabricate an `AccountBalanceObservation`.
    """

    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db, with_balance_observation=False)
        db.commit()

        process_household(db, synthetic.household, period_from=None, period_to=None)
        db.commit()

        snapshot = db.scalar(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == synthetic.household.id,
                FinancialSnapshot.period == synthetic.period,
            )
        )
        assert snapshot is not None
        assert snapshot.payload["balance_evidence_trusted"] is False
        assert snapshot.trusted_for_reports is False
        assert snapshot.trusted_for_projection is False
        assert Decimal(str(snapshot.opening_liquidity_balance)) == synthetic.profile.investment_balance
