"""Tests for the PR 4 Canonical Financial Snapshot / Financial Engine."""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-financial-engine-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-financial-engine-data-{uuid.uuid4().hex}")
os.environ.setdefault("SECRET_KEY", "financial-engine-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountBalanceObservation,
    Category,
    FinancialProfile,
    FinancialSnapshot,
    FinancialSnapshotLineage,
    Household,
    Transaction,
)
from app.services.financial_engine import (  # noqa: E402
    EXCLUDED_ROLES,
    FINANCIAL_ENGINE_CALCULATION_VERSION,
    MovementFact,
    aggregate_movements,
    build_financial_snapshot,
    build_snapshot_invariant_checks,
    classify_movement,
    compute_liquidity,
    persist_financial_snapshot,
    serialize_financial_snapshot,
    serialize_lineage,
)
from app.services.financial_invariants import FINANCIAL_RULES_VERSION, InvariantStatus  # noqa: E402
from app.services.invariant_registry import evaluate_invariant  # noqa: E402

# ---------------------------------------------------------------------------
# classify_movement (pure)
# ---------------------------------------------------------------------------


def test_classify_movement_internal_transfer() -> None:
    role = classify_movement(
        transaction_type="transfer",
        amount=Decimal("-100.00"),
        category_name="Transferência interna",
        canonical_status="unassigned",
        excluded=True,
    )
    assert role == "internal_transfer"


@pytest.mark.parametrize(
    ("amount", "expected_role"),
    [(Decimal("-500.00"), "investment_application"), (Decimal("500.00"), "investment_redemption")],
)
def test_classify_movement_investment_direction(amount: Decimal, expected_role: str) -> None:
    role = classify_movement(
        transaction_type="transfer",
        amount=amount,
        category_name="Transferência patrimonial",
        canonical_status="unassigned",
        excluded=True,
    )
    assert role == expected_role


def test_classify_movement_card_payment_is_reconciliation() -> None:
    role = classify_movement(
        transaction_type="reconciliation",
        amount=Decimal("-1200.00"),
        category_name="Conciliação",
        canonical_status="unassigned",
        excluded=True,
    )
    assert role == "card_payment"


def test_classify_movement_refund() -> None:
    role = classify_movement(
        transaction_type="refund",
        amount=Decimal("50.00"),
        category_name="Reembolsos e estornos",
        canonical_status="unassigned",
        excluded=False,
    )
    assert role == "refund"


def test_classify_movement_income_and_expense() -> None:
    assert (
        classify_movement(
            transaction_type="income",
            amount=Decimal("3000.00"),
            category_name="Receitas",
            canonical_status="unassigned",
            excluded=False,
        )
        == "operating_income"
    )
    assert (
        classify_movement(
            transaction_type="expense",
            amount=Decimal("-80.00"),
            category_name="Mercado e itens domésticos",
            canonical_status="unassigned",
            excluded=False,
        )
        == "operating_expense"
    )


def test_classify_movement_manual_exclusion_is_not_counted() -> None:
    role = classify_movement(
        transaction_type="expense",
        amount=Decimal("-40.00"),
        category_name="Revisar",
        canonical_status="unassigned",
        excluded=True,
    )
    assert role == "excluded_manual"
    assert role in EXCLUDED_ROLES


def test_classify_movement_non_canonical_duplicate_is_excluded_regardless_of_type() -> None:
    """A `canonical_status == "supporting"` row is the PR 3 duplicate engine's
    precedence-resolved non-canonical copy. It must be excluded even though its
    own `transaction_type`/`excluded` flags look like a normal expense."""

    role = classify_movement(
        transaction_type="expense",
        amount=Decimal("-80.00"),
        category_name="Mercado e itens domésticos",
        canonical_status="supporting",
        excluded=False,
    )
    assert role == "excluded_non_canonical"
    assert role in EXCLUDED_ROLES


