from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import reports
from app.db import Base
from app.models import (
    Account,
    AccountBalanceObservation,
    Category,
    FinancialProfile,
    FinancialSnapshot,
    FinancialSnapshotLineage,
    Household,
    Transaction,
    User,
)
from app.services.financial_invariants import InvariantContext, InvariantScope, InvariantStatus
from app.services.financial_snapshots import (
    _observation_is_trusted,
    build_snapshot,
    liquidity_transition_facts,
    snapshot_lineage_facts,
)
from app.services.invariant_registry import evaluate_invariant


def test_only_confirmed_or_reconciled_balance_evidence_is_trusted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Evidência")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        db.add(investment)
        db.flush()
        imported = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("100"),
            as_of_date=date(2026, 9, 1),
            observation_type="closing",
            source="statement",
            confidence=Decimal("0.7000"),
            trace_id="unreconciled",
        )
        confirmed = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("100"),
            as_of_date=date(2026, 9, 2),
            observation_type="point_in_time",
            source="manual_confirmed",
            confidence=Decimal("1.0000"),
            trace_id="confirmed",
        )
        db.add_all([imported, confirmed])
        db.flush()

        assert not _observation_is_trusted(db, imported)
        assert _observation_is_trusted(db, confirmed)


def test_point_in_time_balance_becomes_next_period_opening_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Ponto no Tempo")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        profile = FinancialProfile(
            household_id=household.id,
            investment_name="Reserva DI",
            investment_balance=Decimal("999"),
        )
        db.add_all([investment, profile])
        db.flush()
        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=investment.id,
                amount=Decimal("321.45"),
                as_of_date=date(2026, 9, 2),
                observation_type="point_in_time",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id="confirmed-point",
            )
        )
        db.flush()

        september = build_snapshot(db, household_id=household.id, period="2026-09")
        october = build_snapshot(db, household_id=household.id, period="2026-10")

        assert not september.payload["balance_evidence_trusted"]
        assert october.payload["balance_evidence_trusted"]
        assert october.opening_liquidity_balance == Decimal("321.45")


def _transaction(
    household: Household,
    account: Account,
    category: Category,
    *,
    booked_at: date,
    amount: str,
    transaction_type: str,
    suffix: str,
    excluded: bool = False,
) -> Transaction:
    return Transaction(
        household_id=household.id,
        account_id=account.id,
        category_id=category.id,
        booked_at=booked_at,
        description=f"Movimento {suffix}",
        normalized_description=f"MOVIMENTO {suffix}",
        amount=Decimal(amount),
        transaction_type=transaction_type,
        fingerprint=suffix.rjust(64, "0"),
        canonical_status="canonical",
        excluded=excluded,
    )


