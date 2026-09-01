from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

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
)
from app.services.financial_snapshots import build_snapshot


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
        patrimonial = Category(
            household_id=household.id, name="Transferência patrimonial"
        )
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
        db.commit()

        august = build_snapshot(db, household_id=household.id, period="2026-08")
        db.commit()
        assert august.opening_liquidity_balance == Decimal("50.00")
        assert august.operating_expenses == Decimal("100.00")
        assert august.card_spend == Decimal("100.00")
        assert august.card_payments == Decimal("100.00")
        assert august.redemptions == Decimal("25.00")
        assert august.operating_income == Decimal("0.00")
        assert august.closing_liquidity_balance == Decimal("0.00")
        assert august.closing_uncovered_deficit == Decimal("50.00")
        assert august.payload["opening_balance_source"] == "observation"

        same = build_snapshot(db, household_id=household.id, period="2026-08")
        assert same.id == august.id
        september = build_snapshot(db, household_id=household.id, period="2026-09")
        db.commit()
        assert september.opening_uncovered_deficit == Decimal("50.00")
        assert september.closing_uncovered_deficit == Decimal("0.00")
        assert september.closing_liquidity_balance == Decimal("25.00")
        lineage = tuple(
            db.scalars(
                select(FinancialSnapshotLineage).where(
                    FinancialSnapshotLineage.snapshot_id == september.id
                )
            ).all()
        )
        assert any(item.rule_id == "PRIOR-UNCOVERED-DEFICIT-CARRY" for item in lineage)

        rebuilt = build_snapshot(
            db, household_id=household.id, period="2026-08", force=True
        )
        db.commit()
        db.refresh(august)
        assert rebuilt.version == august.version + 1
        assert rebuilt.checksum == august.checksum
        assert august.status == "superseded"
        assert august.superseded_by_id == rebuilt.id
        assert db.scalar(select(FinancialSnapshot).where(FinancialSnapshot.id == rebuilt.id))