def test_classify_movement_unknown_transaction_type_is_unclassified() -> None:
    role = classify_movement(
        transaction_type="something_new",
        amount=Decimal("10.00"),
        category_name="Revisar",
        canonical_status="unassigned",
        excluded=False,
    )
    assert role == "unclassified"


# ---------------------------------------------------------------------------
# aggregate_movements (pure)
# ---------------------------------------------------------------------------


def _fact(
    role: str,
    amount: str,
    *,
    account_type: str = "checking",
    transaction_id: str = "tx",
) -> MovementFact:
    return MovementFact(
        transaction_id=transaction_id,
        amount=Decimal(amount),
        role=role,
        account_type=account_type,
        account_id="acc-1",
        category_id="cat-1",
        document_id=None,
        competence="2026-08",
    )


def test_aggregate_internal_transfer_never_changes_operating_result() -> None:
    totals = aggregate_movements([_fact("internal_transfer", "-500.00")])
    assert totals.operating_income == Decimal("0.00")
    assert totals.operating_expenses == Decimal("0.00")
    assert totals.operating_result == Decimal("0.00")
    assert totals.budget_usage == Decimal("0.00")
    assert totals.internal_transfers == Decimal("500.00")


def test_aggregate_card_purchase_and_payment_do_not_double_count() -> None:
    facts = [
        _fact("operating_expense", "-300.00", account_type="credit_card", transaction_id="purchase"),
        _fact("card_payment", "-300.00", account_type="checking", transaction_id="payment"),
    ]
    totals = aggregate_movements(facts)
    assert totals.card_spend == Decimal("300.00")
    assert totals.card_payments == Decimal("300.00")
    # The purchase is an economic expense; the payment is bank cash leaving,
    # but it must never also appear as a fresh operating expense (INV-002).
    assert totals.operating_expenses == Decimal("300.00")
    assert totals.bank_cash_out == Decimal("300.00")
    assert totals.bank_cash_in == Decimal("0.00")


def test_aggregate_investment_application_and_redemption_are_patrimonial() -> None:
    facts = [
        _fact("investment_application", "-1000.00"),
        _fact("investment_redemption", "400.00"),
    ]
    totals = aggregate_movements(facts)
    assert totals.investments == Decimal("1000.00")
    assert totals.redemptions == Decimal("400.00")
    assert totals.operating_income == Decimal("0.00")
    assert totals.operating_expenses == Decimal("0.00")
    assert totals.operating_result == Decimal("0.00")
    assert totals.bank_cash_in == Decimal("0.00")
    assert totals.bank_cash_out == Decimal("0.00")


def test_aggregate_refund_reduces_expense_without_creating_income() -> None:
    facts = [
        _fact("operating_expense", "-200.00", account_type="credit_card", transaction_id="purchase"),
        _fact("refund", "50.00", account_type="credit_card", transaction_id="refund"),
    ]
    totals = aggregate_movements(facts)
    assert totals.card_spend == Decimal("150.00")
    assert totals.refunds == Decimal("50.00")
    assert totals.operating_income == Decimal("0.00")
    assert totals.operating_expenses == Decimal("150.00")


def test_aggregate_excluded_and_unclassified_never_contribute() -> None:
    facts = [
        _fact("excluded_non_canonical", "-999.00"),
        _fact("excluded_manual", "-999.00"),
        _fact("unclassified", "-999.00"),
    ]
    totals = aggregate_movements(facts)
    assert totals.operating_income == Decimal("0.00")
    assert totals.operating_expenses == Decimal("0.00")
    assert totals.bank_cash_out == Decimal("0.00")
    assert totals.internal_transfers == Decimal("0.00")
    assert totals.investments == Decimal("0.00")


@given(
    amount=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000.00"), places=2, allow_nan=False)
)
@settings(max_examples=100, deadline=None)
def test_property_transfer_shaped_movements_never_change_operating_result(amount: Decimal) -> None:
    for role in ("internal_transfer", "investment_application", "investment_redemption", "card_payment"):
        signed = -amount if role in {"internal_transfer", "investment_application", "card_payment"} else amount
        totals = aggregate_movements([_fact(role, str(signed))])
        assert totals.operating_result == Decimal("0.00")
        assert totals.budget_usage == Decimal("0.00")