def test_snapshots_separate_economic_cash_card_and_patrimonial_flows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Snapshot")
        db.add(household)
        db.flush()
        investment = Account(
            household_id=household.id,
            name="Privilège DI",
            institution="Itaú",
            account_type="investment",
        )
        card = Account(
            household_id=household.id,
            name="Cartão",
            institution="Banco",
            account_type="credit_card",
        )
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        reconciliation = Category(household_id=household.id, name="Conciliação")
        patrimonial = Category(household_id=household.id, name="Transferência patrimonial")
        db.add_all([investment, card, expense, income, reconciliation, patrimonial])
        db.flush()
        profile = FinancialProfile(
            household_id=household.id,
            monthly_cash_cap=Decimal("1000"),
            emergency_floor=Decimal("20"),
            investment_name="Privilège DI",
            investment_balance=Decimal("9999"),
        )
        observation = AccountBalanceObservation(
            household_id=household.id,
            account_id=investment.id,
            amount=Decimal("50"),
            as_of_date=date(2026, 8, 1),
            observation_type="opening",
            source="manual_confirmed",
            confidence=Decimal("1"),
            trace_id="observation-trace",
        )
        db.add_all(
            [
                profile,
                observation,
                _transaction(
                    household,
                    card,
                    expense,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="1",
                ),
                _transaction(
                    household,
                    card,
                    reconciliation,
                    booked_at=date(2026, 8, 10),
                    amount="100",
                    transaction_type="reconciliation",
                    suffix="2",
                    excluded=True,
                ),
                _transaction(
                    household,
                    investment,
                    patrimonial,
                    booked_at=date(2026, 8, 12),
                    amount="25",
                    transaction_type="transfer",
                    suffix="3",
                    excluded=True,
                ),
                _transaction(
                    household,
                    investment,
                    income,
                    booked_at=date(2026, 9, 2),
                    amount="75",
                    transaction_type="income",
                    suffix="4",
                ),
            ]
        )
        duplicate_candidate = _transaction(
            household,
            card,
            expense,
            booked_at=date(2026, 8, 6),
            amount="-200",
            transaction_type="expense",
            suffix="5",
        )
        duplicate_candidate.canonical_status = "unassigned"
        duplicate_candidate.possible_duplicate = True
        db.add(duplicate_candidate)
        db.commit()

        # Requesting September first must materialize August and carry exactly
        # the same deterministic predecessor state.
        september = build_snapshot(db, household_id=household.id, period="2026-09")
        db.commit()
        august = db.scalar(
            select(FinancialSnapshot).where(
                FinancialSnapshot.household_id == household.id,
                FinancialSnapshot.period == "2026-08",
                FinancialSnapshot.status == "current",
            )
        )
        assert august is not None
        assert august.opening_liquidity_balance == Decimal("50.00")
        assert august.operating_expenses == Decimal("100.00")
        assert august.card_spend == Decimal("100.00")
        assert august.card_payments == Decimal("100.00")
        assert august.redemptions == Decimal("25.00")
        assert august.operating_income == Decimal("0.00")
        assert august.closing_liquidity_balance == Decimal("0.00")
        assert august.closing_uncovered_deficit == Decimal("50.00")
        assert august.payload["opening_balance_source"] == "observation"
        assert august.payload["duplicates_ignored"] == 1
        duplicate_lineage = db.scalar(
            select(FinancialSnapshotLineage).where(
                FinancialSnapshotLineage.snapshot_id == august.id,
                FinancialSnapshotLineage.entity_id == duplicate_candidate.id,
            )
        )
        assert duplicate_lineage is not None
        assert duplicate_lineage.source_role == "excluded"
        assert duplicate_lineage.contribution is None
        payment_lineage = db.scalar(
            select(FinancialSnapshotLineage).where(
                FinancialSnapshotLineage.snapshot_id == august.id,
                FinancialSnapshotLineage.metric_key == "card_payments",
            )
        )
        assert payment_lineage is not None
        assert payment_lineage.source_role == "canonical"
        assert payment_lineage.contribution == Decimal("100.00")

        same = build_snapshot(db, household_id=household.id, period="2026-08")
        assert same.id == august.id
        assert september.opening_uncovered_deficit == Decimal("50.00")
        assert september.closing_uncovered_deficit == Decimal("0.00")
        assert september.closing_liquidity_balance == Decimal("25.00")
        lineage = tuple(
            db.scalars(
                select(FinancialSnapshotLineage).where(FinancialSnapshotLineage.snapshot_id == september.id)
            ).all()
        )
        assert any(item.rule_id == "PRIOR-UNCOVERED-DEFICIT-CARRY" for item in lineage)

        rebuilt = build_snapshot(db, household_id=household.id, period="2026-08", force=True)
        db.commit()
        db.refresh(august)
        assert rebuilt.version == august.version + 1
        assert rebuilt.checksum == august.checksum
        assert august.status == "superseded"
        assert august.superseded_by_id == rebuilt.id
        assert db.scalar(select(FinancialSnapshot).where(FinancialSnapshot.id == rebuilt.id))


