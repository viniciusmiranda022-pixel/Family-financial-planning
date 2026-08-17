from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import _consolidated_expenses
from app.db import Base
from app.models import Account, Category, Document, Household, Transaction
from app.services.classifier import normalize_description


def _transaction(
    household_id: str,
    account_id: str,
    document_id: str,
    category_id: str,
    booked_at: date,
    description: str,
    amount: str,
    transaction_type: str = "expense",
    excluded: bool = False,
) -> Transaction:
    fingerprint = f"{document_id}:{booked_at}:{description}:{amount}"
    return Transaction(
        household_id=household_id,
        account_id=account_id,
        document_id=document_id,
        category_id=category_id,
        booked_at=booked_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=Decimal(amount),
        transaction_type=transaction_type,
        owner_label="Família",
        fingerprint=fingerprint,
        confidence=Decimal("1"),
        excluded=excluded,
    )


def test_workbook_exclusions_remain_authoritative_over_statement_copies() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Teste")
        db.add(household)
        db.flush()
        category = Category(household_id=household.id, name="Revisar")
        plan_account = Account(
            household_id=household.id,
            name="Itaú Conta Corrente",
            institution="Itaú",
            account_type="checking",
            owner_label="Família",
        )
        statement_account = Account(
            household_id=household.id,
            name="Conta importada",
            institution="Itaú",
            account_type="checking",
            owner_label="Família",
        )
        db.add_all((category, plan_account, statement_account))
        db.flush()
        plan_document = Document(
            household_id=household.id,
            original_name="plano.xlsx",
            document_type="financial_plan_workbook",
            sha256="a" * 64,
            encrypted_path="documents/plan.bin",
            status="imported",
        )
        statement_document = Document(
            household_id=household.id,
            account_id=statement_account.id,
            original_name="extrato.pdf",
            document_type="bank_statement",
            sha256="b" * 64,
            encrypted_path="documents/statement.bin",
            status="imported",
        )
        db.add_all((plan_document, statement_document))
        db.flush()

        cases = (
            (date(2026, 8, 3), "PIX TRANSF JOSE FR03/08", "-1000", "expense"),
            (date(2026, 8, 6), "PIX TRANSF GILVANO06/08", "-15000", "expense"),
            (date(2026, 8, 11), "PIX TRANSF Viniciu11/08", "-6000", "transfer"),
        )
        for booked_at, description, amount, plan_type in cases:
            db.add(
                _transaction(
                    household.id,
                    plan_account.id,
                    plan_document.id,
                    category.id,
                    booked_at,
                    description,
                    amount,
                    transaction_type=plan_type,
                    excluded=True,
                )
            )
            db.add(
                _transaction(
                    household.id,
                    statement_account.id,
                    statement_document.id,
                    category.id,
                    booked_at,
                    description,
                    amount,
                )
            )
        db.add(
            _transaction(
                household.id,
                plan_account.id,
                plan_document.id,
                category.id,
                date(2026, 8, 8),
                "Despesa válida",
                "-11022.92",
            )
        )
        db.commit()

        rows, duplicates_ignored = _consolidated_expenses(
            db,
            household.id,
            date(2026, 8, 1),
            date(2026, 9, 1),
        )

    assert duplicates_ignored == 3
    assert len(rows) == 1
    assert rows[0][0].description == "Despesa válida"
    assert rows[0][0].amount == Decimal("-11022.92")