# ---------------------------------------------------------------------------
# compute_liquidity (pure, Privilège DI transition)
# ---------------------------------------------------------------------------


def test_compute_liquidity_unknown_when_opening_balance_unknown() -> None:
    liquidity = compute_liquidity(
        opening_balance=None,
        operating_result=Decimal("-100.00"),
        monthly_investment_rate=Decimal("0.01"),
        safety_floor=Decimal("5000.00"),
    )
    assert liquidity.closing_balance is None
    assert liquidity.liquidity_used is None
    assert liquidity.investment_yield is None
    assert liquidity.floor_breached is None


def test_compute_liquidity_matches_work_order_worked_example() -> None:
    """docs/work-orders/PR4_CANONICAL_FINANCIAL_SNAPSHOT.md's mandatory
    regression example, with the yield rate zeroed out so the operating
    result alone drives the transition."""

    liquidity = compute_liquidity(
        opening_balance=Decimal("87068.54"),
        operating_result=Decimal("-270014.03"),
        monthly_investment_rate=Decimal("0"),
        safety_floor=Decimal("0"),
    )
    assert liquidity.closing_balance == Decimal("0.00")
    assert liquidity.closing_uncovered_deficit == Decimal("182945.49")
    assert liquidity.liquidity_used == Decimal("87068.54")


def test_compute_liquidity_deficit_smaller_than_balance() -> None:
    liquidity = compute_liquidity(
        opening_balance=Decimal("1000.00"),
        operating_result=Decimal("-400.00"),
        monthly_investment_rate=Decimal("0"),
        safety_floor=Decimal("200.00"),
    )
    assert liquidity.closing_balance == Decimal("600.00")
    assert liquidity.closing_uncovered_deficit == Decimal("0.00")
    assert liquidity.distance_to_floor == Decimal("400.00")
    assert liquidity.floor_breached is False


def test_compute_liquidity_deficit_equal_to_balance() -> None:
    liquidity = compute_liquidity(
        opening_balance=Decimal("500.00"),
        operating_result=Decimal("-500.00"),
        monthly_investment_rate=Decimal("0"),
        safety_floor=Decimal("0"),
    )
    assert liquidity.closing_balance == Decimal("0.00")
    assert liquidity.closing_uncovered_deficit == Decimal("0.00")
    assert liquidity.liquidity_used == Decimal("500.00")


def test_compute_liquidity_floor_breached_but_not_blocking() -> None:
    """The floor never blocks withdrawal of real deficit coverage (INV-007):
    it only changes `distance_to_floor`/`floor_breached`."""

    liquidity = compute_liquidity(
        opening_balance=Decimal("1000.00"),
        operating_result=Decimal("-900.00"),
        monthly_investment_rate=Decimal("0"),
        safety_floor=Decimal("5000.00"),
    )
    assert liquidity.closing_balance == Decimal("100.00")
    assert liquidity.liquidity_used == Decimal("900.00")
    assert liquidity.floor_breached is True
    assert liquidity.distance_to_floor == Decimal("-4900.00")


def test_compute_liquidity_yield_only_on_positive_opening_balance() -> None:
    liquidity = compute_liquidity(
        opening_balance=Decimal("10000.00"),
        operating_result=Decimal("0.00"),
        monthly_investment_rate=Decimal("0.01"),
        safety_floor=None,
    )
    assert liquidity.investment_yield == Decimal("100.00")
    assert liquidity.closing_balance == Decimal("10100.00")


# ---------------------------------------------------------------------------
# DB-facing: build_financial_snapshot / persist_financial_snapshot
# ---------------------------------------------------------------------------


