import os
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

TEST_ID = uuid.uuid4().hex
os.environ["DATABASE_URL"] = f"sqlite:////tmp/ffp-api-{TEST_ID}.sqlite"
os.environ["DATA_DIR"] = f"/tmp/ffp-data-{TEST_ID}"
os.environ["SECRET_KEY"] = "integration-test-secret-that-is-never-used-in-production"
os.environ["FILE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_SECURE"] = "false"

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

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
        assert upload.json()["records"] == 3

        duplicate = client.post(
            "/api/imports",
            data={"account_id": account.json()["id"], "document_type": "credit_card"},
            files={"file": ("nubank-copy.csv", csv_payload, "text/csv")},
        )
        assert duplicate.status_code == 409

        dashboard = client.get("/api/dashboard").json()
        assert dashboard["spending"] == 76.78
        assert dashboard["food_benefits"] == 1630.0

        cuts = client.get("/api/cut-plan").json()
        assert cuts["covered_months"] == 1
        assert cuts["potential_monthly_savings"] == 44.88
        assert cuts["recommendations"][0]["category"] == "Restaurantes e delivery"

        forecast = client.get("/api/forecast").json()
        by_month = {row["month"]: row for row in forecast["rows"]}
        assert by_month["2027-03"]["commission_delayed"] == 9400.0

    assert list(Path(os.environ["DATA_DIR"]).glob("documents/*.bin"))
