import csv
import io
import json
import os
import uuid
import zipfile
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-diagnostic-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-diagnostic-data-{uuid.uuid4().hex}")
os.environ.setdefault("SECRET_KEY", "diagnostic-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.cli.export_diagnostics import export_diagnostic  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Category,
    Document,
    FinancialProfile,
    Household,
    ReviewItem,
    Transaction,
)


def test_diagnostic_export_reconciles_overlap_without_secrets(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Teste")
        db.add(household)
        db.flush()
        category = Category(
            household_id=household.id,
            name="Restaurantes e delivery",
            cash_cap=Decimal("0"),
        )
        manual_account = Account(
            household_id=household.id,
            name="Cartão manual",
            institution="Itaú",
            account_type="credit_card",
            owner_label="Família",
            last_four="1111",
        )
        plan_account = Account(
            household_id=household.id,
            name="Cartão consolidado",
            institution="Itaú",
            account_type="credit_card",
            owner_label="Família",
            last_four="1111",
        )
        db.add_all((category, manual_account, plan_account))
        db.flush()
        manual_document = Document(
            household_id=household.id,
            account_id=manual_account.id,
            original_name="fatura.pdf",
            document_type="credit_card",
            sha256="a" * 64,
            encrypted_path="private/manual.bin",
            status="imported",
            record_count=1,
        )
        plan_document = Document(
            household_id=household.id,
            original_name="plano.xlsx",
            document_type="financial_plan_workbook",
            sha256="b" * 64,
            encrypted_path="private/plan.bin",
            status="imported",
            record_count=1,
        )
        db.add_all((manual_document, plan_document))
        db.flush()
        db.add(FinancialProfile(household_id=household.id, monthly_cash_cap=Decimal("4000")))
        plan_transaction = Transaction(
            household_id=household.id,
            account_id=plan_account.id,
            document_id=plan_document.id,
            category_id=category.id,
            booked_at=date(2026, 8, 1),
            description="Restaurante Teste",
            normalized_description="RESTAURANTE TESTE",
            amount=Decimal("-100"),
            transaction_type="expense",
            owner_label="Família",
            fingerprint="c" * 64,
            confidence=Decimal("1"),
        )
        manual_transaction = Transaction(
            household_id=household.id,
            account_id=manual_account.id,
            document_id=manual_document.id,
            category_id=category.id,
            booked_at=date(2026, 8, 15),
            description="Restaurante Teste",
            normalized_description="RESTAURANTE TESTE",
            amount=Decimal("-100"),
            transaction_type="expense",
            owner_label="Família",
            fingerprint="d" * 64,
            confidence=Decimal("0.8"),
        )
        db.add_all((plan_transaction, manual_transaction))
        db.flush()
        db.add(
            ReviewItem(
                household_id=household.id,
                transaction_id=manual_transaction.id,
                document_id=manual_document.id,
                reason="classification_low_confidence",
                details="Conferir lançamento",
            )
        )
        db.commit()

        output = export_diagnostic(db, household, date(2026, 8, 1), tmp_path)

    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "LEIA-ME.txt",
            "contas.csv",
            "documentos.csv",
            "lancamentos.csv",
            "possiveis_duplicidades.csv",
            "resumo.json",
            "revisoes_abertas.csv",
        }
        summary = json.loads(archive.read("resumo.json"))
        assert summary["raw_spending"] == 200.0
        assert summary["consolidated_spending"] == 100.0
        assert summary["overlap_reduction"] == 100.0
        assert summary["overlap_rows_ignored"] == 1

        transactions = list(csv.DictReader(io.StringIO(archive.read("lancamentos.csv").decode("utf-8-sig"))))
        assert {row["calculation_status"] for row in transactions} == {
            "counted",
            "overlap_ignored",
        }
        package_text = "\n".join(
            archive.read(name).decode("utf-8-sig", errors="replace") for name in archive.namelist()
        ).lower()
        assert "private/manual.bin" not in package_text
        assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in package_text
        assert "password_hash" not in package_text