def _seeded_household(db: Session) -> tuple[Household, Account, Account]:
    household = Household(name="Família Teste")
    db.add(household)
    db.flush()
    checking = Account(
        household_id=household.id,
        name="Conta corrente",
        institution="Banco",
        account_type="checking",
        owner_label="Família",
    )
    card = Account(
        household_id=household.id,
        name="Cartão",
        institution="Banco",
        account_type="credit_card",
        owner_label="Família",
    )
    db.add_all([checking, card])
    db.flush()
    return household, checking, card


def _category(db: Session, household_id: str, name: str) -> Category:
    category = Category(household_id=household_id, name=name)
    db.add(category)
    db.flush()
    return category


def _transaction(
    db: Session,
    *,
    household_id: str,
    account_id: str,
    category_id: str | None,
    booked_at: date,
    amount: str,
    transaction_type: str,
    competence: str | None = None,
    canonical_status: str = "unassigned",
    excluded: bool = False,
    description: str = "Movimento",
    fingerprint_seed: str = "1",
) -> Transaction:
    transaction = Transaction(
        household_id=household_id,
        account_id=account_id,
        category_id=category_id,
        booked_at=booked_at,
        description=description,
        normalized_description=description.upper(),
        amount=Decimal(amount),
        transaction_type=transaction_type,
        owner_label="Família",
        fingerprint=fingerprint_seed * 64,
        competence=competence,
        canonical_status=canonical_status,
        excluded=excluded,
    )
    db.add(transaction)
    db.flush()
    return transaction


def _engine_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_build_snapshot_full_flow_separates_every_concept() -> None:
    with _engine_session() as db:
        household, checking, card = _seeded_household(db)
        income_category = _category(db, household.id, "Receitas")
        market_category = _category(db, household.id, "Mercado e itens domésticos")
        transfer_category = _category(db, household.id, "Transferência interna")
        patrimonial_category = _category(db, household.id, "Transferência patrimonial")
        reconciliation_category = _category(db, household.id, "Conciliação")
        refund_category = _category(db, household.id, "Reembolsos e estornos")

        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=income_category.id,
            booked_at=date(2026, 8, 5),
            amount="5000.00",
            transaction_type="income",
            fingerprint_seed="1",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=market_category.id,
            booked_at=date(2026, 8, 6),
            amount="-300.00",
            transaction_type="expense",
            fingerprint_seed="2",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=card.id,
            category_id=market_category.id,
            booked_at=date(2026, 7, 20),
            competence="2026-08",
            amount="-450.00",
            transaction_type="expense",
            fingerprint_seed="3",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=card.id,
            category_id=refund_category.id,
            booked_at=date(2026, 8, 10),
            competence="2026-08",
            amount="50.00",
            transaction_type="refund",
            fingerprint_seed="4",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=reconciliation_category.id,
            booked_at=date(2026, 8, 12),
            amount="-450.00",
            transaction_type="reconciliation",
            excluded=True,
            fingerprint_seed="5",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=transfer_category.id,
            booked_at=date(2026, 8, 8),
            amount="-1000.00",
            transaction_type="transfer",
            excluded=True,
            fingerprint_seed="6",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=patrimonial_category.id,
            booked_at=date(2026, 8, 15),
            amount="-2000.00",
            transaction_type="transfer",
            excluded=True,
            fingerprint_seed="7",
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=patrimonial_category.id,
            booked_at=date(2026, 8, 20),
            amount="700.00",
            transaction_type="transfer",
            excluded=True,
            fingerprint_seed="8",
        )
        # A duplicate copy the PR 3 engine has already resolved as non-canonical.
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=market_category.id,
            booked_at=date(2026, 8, 6),
            amount="-300.00",
            transaction_type="expense",
            canonical_status="supporting",
            fingerprint_seed="9",
        )
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")

        assert build.totals.operating_income == Decimal("5000.00")
        assert build.totals.card_spend == Decimal("400.00")  # 450 purchase - 50 refund
        assert build.totals.operating_expenses == Decimal("700.00")  # 400 card + 300 bank
        assert build.totals.operating_result == Decimal("4300.00")
        assert build.totals.budget_usage == build.totals.operating_expenses
        assert build.totals.card_payments == Decimal("450.00")
        assert build.totals.bank_cash_out == Decimal("750.00")  # 300 market + 450 card payment
        assert build.totals.bank_cash_in == Decimal("5000.00")
        assert build.totals.internal_transfers == Decimal("1000.00")
        assert build.totals.investments == Decimal("2000.00")
        assert build.totals.redemptions == Decimal("700.00")
        assert build.totals.refunds == Decimal("50.00")

        snapshot = persist_financial_snapshot(db, build)
        db.commit()
        assert snapshot.version == 1
        assert snapshot.status == "current"
        assert snapshot.completeness_status == "incomplete"  # no confirmed opening balance yet
        # No integrity run has evaluated this snapshot yet at this level (that
        # only happens in the `rebuild` endpoint, one layer up).
        assert snapshot.integrity_status == "unknown"
        assert snapshot.commitments is None
        assert snapshot.budget_cap is None
        assert snapshot.budget_remaining is None
        # Every fetched transaction is traceable in lineage, including the
        # excluded duplicate copy -- it stays auditable, just not counted.
        assert snapshot.source_count == 9

        lineage_rows = db.scalars(
            select(FinancialSnapshotLineage).where(FinancialSnapshotLineage.snapshot_id == snapshot.id)
        ).all()
        assert lineage_rows
        supporting_rows = [row for row in lineage_rows if row.source_role == "supporting"]
        assert len(supporting_rows) == 1
        assert supporting_rows[0].metric_key == "excluded_non_canonical"
        assert supporting_rows[0].contribution is None

        operating_expense_rows = [row for row in lineage_rows if row.metric_key == "operating_expenses"]
        # bank market expense + card expense + refund reduction = 3 rows.
        assert len(operating_expense_rows) == 3
        assert sum((row.contribution for row in operating_expense_rows), Decimal("0")) == Decimal(
            "700.00"
        )

        payload = serialize_financial_snapshot(snapshot)
        assert payload["operating_result"] == "4300.00"
        assert payload["calculation_version"] == FINANCIAL_ENGINE_CALCULATION_VERSION
        assert payload["financial_rules_version"] == FINANCIAL_RULES_VERSION
        assert serialize_lineage(lineage_rows)[0]["snapshot_id"] == snapshot.id


