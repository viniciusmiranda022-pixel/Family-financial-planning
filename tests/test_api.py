import os
import uuid
from datetime import date, timedelta
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
from app.services.codex_client import CodexResult  # noqa: E402

Base.metadata.create_all(bind=engine)


def test_complete_local_financial_flow(monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 8, 31)

    monkeypatch.setattr("app.api.date", FixedDate)

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

        capture_preview = client.post(
            "/api/captures/preview",
            data={
                "text": "Gastei R$ 150 com combustível ontem",
                "account_id": account.json()["id"],
                "document_type": "auto",
            },
        )
        assert capture_preview.status_code == 201
        capture_data = capture_preview.json()
        assert capture_data["status"] == "preview"
        assert capture_data["detected_type"] == "text"
        assert capture_data["items"][0]["category_name"] == "Transporte"
        capture_confirm = client.post(
            f"/api/captures/{capture_data['id']}/confirm",
            json={"items": capture_data["items"]},
        )
        assert capture_confirm.status_code == 200
        captured_transaction_id = capture_confirm.json()["result"]["transactions"][0]
        assert (
            client.post(
                f"/api/captures/{capture_data['id']}/confirm",
                json={"items": capture_data["items"]},
            ).status_code
            == 409
        )
        assert client.delete(f"/api/transactions/{captured_transaction_id}").status_code == 200

        boleto_preview = client.post(
            "/api/captures/preview",
            data={
                "text": (
                    "ENERGIA DA CASA\nVENCIMENTO 30/08/2026\n"
                    "VALOR DO DOCUMENTO R$ 350,40\nLINHA DIGITÁVEL 00190.00009"
                ),
                "document_type": "boleto",
            },
        )
        assert boleto_preview.status_code == 201
        boleto_data = boleto_preview.json()
        assert boleto_data["items"][0]["kind"] == "obligation"
        boleto_confirm = client.post(
            f"/api/captures/{boleto_data['id']}/confirm",
            json={"items": boleto_data["items"]},
        )
        assert boleto_confirm.status_code == 200
        captured_obligation_id = boleto_confirm.json()["result"]["obligations"][0]
        assert client.delete(f"/api/obligations/{captured_obligation_id}").status_code == 200
        captures = client.get("/api/captures").json()
        assert {item["status"] for item in captures} == {"confirmed"}
        advisor_status = client.get("/api/advisor/status")
        assert advisor_status.status_code == 200
        assert advisor_status.json()["local_engine"] is True

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

        large_capture_preview = client.post(
            "/api/captures/preview",
            data={
                "text": "Gastei R$ 80 com combustível hoje",
                "account_id": account.json()["id"],
                "document_type": "auto",
            },
        )
        assert large_capture_preview.status_code == 201
        large_capture_data = large_capture_preview.json()
        large_capture_items = large_capture_data["items"]
        large_capture_items[0]["amount"] = 8000
        assert client.post(
            f"/api/captures/{large_capture_data['id']}/confirm",
            json={"items": large_capture_items},
        ).status_code == 409
        confirmed_large_capture = client.post(
            f"/api/captures/{large_capture_data['id']}/confirm",
            json={"items": large_capture_items, "confirmed_large_amount": True},
        )
        assert confirmed_large_capture.status_code == 200
        large_capture_transaction = confirmed_large_capture.json()["result"]["transactions"][0]
        assert client.delete(f"/api/transactions/{large_capture_transaction}").status_code == 200

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
            b"2026-08-02,Spotify 1/4,31.90\n"
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

        dashboard = client.get("/api/dashboard?month=2026-08").json()
        assert dashboard["spending"] == 76.78
        assert dashboard["food_benefits"] == 1630.0
        assert dashboard["liquidity_name"] == "Privilege DI"
        assert dashboard["liquidity_starting_balance"] == 20000
        assert dashboard["liquidity_balance"] == 19923.22
        assert dashboard["liquidity_available"] == 9923.22
        assert dashboard["liquidity_withdrawal"] == 76.78
        assert dashboard["liquidity_uncovered_deficit"] == 0
        assert dashboard["liquidity_flow"] == -76.78
        assert dashboard["liquidity_direction"] == "withdrawal"
        assert dashboard["bank_cash_out"] == 0
        assert dashboard["card_spending"] == 76.78
        assert dashboard["duplicates_ignored"] == 1
        assert dashboard["review_count"] == 1
        assert dashboard["category_spending"][0] == {
            "category": "Restaurantes e delivery",
            "amount": 44.88,
        }
        review_rows = client.get("/api/reviews").json()
        august_review = next(item for item in review_rows if item["reason"] == "test_august_review")
        assert august_review["transaction_id"]
        assert august_review["category"] == "Restaurantes e delivery"
        assert august_review["account"]
        reviewed = client.patch(
            f"/api/transactions/{august_review['transaction_id']}",
            json={
                "category_id": august_review["category_id"],
                "excluded": False,
                "possible_duplicate": False,
                "reviewed": True,
            },
        )
        assert reviewed.status_code == 200
        assert august_review["id"] not in {item["id"] for item in client.get("/api/reviews").json()}

        july_dashboard = client.get("/api/dashboard?month=2026-07")
        assert july_dashboard.status_code == 200
        assert july_dashboard.json()["month"] == "2026-07"
        assert july_dashboard.json()["spending"] == 33.4
        assert july_dashboard.json()["review_count"] == 1
        assert client.get("/api/dashboard?month=07-2026").status_code == 422
        report = client.get("/api/reports?end_month=2026-08&months=2")
        assert report.status_code == 200
        report_data = report.json()
        assert report_data["start_month"] == "2026-07"
        assert report_data["end_month"] == "2026-08"
        assert report_data["months"] == 2
        assert [item["month"] for item in report_data["monthly"]] == ["2026-07", "2026-08"]
        assert report_data["monthly"][0]["spending"] == 33.4
        assert report_data["monthly"][1]["spending"] == 76.78
        assert report_data["summary"]["total_spending"] == 110.18
        assert report_data["summary"]["average_spending"] == 55.09
        assert report_data["summary"]["total_bank_cash_out"] == 0
        assert report_data["summary"]["total_card_spending"] == 110.18
        assert report_data["monthly"][1]["bank_cash_out"] == 0
        assert report_data["monthly"][1]["card_spending"] == 76.78
        assert report_data["summary"]["highest_month"] == "2026-08"
        assert report_data["summary"]["liquidity_name"] == "Privilege DI"
        assert report_data["summary"]["liquidity_starting_balance"] == 20000
        assert report_data["summary"]["liquidity_balance"] == 19889.82
        assert report_data["summary"]["liquidity_available"] == 9889.82
        assert report_data["summary"]["liquidity_withdrawal"] == 110.18
        assert report_data["summary"]["liquidity_uncovered_deficit"] == 0
        assert report_data["summary"]["liquidity_flow"] == -110.18
        assert report_data["summary"]["liquidity_direction"] == "withdrawal"
        assert report_data["categories"][0]["amount"] > 0
        assert client.get("/api/reports?months=13").status_code == 422
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
        assert by_month["2026-09"]["installments"] == 31.9
        assert by_month["2026-10"]["installments"] == 31.9
        assert by_month["2026-11"]["installments"] == 31.9
        assert by_month["2027-03"]["commission_delayed"] == 9400.0

        restaurant_category = next(
            item for item in client.get("/api/categories").json() if item["name"] == "Restaurantes e delivery"
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
        manual_income = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Receita manual",
                "amount": 1000,
                "movement_type": "income",
                "account_id": account.json()["id"],
            },
        )
        assert manual_income.status_code == 201
        manual_redemption = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Resgate do Privilege DI",
                "amount": 200,
                "movement_type": "redemption",
                "account_id": account.json()["id"],
            },
        )
        assert manual_redemption.status_code == 201
        dashboard_with_manual = client.get("/api/dashboard?month=2026-08").json()
        assert dashboard_with_manual["spending"] == 176.78
        assert dashboard_with_manual["cash_in"] == 1000
        assert dashboard_with_manual["cash_out"] == 176.78
        assert dashboard_with_manual["bank_cash_out"] == 0
        assert dashboard_with_manual["card_spending"] == 176.78
        assert dashboard_with_manual["investment_balance"] == 20300
        assert dashboard_with_manual["liquidity_starting_balance"] == 20300
        assert dashboard_with_manual["liquidity_balance"] == 21123.22
        assert dashboard_with_manual["liquidity_available"] == 11123.22
        assert dashboard_with_manual["liquidity_deposit"] == 823.22
        assert dashboard_with_manual["liquidity_flow"] == 823.22
        assert dashboard_with_manual["liquidity_direction"] == "deposit"
        nubank_flow = next(
            item
            for item in dashboard_with_manual["cash_flow_by_account"]
            if item["account_id"] == account.json()["id"]
        )
        assert nubank_flow["account_type"] == "credit_card"
        assert nubank_flow["cash_in"] == 1000
        assert nubank_flow["cash_out"] == 131.9
        assert nubank_flow["bank_cash_out"] == 0
        assert nubank_flow["card_spending"] == 131.9

        excluded_income = client.patch(
            f"/api/transactions/{manual_income.json()['id']}",
            json={"excluded": True},
        )
        assert excluded_income.status_code == 200
        assert client.get("/api/dashboard?month=2026-08").json()["cash_in"] == 0
        assert client.patch(
            f"/api/transactions/{manual_income.json()['id']}",
            json={"excluded": False},
        ).status_code == 200

        unconfirmed_large = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Simulação de compra grande",
                "amount": 80000,
                "movement_type": "expense",
                "account_id": account.json()["id"],
                "category_id": restaurant_category["id"],
            },
        )
        assert unconfirmed_large.status_code == 409
        assert "simulações" in unconfirmed_large.json()["detail"].lower()

        transaction_rows = client.get("/api/transactions?month=2026-08").json()
        assert sum(item["manual"] for item in transaction_rows) == 4
        imported = next(item for item in transaction_rows if not item["manual"])
        assert client.delete(f"/api/transactions/{imported['id']}").status_code == 409
        assert client.delete(f"/api/transactions/{manual_expense.json()['id']}").status_code == 200
        assert client.delete(f"/api/transactions/{manual_investment.json()['id']}").status_code == 200
        assert client.delete(f"/api/transactions/{manual_income.json()['id']}").status_code == 200
        assert client.delete(f"/api/transactions/{manual_redemption.json()['id']}").status_code == 200
        dashboard_after_delete = client.get("/api/dashboard?month=2026-08").json()
        assert dashboard_after_delete["spending"] == 76.78
        assert dashboard_after_delete["cash_in"] == 0
        assert dashboard_after_delete["cash_out"] == 76.78
        assert dashboard_after_delete["bank_cash_out"] == 0
        assert dashboard_after_delete["card_spending"] == 76.78
        assert dashboard_after_delete["investment_balance"] == 20000
        assert dashboard_after_delete["liquidity_balance"] == 19923.22
        assert dashboard_after_delete["liquidity_available"] == 9923.22

        uncovered_expense = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Teste de déficit maior que a liquidez",
                "amount": 25000,
                "movement_type": "expense",
                "account_id": account.json()["id"],
                "category_id": restaurant_category["id"],
                "confirmed_large_amount": True,
            },
        )
        assert uncovered_expense.status_code == 201
        exhausted = client.get("/api/dashboard?month=2026-08").json()
        assert exhausted["liquidity_starting_balance"] == 20000
        assert exhausted["liquidity_flow"] == -25076.78
        assert exhausted["liquidity_withdrawal"] == 20000
        assert exhausted["liquidity_balance"] == 0
        assert exhausted["liquidity_available"] == -10000
        assert exhausted["liquidity_uncovered_deficit"] == 5076.78
        assert client.delete(f"/api/transactions/{uncovered_expense.json()['id']}").status_code == 200

        liquidity_answer = client.post(
            "/api/advisor/chat",
            json={"message": "Quanto tenho disponível no Privilège DI?"},
        )
        assert liquidity_answer.status_code == 200
        liquidity_data = liquidity_answer.json()
        assert liquidity_data["intent"] == "liquidity"
        assert liquidity_data["metrics"]["liquidity_starting_balance"] == 20000
        assert liquidity_data["metrics"]["liquidity_balance"] == 19923.22
        assert liquidity_data["metrics"]["liquidity_available"] == 9923.22
        assert "conta" in liquidity_data["answer"].lower()
        assert "renda ou gasto" in liquidity_data["answer"]

        custom_expense = client.post(
            "/api/transactions",
            json={
                "booked_at": "2026-08-17",
                "description": "Paisagismo da chácara",
                "amount": 3100,
                "movement_type": "expense",
                "account_id": account.json()["id"],
                "category_name": "Paisagismo",
            },
        )
        assert custom_expense.status_code == 201
        assert "Paisagismo" in {item["name"] for item in client.get("/api/categories").json()}
        over_budget = client.get("/api/dashboard?month=2026-08").json()
        assert over_budget["remaining_cap"] == -176.78
        advisor_missing_payment = client.post(
            "/api/advisor/chat",
            json={"message": "Quero fazer uma compra que custa R$ 2.000, posso fazer?"},
        )
        assert advisor_missing_payment.status_code == 200
        assert advisor_missing_payment.json()["status"] == "insufficient_data"
        assert "à vista ou parcelada" in advisor_missing_payment.json()["answer"]
        advisor_bare_amount = client.post(
            "/api/advisor/chat",
            json={"message": "Comprar enxada de 250 posso?"},
        )
        assert advisor_bare_amount.status_code == 200
        assert advisor_bare_amount.json()["status"] == "insufficient_data"
        assert advisor_bare_amount.json()["metrics"]["purchase_amount"] == 250
        assert "à vista ou parcelada" in advisor_bare_amount.json()["answer"]
        advisor_four_digit_amount = client.post(
            "/api/advisor/chat",
            json={"message": "Em setembro quero comprar uma enxada rotativa de 2000 à vista"},
        )
        assert advisor_four_digit_amount.status_code == 200
        assert advisor_four_digit_amount.json()["metrics"]["purchase_amount"] == 2000
        assert "R$ 2.000,00" in advisor_four_digit_amount.json()["answer"]
        assert (
            "Caber matematicamente no orçamento não significa" in (advisor_four_digit_amount.json()["answer"])
        )
        assert "quantas vezes você realmente usará" in advisor_four_digit_amount.json()["answer"]
        advisor_small_cosmetic = client.post(
            "/api/advisor/chat",
            json={"message": "Em setembro comprar um esmalte de 2 reais"},
        )
        assert advisor_small_cosmetic.status_code == 200
        cosmetic_result = advisor_small_cosmetic.json()
        assert cosmetic_result["status"] == "favorable"
        assert cosmetic_result["metrics"]["payment"]["mode"] == "cash"
        assert cosmetic_result["metrics"]["payment"]["assumed_cash"] is True
        assert cosmetic_result["metrics"]["purchase_context"]["kind"] == "personal_care"
        assert cosmetic_result["metrics"]["purchase_context"]["analysis_depth"] == "quick"
        assert cosmetic_result["metrics"]["show_commitment_schedule"] is False
        assert "Estética e beleza" in cosmetic_result["answer"]
        assert "Cronograma dos próximos meses" not in cosmetic_result["answer"]
        assert "quanto custaria alugar" not in cosmetic_result["answer"]
        assert "haverá manutenção" not in cosmetic_result["answer"]
        advisor_with_long_history = client.post(
            "/api/advisor/chat",
            json={
                "message": "Em setembro comprar um esmalte de 2 reais",
                "history": [
                    {
                        "role": "assistant",
                        "content": "Compra consciente:\n" + ("contexto financeiro " * 180),
                    }
                ],
            },
        )
        assert advisor_with_long_history.status_code == 200
        with monkeypatch.context() as codex_patch:
            codex_patch.setattr(
                "app.api.CodexAdvisorClient.analyze",
                lambda _self, _payload: CodexResult(
                    {
                        "verdict": "not_recommended",
                        "answer": (
                            "Como o uso seria ocasional e o aluguel custa menos, não recomendo comprar agora."
                        ),
                        "evidence": ["Uso ocasional", "Aluguel mais barato"],
                        "assumptions": ["Frequência informada pelo usuário"],
                        "confidence": 0.92,
                        "provider": "codex",
                        "model": "modelo-de-teste",
                    }
                ),
            )
            advisor_reflection = client.post(
                "/api/advisor/chat",
                json={
                    "message": "Vou usar duas vezes por ano e o aluguel custa R$ 200 por dia",
                    "history": [
                        {
                            "role": "user",
                            "content": ("Em setembro quero comprar uma enxada rotativa de 2000 à vista"),
                        },
                        {
                            "role": "assistant",
                            "content": advisor_four_digit_amount.json()["answer"],
                        },
                    ],
                },
            )
        assert advisor_reflection.status_code == 200
        assert advisor_reflection.json()["intent"] == "purchase"
        assert advisor_reflection.json()["status"] == "not_recommended"
        assert advisor_reflection.json()["provider"] == "codex"
        assert advisor_reflection.json()["metrics"]["purchase_amount"] == 2000
        assert advisor_reflection.json()["metrics"]["purchase_reflection_answered"] is True
        assert "uso seria ocasional" in advisor_reflection.json()["answer"]

        shared_due_date = FixedDate.today() + timedelta(days=60)
        same_day_obligations = []
        for name in ("Parcela mensal da chácara", "Reforço da chácara"):
            response = client.post(
                "/api/obligations",
                json={
                    "name": name,
                    "due_date": shared_due_date.isoformat(),
                    "amount": 1500,
                    "recurrence_months": 0,
                    "occurrence_count": 1,
                    "category": "property",
                },
            )
            assert response.status_code == 201
            same_day_obligations.append(response.json()["id"])

        advisor_cash_follow_up = client.post(
            "/api/advisor/chat",
            json={
                "message": "será à vista",
                "history": [
                    {
                        "role": "user",
                        "content": "Comprar um carro de 20 mil, posso?",
                    },
                    {
                        "role": "assistant",
                        "content": "Essa compra será à vista ou parcelada?",
                    },
                ],
            },
        )
        assert advisor_cash_follow_up.status_code == 200
        assert advisor_cash_follow_up.json()["intent"] == "purchase"
        assert advisor_cash_follow_up.json()["metrics"]["purchase_amount"] == 20000
        assert advisor_cash_follow_up.json()["metrics"]["payment"]["mode"] == "cash"
        for obligation_id in same_day_obligations:
            assert client.delete(f"/api/obligations/{obligation_id}").status_code == 200

        with monkeypatch.context() as codex_patch:
            codex_patch.setattr(
                "app.api.CodexAdvisorClient.analyze",
                lambda _self, _payload: CodexResult(
                    {
                        "verdict": "not_recommended",
                        "answer": "Não recomendo a compra agora, pois o teto mensal já foi ultrapassado.",
                        "evidence": ["Teto mensal ultrapassado"],
                        "assumptions": ["Compra à vista"],
                        "confidence": 0.98,
                        "provider": "codex",
                        "model": "modelo-de-teste",
                    }
                ),
            )
            advisor = client.post(
                "/api/advisor/chat",
                json={
                    "message": (
                        "Em agosto de 2026 quero fazer uma compra que custa R$ 2.000 à vista, posso fazer?"
                    )
                },
            )
        assert advisor.status_code == 200
        assert advisor.json()["status"] == "not_recommended"
        assert advisor.json()["provider"] == "codex"
        assert advisor.json()["model"] == "modelo-de-teste"
        assert "Não recomendo" in advisor.json()["answer"]
        with monkeypatch.context() as codex_patch:
            codex_patch.setattr(
                "app.api.CodexAdvisorClient.analyze",
                lambda _self, _payload: CodexResult(
                    {
                        "verdict": "favorable",
                        "answer": "Pode comprar.",
                        "evidence": [],
                        "assumptions": [],
                        "confidence": 1,
                    }
                ),
            )
            guarded_advisor = client.post(
                "/api/advisor/chat",
                json={"message": "Posso comprar R$ 2.000 à vista em agosto de 2026?"},
            )
        assert guarded_advisor.json()["status"] == "not_recommended"
        assert guarded_advisor.json()["provider"] == "local"
        assert guarded_advisor.json()["answer"] != "Pode comprar."
        advisor_installments = client.post(
            "/api/advisor/chat",
            json={
                "message": "10x sem juros",
                "history": [
                    {
                        "role": "user",
                        "content": "Quero comprar um videogame que custa R$ 2.000; posso?",
                    },
                    {
                        "role": "assistant",
                        "content": "Essa compra será à vista ou parcelada?",
                    },
                ],
            },
        )
        assert advisor_installments.status_code == 200
        assert advisor_installments.json()["intent"] == "purchase"
        assert advisor_installments.json()["metrics"]["payment"]["installments"] == 10
        assert advisor_installments.json()["metrics"]["card_installments_in_projection"] == 95.7
        assert "Outubro de 2026: cartões R$ 31,90" in advisor_installments.json()["answer"]
        advisor_cuts = client.post(
            "/api/advisor/chat",
            json={"message": "Onde posso economizar?"},
        )
        assert advisor_cuts.status_code == 200
        assert advisor_cuts.json()["intent"] == "cuts"
        assert client.delete(f"/api/transactions/{custom_expense.json()['id']}").status_code == 200

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
        due_soon = FixedDate.today() + timedelta(days=5)
        alert_obligation = client.post(
            "/api/obligations",
            json={
                "name": "Conta próxima",
                "due_date": due_soon.isoformat(),
                "amount": 250,
                "recurrence_months": 0,
                "occurrence_count": 1,
                "category": "general",
            },
        )
        assert alert_obligation.status_code == 201
        obligation_rows = client.get("/api/obligations").json()
        near = next(item for item in obligation_rows if item["name"] == "Conta próxima")
        assert near["days_until_due"] == 5
        assert near["alert_level"] == "urgent"
        alerts = client.get("/api/dashboard?month=2026-08").json()["obligation_alerts"]
        assert any(item["name"] == "Conta próxima" for item in alerts)
        advisor_obligations = client.post(
            "/api/advisor/chat",
            json={"message": "Quais obrigações estão próximas?"},
        ).json()
        assert advisor_obligations["intent"] == "obligations"
        assert "Conta próxima" in advisor_obligations["answer"]
        schedule = {item["month"]: item for item in advisor_obligations["metrics"]["commitment_schedule"]}
        assert schedule["2026-10"]["card_installments"] == 31.9
        assert schedule["2026-12"]["obligations"] == 1000
        assert "Dezembro de 2026: cartões R$ 0,00 + obrigações R$ 1.000,00" in (advisor_obligations["answer"])
        assert client.delete(f"/api/obligations/{obligation.json()['id']}").status_code == 200
        assert client.delete(f"/api/obligations/{alert_obligation.json()['id']}").status_code == 200

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
        assert (
            next(item for item in client.get("/api/users").json() if item["username"] == "kelly")["active"]
            is False
        )

    assert list(Path(os.environ["DATA_DIR"]).glob("documents/*.bin"))
