"""Regression coverage for the shared synthetic fixture itself.

Guards the fixture's own shape (WO scope item 3: "datasets fictícios de
regressão... suficientes para cobrir regras financeiras críticas,
reconciliação, duplicidades, snapshots, projeção, publicação e monthly
close") so a future edit to `build_synthetic_household` cannot silently
stop covering the scenarios other tests rely on it for.
"""

import os
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Settings are process-wide and read once (`get_settings` is `lru_cache`d); a
# module run standalone (not after `tests/test_api.py`, which sets these as a
# side effect of import order) still needs them to import `app.db`.
os.environ.setdefault("SECRET_KEY", "synthetic-fixture-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("MFA_ENCRYPTION_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")

from app.db import Base  # noqa: E402
from app.services.financial_snapshots import build_snapshot  # noqa: E402
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_fixture_seeds_every_documented_scenario_shape() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)

        assert synthetic.household.id
        assert {"checking", "credit_card", "investment"} == {
            synthetic.checking.account_type,
            synthetic.credit_card.account_type,
            synthetic.investment.account_type,
        }
        assert len(synthetic.transactions) == 10
        # Internal transfer nets to zero (INV-001 evidence available).
        transfer_sum = synthetic.transactions["internal_transfer_out"].amount + (
            synthetic.transactions["internal_transfer_in"].amount
        )
        assert transfer_sum == Decimal("0.00")
        # Card purchase and its statement-side payment share the same
        # amount (INV-002 reconciliation evidence available): the purchase
        # debits the card account and the payment debits the checking
        # account for the same value.
        assert synthetic.transactions["card_purchase"].amount == synthetic.transactions[
            "card_payment"
        ].amount
        # Duplicate pair left unclassified for the pipeline/backfill to process.
        first, second = synthetic.duplicate_pair
        assert first.amount == second.amount
        assert first.account_id != second.account_id
        assert first.canonical_status == "unassigned"
        assert second.canonical_status == "unassigned"
        assert first.duplicate_group_id is None
        assert second.duplicate_group_id is None
        # Two documents (statement, card) have no reconciliation yet -- the
        # "imported, not reconciled" state app.cli.backfill must close out.
        assert len(synthetic.documents) == 3
        assert synthetic.commission.status == "expected"
        assert synthetic.payroll_record.net_amount == Decimal("6000.00")
        assert synthetic.obligation.active is True
        # No balance evidence by default: opening balance must stay untrusted.
        assert synthetic.profile.investment_balance == Decimal("10000.00")


def test_fixture_produces_a_buildable_snapshot() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db)
        snapshot = build_snapshot(db, household_id=synthetic.household.id, period=synthetic.period)

        assert snapshot.period == synthetic.period
        # Untrusted opening balance (no AccountBalanceObservation seeded):
        # publication trust gates must reflect that, not silently pass.
        assert snapshot.payload["balance_evidence_trusted"] is False
        assert snapshot.trusted_for_reports is False
        assert snapshot.operating_income == Decimal("6000.00")
        # Internal transfer and patrimonial movement never touch operating
        # income/expenses (INV-001/INV-003/INV-004). `internal_transfers`
        # sums the absolute amount of both legs of the pair (500 out + 500
        # in), not their net (which is zero by construction).
        assert snapshot.internal_transfers == Decimal("1000.00")
        assert snapshot.investments == Decimal("1000.00")
        assert snapshot.redemptions == Decimal("300.00")


def test_fixture_with_balance_observation_is_trusted() -> None:
    with Session(_engine()) as db:
        synthetic = build_synthetic_household(db, with_balance_observation=True)
        snapshot = build_snapshot(db, household_id=synthetic.household.id, period=synthetic.period)

        assert snapshot.payload["balance_evidence_trusted"] is True