def test_card_purchase_is_counted_by_statement_competence_not_purchase_month() -> None:
    """A card purchase booked in July but billed to August's statement must be
    counted in August (INV-017), not in July."""

    with _engine_session() as db:
        household, _checking, card = _seeded_household(db)
        category = _category(db, household.id, "Mercado e itens domésticos")
        _transaction(
            db,
            household_id=household.id,
            account_id=card.id,
            category_id=category.id,
            booked_at=date(2026, 7, 28),
            competence="2026-08",
            amount="-120.00",
            transaction_type="expense",
        )
        db.commit()

        july_build = build_financial_snapshot(db, household_id=household.id, period="2026-07")
        august_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")

        assert july_build.totals.card_spend == Decimal("0.00")
        assert august_build.totals.card_spend == Decimal("120.00")


def test_rebuild_creates_new_version_and_supersedes_previous() -> None:
    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        category = _category(db, household.id, "Receitas")
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=category.id,
            booked_at=date(2026, 8, 1),
            amount="1000.00",
            transaction_type="income",
        )
        db.commit()

        first_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        first = persist_financial_snapshot(db, first_build)
        db.commit()

        second_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        second = persist_financial_snapshot(db, second_build)
        db.commit()

        db.refresh(first)
        assert first.status == "superseded"
        assert first.superseded_by_id == second.id
        assert second.status == "current"
        assert second.version == 2
        assert second.supersedes_id == first.id
        # Identical underlying facts must be idempotent: same checksum despite
        # different trace_id/generated_at/version.
        assert second.checksum == first.checksum

        current_rows = db.scalars(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == household.id,
                FinancialSnapshot.period == "2026-08",
                FinancialSnapshot.status == "current",
            )
        ).all()
        assert [row.id for row in current_rows] == [second.id]


