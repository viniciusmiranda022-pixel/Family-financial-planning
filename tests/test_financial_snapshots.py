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
from app.services.financial_snapshots import _observation_is_trusted, build_snapshot


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