def test_report_aggregates_snapshot_refunds_and_latest_balance_observation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        household = Household(name="Família Relatório")
        db.add(household)
        db.flush()
        user = User(
            household_id=household.id,
            name="Admin",
            username="report-admin",
            password_hash="hash",
            is_admin=True,
        )
        investment = Account(
            household_id=household.id,
            name="Reserva DI",
            account_type="investment",
        )
        card = Account(
            household_id=household.id,
            name="Cartão",
            account_type="credit_card",
        )
        purchases = Category(household_id=household.id, name="Compras")
        profile = FinancialProfile(
            household_id=household.id,
            monthly_cash_cap=Decimal("500"),
            emergency_floor=Decimal("0"),
            investment_name="Reserva DI",
            investment_balance=Decimal("9999"),
        )
        db.add_all([user, investment, card, purchases, profile])
        db.flush()
        db.add_all(
            [
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("100"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="august-balance",
                ),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("1000"),
                    as_of_date=date(2026, 9, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="september-balance",
                ),
                _transaction(
                    household,
                    card,
                    purchases,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="report-expense",
                ),
                _transaction(
                    household,
                    card,
                    purchases,
                    booked_at=date(2026, 8, 6),
                    amount="20",
                    transaction_type="refund",
                    suffix="report-refund",
                ),
            ]
        )
        db.commit()

        result = reports(end_month="2026-09", months=2, user=user, db=db)

        assert result["summary"]["total_spending"] == 80.0
        assert result["categories"] == [
            {
                "category": "Compras",
                "amount": 80.0,
                "average": 80.0,
                "share": 100.0,
                "color": "#64748B",
            }
        ]
        assert result["summary"]["liquidity_starting_balance"] == 100.0
        assert result["summary"]["liquidity_balance"] == 1000.0
        assert result["summary"]["liquidity_withdrawal"] == 80.0
        assert result["monthly"][-1]["snapshot_id"]


def _evaluate_projection_gate_check(invariant_id: str, facts: dict[str, object]):
    return evaluate_invariant(
        invariant_id,
        InvariantContext(
            facts=facts,
            scope=InvariantScope.PROJECTION,
            entity_type="projection",
            entity_id="projection:household-gate:2026-09",
            period="2026-09",
            trace_id="trace-projection-gate-test",
        ),
    )