def test_opening_liquidity_uses_confirmed_observation_over_legacy_profile_field() -> None:
    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        profile = FinancialProfile(
            household_id=household.id,
            investment_balance=Decimal("999999.00"),  # must never be trusted directly
            emergency_floor=Decimal("5000.00"),
            central_liquidity_account_id=checking.id,
        )
        db.add(profile)
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=checking.id,
                amount=Decimal("10000.00"),
                as_of_date=date(2026, 8, 1),
                observation_type="opening",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-obs-1",
            )
        )
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")

        assert build.opening_liquidity_balance == Decimal("10000.00")
        assert build.opening_balance_source_type == "observation"
        assert build.completeness_status == "complete"


def test_liquidity_transition_metrics_are_individually_traceable_in_lineage() -> None:
    """Every liquidity metric the Work Order calls out (`investment_yield`,
    `liquidity_used`, `closing_liquidity_balance`, `closing_uncovered_deficit`,
    `distance_to_floor`) must have its own auditable lineage row, referencing
    both the rule/version used and the opening-balance evidence -- not just
    `opening_liquidity_balance` itself."""

    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        profile = FinancialProfile(
            household_id=household.id,
            emergency_floor=Decimal("1000.00"),
            central_liquidity_account_id=checking.id,
        )
        db.add(profile)
        observation = AccountBalanceObservation(
            household_id=household.id,
            account_id=checking.id,
            amount=Decimal("5000.00"),
            as_of_date=date(2026, 8, 1),
            observation_type="opening",
            source="manual_confirmed",
            confidence=Decimal("1.0000"),
            trace_id="trace-obs-lineage",
        )
        db.add(observation)
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        snapshot = persist_financial_snapshot(db, build)
        db.commit()

        lineage_rows = db.scalars(
            select(FinancialSnapshotLineage).where(FinancialSnapshotLineage.snapshot_id == snapshot.id)
        ).all()
        liquidity_metric_keys = {
            "investment_yield",
            "liquidity_used",
            "closing_liquidity_balance",
            "closing_uncovered_deficit",
            "distance_to_floor",
        }
        by_metric = {row.metric_key: row for row in lineage_rows if row.metric_key in liquidity_metric_keys}
        assert set(by_metric) == liquidity_metric_keys
        for row in by_metric.values():
            assert row.rule_id == f"liquidity_transition:{build.financial_rules_version}"
            assert row.entity_type == "account_balance_observation"
            assert row.entity_id == observation.id
            assert row.source_role == "canonical"
        assert by_metric["closing_liquidity_balance"].contribution == build.liquidity.closing_balance


def test_budget_cap_and_remaining_come_from_financial_profile_cash_cap() -> None:
    """`budget_cap`/`budget_remaining` (INTEGRITY_IMPLEMENTATION_PLAN.md §7.2
    minimum contract fields) must be populated from the same canonical source
    the existing cash-cap report already reads (`FinancialProfile.
    monthly_cash_cap`), never a new parallel calculation -- and stay `None`,
    not a fabricated `0`, when no profile is configured at all."""

    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        expense_category = _category(db, household.id, "Mercado e itens domésticos")
        profile = FinancialProfile(household_id=household.id, monthly_cash_cap=Decimal("2000.00"))
        db.add(profile)
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=expense_category.id,
            booked_at=date(2026, 8, 6),
            amount="-300.00",
            transaction_type="expense",
        )
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        assert build.budget_cap == Decimal("2000.00")
        assert build.budget_remaining == Decimal("1700.00")
        assert build.budget_remaining == build.budget_cap - build.totals.budget_usage

        snapshot = persist_financial_snapshot(db, build)
        db.commit()
        assert snapshot.budget_cap == Decimal("2000.00")
        assert snapshot.budget_remaining == Decimal("1700.00")


