import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

TEST_ID = uuid.uuid4().hex
os.environ["DATABASE_URL"] = f"sqlite:////tmp/ffp-api-{TEST_ID}.sqlite"
os.environ["DATA_DIR"] = f"/tmp/ffp-data-{TEST_ID}"
os.environ["SECRET_KEY"] = "integration-test-secret-that-is-never-used-in-production"
os.environ["FILE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_SECURE"] = "false"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category, Document, Household, ReviewItem, Transaction  # noqa: E402

Base.metadata.create_all(bind=engine)


def test_complete_local_financial_flow() -> None:
    with TestClient(app) as client:
        setup = client.post(
            "/api/auth/setup",
            json={
                "household_name": "Família Teste",
                "name": "Administrador",
                "username": "admin",
                "password": "senha-local-segura",
            },
        )
        assert setup.status_code == 201

        family_user = client.post(
            "/api/users",
            json={
                "name": "Kelly",
                "username": "kelly",
                "password": "senha-kelly-segura",
                "is_admin": False,
            },
        )
        assert family_user.status_code == 201
        users = client.get("/api/users").json()
        assert {(item["username"], item["is_admin"]) for item in users} == {
            ("admin", True),
            ("kelly", False),
        }

        account = client.post(
            "/api/accounts",
            json={
                "name": "Nubank",
                "institution": "Nubank",
                "account_type": "credit_card",
                "owner_label": "Família",
                "last_four": "1234",
            },
        )
        assert account.status_code == 201

        profile = client.put(
            "/api/profile",
            json={
                "monthly_salary_net": 5000,
                "monthly_cash_cap": 3000,
                "emergency_floor": 10000,
                "food_allowance": 1000,
                "meal_allowance_daily": 30,
                "workdays_month": 21,
                "investment_name": "Privilege DI",
                "investment_balance": 20000,
                "investment_gross_annual_rate": 0.1402265,
                "investment_income_tax_rate": 0.225,
                "projection_end": "2027-05-01",
            },
        )
        assert profile.status_code == 200

        commission = client.post(
            "/api/commissions",
            json={
                "description": "Recebível 2027",
                "expected_date": "2027-01-15",
                "gross_amount": 10000,
                "tax_rate": 0.06,
                "delay_days": 60,
            },
        )
        assert commission.status_code == 201
        assert commission.json()["net"] == 9400.0

        csv_payload = (
            b"date,title,amount\n"
            b"2026-07-31,iFood - NuPay,33.40\n"
            b"2026-08-04,iFood - NuPay,44.88\n"
            b"2026-08-03,Pagamento recebido,-1724.90\n"
            b"2026-08-02,Spotify,31.90\n"
        )
        upload = client.post(
            "/api/imports",
            data={"account_id": account.json()["id"], "document_type": "credit_card"},
            files={"file": ("nubank.csv", csv_payload, "text/csv")},
        )
        assert upload.status_code == 201
        assert upload.json()["records"] == 4

        duplicate = client.post(
            "/api/imports",
            data={"account_id": account.json()["id"], "document_type": "credit_card"},
            files={"file": ("nubank-copy.csv", csv_payload, "text/csv")},
        )
        assert duplicate.status_code == 409

        with SessionLocal() as db:
            household = db.scalar(select(Household))
            category = db.scalar(select(Category).where(Category.name == "Restaurantes e delivery"))
            plan_account = Account(
                household_id=household.id,
                name="Cartão consolidado",
                institution="Itaú",
                account_type="credit_card",
                owner_label="Família",
            )
            plan_document = Document(
                household_id=household.id,
                original_name="plano.xlsx",
                document_type="financial_plan_workbook",
                sha256="a" * 64,
                encrypted_path="documents/plan.bin",
                status="imported",
                record_count=1,
            )
            db.add_all((plan_account, plan_document))
            db.flush()
            db.add(
                Transaction(
                    household_id=household.id,
                    account_id=plan_account.id,
                    document_id=plan_document.id,
                    category_id=category.id,
                    booked_at=date(2026, 8, 1),
                    description="iFood - NuPay",
                    normalized_description="IFOOD NUPAY",
                    amount=Decimal("-44.88"),
                    transaction_type="expense",
                    owner_label="Família",
                    fingerprint="b" * 64,
                    confidence=Decimal("1"),
                )
            )
            august_transaction = db.scalar(
                select(Transaction).where(
                    Transaction.booked_at == date(2026, 8, 4),
                    Transaction.description == "iFood - NuPay",
                )
            )
            july_transaction = db.scalar(
                select(Transaction).where(
                    Transaction.booked_at == date(2026, 7, 31),
                    Transaction.description == "iFood - NuPay",
                )
            )
            assert august_transaction is not None
            assert july_transaction is not None
            db.add_all(
                (
                    ReviewItem(
                        household_id=household.id,
                        transaction_id=august_transaction.id,
                        reason="test_august_review",
                    ),
                    ReviewItem(
                        household_id=household.id,
                        transaction_id=july_transaction.id,
                        reason="test_july_review",
                    ),
                )
            )
            db.commit()

        dashboard = client.get("/api/dashboard").json()
        assert dashboard["spending"] == 76.78
        assert dashboard["food_benefits"] == 1630.0
        assert dashboard["duplicates_ignored"] == 1
        assert dashboard["review_count"] == 1
        assert dashboard["category_spending"][0] == {
            "category": "Restaurantes e delivery",
            "amount": 44.88,
        }

        july_dashboard = client.get("/api/dashboard?month=2026-07")
        assert july_dashboard.status_code == 200
        assert july_dashboard.json()["month"] == "2026-07"
        assert july_dashboard.json()["spending"] == 33.4
        assert july_dashboard.json()["review_count"] == 1
        assert client.get("/api/dashboard?month=07-2026").status_code == 422
        july_cuts = client.get("/api/cut-plan?month=2026-07").json()
        assert july_cuts["covered_months"] == 1
        assert july_cuts["potential_monthly_savings"] == 8.35

        cuts = client.get("/api/cut-plan").json()
        assert cuts["covered_months"] == 2
        assert cuts["duplicates_ignored"] == 1
        assert cuts["potential_monthly_savings"] == 9.79
        assert cuts["recommendations"][0]["category"] == "Restaurantes e delivery"

        forecast = client.get("/api/forecast").json()
        by_month = {row["month"]: row for row in forecast["rows"]}
        assert by_month["2027-03"]["commission_delayed"] == 9400.0

        restaurant_category = next(
            item for item in client.get("/api/categories").json()
            if item["name"] == "Restaurantes e delivery"
        )
        manual_expense = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Abastecimento manual",
                "amount": 100,
                "movement_type": "expense",
                "account_id": account.json()["id"],
                "category_id": restaurant_category["id"],
            },
        )
        assert manual_expense.status_code == 201
        manual_investment = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Aplicação no Privilege DI",
                "amount": 500,
                "movement_type": "investment",
                "account_id": account.json()["id"],
            },
        )
        assert manual_investment.status_code == 201
        dashboard_with_manual = client.get("/api/dashboard?month=2026-08").json()
        assert dashboard_with_manual["spending"] == 176.78
        assert dashboard_with_manual["cash_in"] == 0
        assert dashboard_with_manual["cash_out"] == 676.78
        assert dashboard_with_manual["investment_balance"] == 20500

        transaction_rows = client.get("/api/transactions?month=2026-08").json()
        assert sum(item["manual"] for item in transaction_rows) == 2
        imported = next(item for item in transaction_rows if not item["manual"])
        assert client.delete(f"/api/transactions/{imported['id']}").status_code == 409
        assert client.delete(
            f"/api/transactions/{manual_expense.json()['id']}"
        ).status_code == 200
        assert client.delete(
            f"/api/transactions/{manual_investment.json()['id']}"
        ).status_code == 200
        dashboard_after_delete = client.get("/api/dashboard?month=2026-08").json()
        assert dashboard_after_delete["spending"] == 76.78
        assert dashboard_after_delete["cash_out"] == 76.78
        assert dashboard_after_delete["investment_balance"] == 20000

        obligation = client.post(
            "/api/obligations",
            json={
                "name": "Compromisso temporário",
                "due_date": "2026-12-10",
                "amount": 1000,
                "recurrence_months": 0,
                "occurrence_count": 1,
                "category": "general",
            },
        )
        assert obligation.status_code == 201
        assert client.delete(f"/api/obligations/{obligation.json()['id']}").status_code == 200

        payroll = client.post(
            "/api/payroll",
            json={
                "person_name": "Kelly",
                "competence": "2026-08-01",
                "payment_date": "2026-09-05",
                "payroll_kind": "other",
                "gross_amount": 100,
                "deductions": 0,
                "net_amount": 100,
                "payroll_loan": 0,
            },
        )
        assert payroll.status_code == 201
        assert client.delete(f"/api/payroll/{payroll.json()['id']}").status_code == 200
        assert client.delete(f"/api/commissions/{commission.json()['id']}").status_code == 200
        with TestClient(app) as family_client:
            family_login = family_client.post(
                "/api/auth/login",
                json={"username": "kelly", "password": "senha-kelly-segura"},
            )
            assert family_login.status_code == 200
            assert family_login.json()["is_admin"] is False
            assert family_client.get("/api/dashboard?month=2026-08").json()["spending"] == 76.78
            assert family_client.get("/api/users").status_code == 403
        assert client.delete(f"/api/users/{family_user.json()['id']}").status_code == 200
        assert next(item for item in client.get("/api/users").json() if item["username"] == "kelly")[
            "active"
        ] is False

    assert list(Path(os.environ["DATA_DIR"]).glob("documents/*.bin"))