def test_liquidity_transition_facts_prove_a_debt_carrying_snapshot() -> None:
    """INV-005/INV-006 must evaluate PASS on a real, debt-carrying snapshot.

    `calculate_liquidity_transition` (the invariants' own independent
    formula) has no parameter for prior uncovered debt, so
    `liquidity_transition_facts` must fold it into a single-step transition
    before calling it. This proves that fold reproduces the engine's own
    numbers for a snapshot that actually carries debt into the period being
    checked -- the exact shape of PR 34's regression scenario -- not just the
    debt-free case already covered by `tests/test_financial_invariants.py`.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Liquidez")
        db.add(household)
        db.flush()
        investment = Account(household_id=household.id, name="Reserva DI", account_type="investment")
        card = Account(household_id=household.id, name="Cartão", account_type="credit_card")
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        db.add_all([investment, card, expense, income])
        db.flush()
        db.add_all(
            [
                FinancialProfile(household_id=household.id, investment_name="Reserva DI"),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("50"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="opening-observation",
                ),
                _transaction(
                    household,
                    card,
                    expense,
                    booked_at=date(2026, 8, 5),
                    amount="-100",
                    transaction_type="expense",
                    suffix="1",
                ),
                _transaction(
                    household,
                    investment,
                    income,
                    booked_at=date(2026, 9, 2),
                    amount="75",
                    transaction_type="income",
                    suffix="2",
                ),
            ]
        )
        db.commit()

        september = build_snapshot(db, household_id=household.id, period="2026-09")
        assert september.opening_uncovered_deficit == Decimal("50.00")
        assert september.closing_uncovered_deficit == Decimal("0.00")
        assert september.closing_liquidity_balance == Decimal("25.00")

        facts = liquidity_transition_facts(september)
        assert facts["opening_liquidity_balance"] == Decimal("0.00")
        assert facts["monthly_operating_result"] == Decimal("25.00")
        non_negative_balance = _evaluate_projection_gate_check("INV-005", facts)
        deficit_consumes_liquidity = _evaluate_projection_gate_check("INV-006", facts)
        assert non_negative_balance.status is InvariantStatus.PASS
        assert deficit_consumes_liquidity.status is InvariantStatus.PASS


def test_liquidity_transition_facts_catch_a_broken_engine_result() -> None:
    """A snapshot that misreports its own closing figures must FAIL.

    This is the fold's contrapositive to the previous test: if `settle_liquidity`
    ever mis-happens to publish a closing balance that doesn't match the
    single-step transition implied by the snapshot's own opening/result/debt
    fields, `liquidity_transition_facts` + INV-005 must catch it instead of
    trusting the engine's own arithmetic -- proving the check is a real
    second opinion, not a tautology that always agrees with the engine.
    """

    from types import SimpleNamespace

    broken_snapshot = SimpleNamespace(
        opening_liquidity_balance=Decimal("0.00"),
        operating_result=Decimal("75.00"),
        investment_yield=Decimal("0.00"),
        opening_uncovered_deficit=Decimal("50.00"),
        # Correct closing balance would be 25.00 (75 - 50 of debt paid down).
        closing_liquidity_balance=Decimal("30.00"),
        closing_uncovered_deficit=Decimal("0.00"),
        liquidity_used=Decimal("0.00"),
    )
    facts = liquidity_transition_facts(broken_snapshot)
    result = _evaluate_projection_gate_check("INV-005", facts)
    assert result.status is InvariantStatus.FAIL


def test_snapshot_lineage_facts_require_transactions_accounts_and_categories() -> None:
    """INV-022 must FAIL for an otherwise-trusted snapshot with an empty period.

    A manually confirmed opening balance alone identifies no transaction,
    account, category or rule lineage, so a period with zero transactions
    cannot claim complete traceability -- exactly the gap that let
    `trusted_for_projection` become true without genuine invariant coverage.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Rastreabilidade")
        db.add(household)
        db.flush()
        investment = Account(household_id=household.id, name="Reserva DI", account_type="investment")
        card = Account(household_id=household.id, name="Cartão", account_type="credit_card")
        expense = Category(household_id=household.id, name="Compras")
        income = Category(household_id=household.id, name="Receitas")
        db.add_all([investment, card, expense, income])
        db.flush()
        db.add_all(
            [
                FinancialProfile(household_id=household.id, investment_name="Reserva DI"),
                AccountBalanceObservation(
                    household_id=household.id,
                    account_id=investment.id,
                    amount=Decimal("50"),
                    as_of_date=date(2026, 8, 1),
                    observation_type="opening",
                    source="manual_confirmed",
                    confidence=Decimal("1"),
                    trace_id="opening-observation",
                ),
            ]
        )
        db.commit()

        empty_period = build_snapshot(db, household_id=household.id, period="2026-08")
        empty_facts = snapshot_lineage_facts(db, empty_period)
        assert empty_facts["transaction_ids"] == []
        assert empty_facts["account_ids"] == []
        assert empty_facts["category_ids"] == []
        empty_result = _evaluate_projection_gate_check("INV-022", empty_facts)
        assert empty_result.status is InvariantStatus.FAIL

        db.add(
            _transaction(
                household,
                card,
                expense,
                booked_at=date(2026, 8, 5),
                amount="-100",
                transaction_type="expense",
                suffix="lineage-1",
            )
        )
        db.commit()

        populated_period = build_snapshot(db, household_id=household.id, period="2026-08", force=True)
        populated_facts = snapshot_lineage_facts(db, populated_period)
        assert populated_facts["transaction_ids"] == [
            db.scalar(select(Transaction.id).where(Transaction.household_id == household.id))
        ]
        assert populated_facts["account_ids"] == [card.id]
        assert populated_facts["category_ids"] == [expense.id]
        assert populated_facts["source_count"] == len(populated_facts["source_ids"])
        populated_result = _evaluate_projection_gate_check("INV-022", populated_facts)
        assert populated_result.status is InvariantStatus.PASS