def test_budget_cap_is_none_without_a_financial_profile() -> None:
    with _engine_session() as db:
        household, _checking, _card = _seeded_household(db)
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        assert build.budget_cap is None
        assert build.budget_remaining is None


def test_opening_liquidity_is_unknown_without_observation_or_prior_snapshot() -> None:
    with _engine_session() as db:
        household, _checking, _card = _seeded_household(db)
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")

        assert build.opening_liquidity_balance is None
        assert build.completeness_status == "incomplete"
        assert build.liquidity.closing_balance is None


def test_opening_liquidity_chains_from_previous_snapshot_closing_balance() -> None:
    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        profile = FinancialProfile(household_id=household.id, central_liquidity_account_id=checking.id)
        db.add(profile)
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=checking.id,
                amount=Decimal("1000.00"),
                as_of_date=date(2026, 7, 1),
                observation_type="opening",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-obs-2",
            )
        )
        db.commit()

        july_build = build_financial_snapshot(db, household_id=household.id, period="2026-07")
        persist_financial_snapshot(db, july_build)
        db.commit()

        august_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        assert august_build.opening_balance_source_type == "previous_snapshot"
        assert august_build.opening_liquidity_balance == july_build.liquidity.closing_balance


def test_opening_uncovered_deficit_is_paid_down_by_next_period_surplus_before_new_liquidity() -> None:
    """Full end-to-end regression for the Work Order's mandatory chained
    example (item 1): month N closes with an uncovered deficit; month N+1's
    surplus must pay that debt down first, and only the excess may become new
    `closing_liquidity_balance` -- through the real DB-backed two-snapshot
    chain, not just the pure `calculate_liquidity_transition` helper."""

    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        income_category = _category(db, household.id, "Receitas")
        expense_category = _category(db, household.id, "Mercado e itens domésticos")
        profile = FinancialProfile(household_id=household.id, central_liquidity_account_id=checking.id)
        db.add(profile)
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=checking.id,
                amount=Decimal("1000.00"),
                as_of_date=date(2026, 7, 1),
                observation_type="opening",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-obs-carry-1",
            )
        )
        # July: a 3000.00 expense against a 1000.00 opening balance leaves a
        # 2000.00 uncovered deficit (INV-005/006's own worked formula).
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=expense_category.id,
            booked_at=date(2026, 7, 10),
            amount="-3000.00",
            transaction_type="expense",
            fingerprint_seed="a",
        )
        db.commit()

        july_build = build_financial_snapshot(db, household_id=household.id, period="2026-07")
        assert july_build.liquidity.closing_balance == Decimal("0.00")
        assert july_build.liquidity.closing_uncovered_deficit == Decimal("2000.00")
        persist_financial_snapshot(db, july_build)
        db.commit()

        # August: a 2500.00 income surplus. It must pay the inherited 2000.00
        # debt down first; only the 500.00 excess becomes new liquidity.
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=income_category.id,
            booked_at=date(2026, 8, 5),
            amount="2500.00",
            transaction_type="income",
            fingerprint_seed="b",
        )
        db.commit()

        august_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        assert august_build.opening_balance_source_type == "previous_snapshot"
        assert august_build.opening_liquidity_balance == Decimal("0.00")
        assert august_build.opening_uncovered_deficit == Decimal("2000.00")
        assert august_build.liquidity.closing_balance == Decimal("500.00")
        assert august_build.liquidity.closing_uncovered_deficit == Decimal("0.00")

        # The persisted snapshot and its INV-005/006 checks must agree.
        august_snapshot = persist_financial_snapshot(db, august_build)
        db.commit()
        assert august_snapshot.closing_liquidity_balance == Decimal("500.00")
        assert august_snapshot.closing_uncovered_deficit == Decimal("0.00")
        for check in build_snapshot_invariant_checks(august_build):
            if check.invariant_id in {"INV-005", "INV-006"}:
                result = evaluate_invariant(check.invariant_id, check.context)
                assert result.status is InvariantStatus.PASS, (check.invariant_id, result.to_dict())


def test_opening_uncovered_deficit_smaller_surplus_leaves_remaining_debt() -> None:
    """Same chained scenario, but August's surplus (1200.00) is smaller than
    the inherited debt (2000.00): liquidity must stay at zero and the
    remaining 800.00 debt must carry forward, never a fabricated positive
    balance."""

    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        income_category = _category(db, household.id, "Receitas")
        expense_category = _category(db, household.id, "Mercado e itens domésticos")
        profile = FinancialProfile(household_id=household.id, central_liquidity_account_id=checking.id)
        db.add(profile)
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=checking.id,
                amount=Decimal("1000.00"),
                as_of_date=date(2026, 7, 1),
                observation_type="opening",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-obs-carry-2",
            )
        )
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=expense_category.id,
            booked_at=date(2026, 7, 10),
            amount="-3000.00",
            transaction_type="expense",
            fingerprint_seed="a",
        )
        db.commit()

        july_build = build_financial_snapshot(db, household_id=household.id, period="2026-07")
        persist_financial_snapshot(db, july_build)
        db.commit()

        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=income_category.id,
            booked_at=date(2026, 8, 5),
            amount="1200.00",
            transaction_type="income",
            fingerprint_seed="b",
        )
        db.commit()

        august_build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        assert august_build.liquidity.closing_balance == Decimal("0.00")
        assert august_build.liquidity.closing_uncovered_deficit == Decimal("800.00")


# ---------------------------------------------------------------------------
# Snapshot-driven integrity checks
# ---------------------------------------------------------------------------


def test_snapshot_invariant_checks_pass_on_well_formed_data() -> None:
    with _engine_session() as db:
        household, checking, _card = _seeded_household(db)
        transfer_category = _category(db, household.id, "Transferência interna")
        _transaction(
            db,
            household_id=household.id,
            account_id=checking.id,
            category_id=transfer_category.id,
            booked_at=date(2026, 8, 8),
            amount="-1000.00",
            transaction_type="transfer",
            excluded=True,
        )
        profile = FinancialProfile(household_id=household.id, central_liquidity_account_id=checking.id)
        db.add(profile)
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=checking.id,
                amount=Decimal("5000.00"),
                as_of_date=date(2026, 8, 1),
                observation_type="opening",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="trace-obs-3",
            )
        )
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        checks = build_snapshot_invariant_checks(build)
        assert checks
        for check in checks:
            result = evaluate_invariant(check.invariant_id, check.context)
            assert result.status is InvariantStatus.PASS, (check.invariant_id, result.to_dict())


def test_snapshot_invariant_checks_are_unknown_without_lineage_gap_being_hidden_as_pass() -> None:
    """A transaction with no linked account has a genuine lineage gap; INV-022
    must surface it, not silently treat it as fully traceable."""

    with _engine_session() as db:
        household = Household(name="Família Teste")
        db.add(household)
        db.flush()
        category = _category(db, household.id, "Receitas")
        transaction = Transaction(
            household_id=household.id,
            account_id=None,
            category_id=category.id,
            booked_at=date(2026, 8, 5),
            description="Renda sem conta vinculada",
            normalized_description="RENDA SEM CONTA VINCULADA",
            amount=Decimal("100.00"),
            transaction_type="income",
            owner_label="Família",
            fingerprint="a" * 64,
        )
        db.add(transaction)
        db.commit()

        build = build_financial_snapshot(db, household_id=household.id, period="2026-08")
        checks = build_snapshot_invariant_checks(build)
        lineage_checks = [check for check in checks if check.invariant_id == "INV-022"]
        assert lineage_checks
        result = evaluate_invariant(lineage_checks[0].invariant_id, lineage_checks[0].context)
        assert result.status is InvariantStatus.FAIL
        assert "account_ids" in result.actual["missing_dimensions"]

