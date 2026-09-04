"""HTTP-level tests for `GET /api/reports/export` (Excel/PDF export).

The export endpoint must reuse `GET /api/reports`'s exact canonical dict --
no second parsing, calculation, reconciliation or deduplication policy in
either the Excel or the PDF renderer. These tests exercise that contract end
to end over the real FastAPI app: parity between the JSON report and both
export formats, a zero-movement period producing a valid (not fabricated)
file, household isolation, safe filenames/content-type, and invalid-format
rejection.

Uses the same fully isolated database/engine pattern as
`tests/test_monthly_close_api.py`/`tests/test_batch_import.py` (a `get_db`
dependency override on its own in-memory SQLite engine).
"""

import os
import uuid
from io import BytesIO

import pymupdf
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "report-export-api-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-report-export-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Household, User  # noqa: E402
from app.security import hash_password  # noqa: E402

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _setup_household(client: TestClient, *, household_name: str, username: str, password: str) -> dict:
    with _TestSessionLocal() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Administrador",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
        household_id = household.id
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"household_id": household_id, "username": username}


def _create_account(client: TestClient, **overrides) -> dict:
    payload = {
        "name": "Conta Corrente",
        "institution": "Itaú",
        "account_type": "checking",
        "owner_label": "Família",
    }
    payload.update(overrides)
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201
    return response.json()


def _add_expense(client: TestClient, *, account_id: str, booked_at: str, amount: str, description: str) -> None:
    response = client.post(
        "/api/transactions",
        json={
            "booked_at": booked_at,
            "description": description,
            "amount": amount,
            "movement_type": "expense",
            "account_id": account_id,
            "category_name": "Mercado",
        },
    )
    assert response.status_code == 201


def _add_income(client: TestClient, *, account_id: str, booked_at: str, amount: str, description: str) -> None:
    response = client.post(
        "/api/transactions",
        json={
            "booked_at": booked_at,
            "description": description,
            "amount": amount,
            "movement_type": "income",
            "account_id": account_id,
        },
    )
    assert response.status_code == 201


def _xlsx_rows(content: bytes, sheet_name: str) -> list[tuple]:
    workbook = load_workbook(BytesIO(content))
    sheet = workbook[sheet_name]
    return list(sheet.iter_rows(values_only=True))


def test_report_export_xlsx_parity_with_canonical_json_report() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export XLSX", username="admin-export-1", password="senha-export-segura-1"
            )
            account = _create_account(client)
            _add_expense(client, account_id=account["id"], booked_at="2026-07-05", amount="120.50", description="Mercado")
            _add_income(client, account_id=account["id"], booked_at="2026-07-10", amount="3000.00", description="Salário")
            _add_expense(client, account_id=account["id"], booked_at="2026-08-03", amount="76.78", description="Farmácia")

            report_json = client.get("/api/reports?end_month=2026-08&months=2").json()

            response = client.get("/api/reports/export?end_month=2026-08&months=2&format=xlsx")
            assert response.status_code == 200
            assert response.headers["content-type"] == (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            assert response.headers["content-disposition"] == (
                'attachment; filename="relatorio_2026-07_a_2026-08.xlsx"'
            )

            workbook = load_workbook(BytesIO(response.content))
            assert workbook.sheetnames == ["Resumo", "Mensal", "Categorias", "Contas"]

            monthly_rows = _xlsx_rows(response.content, "Mensal")
            header, *data_rows = monthly_rows
            assert header[0] == "Mês"
            xlsx_months = {row[0]: row for row in data_rows}
            for month_row in report_json["monthly"]:
                xlsx_row = xlsx_months[month_row["month"]]
                # column order matches _MONTHLY_COLUMNS: month, cash_in,
                # bank_cash_out, card_spending, spending, cash_net,
                # remaining_cap, change_percentage, transaction_count
                assert xlsx_row[1] == month_row["cash_in"]
                assert xlsx_row[4] == month_row["spending"]
                assert xlsx_row[8] == month_row["transaction_count"]

            summary_rows = {row[0]: row[1] for row in _xlsx_rows(response.content, "Resumo")[1:]}
            assert summary_rows["Período inicial"] == report_json["start_month"]
            assert summary_rows["Período final"] == report_json["end_month"]
            assert summary_rows["Gasto total"] == report_json["summary"]["total_spending"]
            assert summary_rows["Entradas totais"] == report_json["summary"]["total_cash_in"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_pdf_is_valid_and_matches_canonical_totals() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export PDF", username="admin-export-2", password="senha-export-segura-2"
            )
            account = _create_account(client)
            _add_expense(client, account_id=account["id"], booked_at="2026-07-05", amount="120.50", description="Mercado")
            _add_income(client, account_id=account["id"], booked_at="2026-07-10", amount="3000.00", description="Salário")

            report_json = client.get("/api/reports?end_month=2026-07&months=1").json()

            response = client.get("/api/reports/export?end_month=2026-07&months=1&format=pdf")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert response.headers["content-disposition"] == (
                'attachment; filename="relatorio_2026-07_a_2026-07.pdf"'
            )

            doc = pymupdf.open(stream=response.content, filetype="pdf")
            assert doc.page_count >= 1
            full_text = "".join(page.get_text() for page in doc)
            # The canonical total, formatted pt-BR, must actually appear in
            # the rendered PDF text -- not recomputed, not approximated.
            total_spending = report_json["summary"]["total_spending"]
            formatted = f"{total_spending:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            assert formatted in full_text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_zero_movement_period_is_valid_not_fabricated() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export Vazia", username="admin-export-3", password="senha-export-segura-3"
            )
            _create_account(client)

            for fmt, content_type in (
                ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("pdf", "application/pdf"),
            ):
                response = client.get(f"/api/reports/export?end_month=2026-01&months=1&format={fmt}")
                assert response.status_code == 200
                assert response.headers["content-type"] == content_type
                assert len(response.content) > 0

            xlsx_response = client.get("/api/reports/export?end_month=2026-01&months=1&format=xlsx")
            monthly_rows = _xlsx_rows(xlsx_response.content, "Mensal")
            header, *data_rows = monthly_rows
            assert len(data_rows) == 1
            assert data_rows[0][0] == "2026-01"
            assert data_rows[0][8] == 0  # transaction_count, honestly zero

            pdf_response = client.get("/api/reports/export?end_month=2026-01&months=1&format=pdf")
            doc = pymupdf.open(stream=pdf_response.content, filetype="pdf")
            full_text = "".join(page.get_text() for page in doc)
            assert "Sem dados no período" in full_text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_household_isolation() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export A", username="admin-export-4a", password="senha-export-segura-4a"
            )
            account_a = _create_account(client)
            _add_expense(
                client, account_id=account_a["id"], booked_at="2026-07-05", amount="999.99", description="SEGREDO FAMILIA A"
            )

            _setup_household(
                client, household_name="Família Export B", username="admin-export-4b", password="senha-export-segura-4b"
            )
            account_b = _create_account(client)
            _add_expense(
                client, account_id=account_b["id"], booked_at="2026-07-05", amount="10.00", description="Farmácia B"
            )

            # Logged in as household B now -- its export must not contain
            # household A's transaction, account or total.
            xlsx_response = client.get("/api/reports/export?end_month=2026-07&months=1&format=xlsx")
            xlsx_bytes = xlsx_response.content
            for sheet_name in ("Mensal", "Categorias", "Contas"):
                for row in _xlsx_rows(xlsx_bytes, sheet_name):
                    for cell in row:
                        assert cell != 999.99
                        assert cell != "SEGREDO FAMILIA A"

            pdf_response = client.get("/api/reports/export?end_month=2026-07&months=1&format=pdf")
            doc = pymupdf.open(stream=pdf_response.content, filetype="pdf")
            full_text = "".join(page.get_text() for page in doc)
            assert "SEGREDO FAMILIA A" not in full_text
            assert "999,99" not in full_text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_excluded_transaction_never_inflates_exported_totals() -> None:
    """An `investment` movement is a transfer into liquidity, not spending
    (INV-002) -- `Transaction.excluded=True`, set by the exact same
    canonical `create_manual_transaction` path both the JSON report and this
    export consume. This proves the export's `spending`/`total_spending`
    match the canonical JSON exactly with an excluded transaction present,
    i.e. the exporter never re-derives its own inclusion/exclusion policy.
    """

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export Excluído", username="admin-export-6", password="senha-export-segura-6"
            )
            account = _create_account(client)
            _add_expense(client, account_id=account["id"], booked_at="2026-07-05", amount="120.50", description="Mercado")
            investment = client.post(
                "/api/transactions",
                json={
                    "booked_at": "2026-07-06",
                    "description": "Aplicação Privilege DI",
                    "amount": "500.00",
                    "movement_type": "investment",
                    "account_id": account["id"],
                },
            )
            assert investment.status_code == 201

            report_json = client.get("/api/reports?end_month=2026-07&months=1").json()
            # The investment must not have been counted as spending in the
            # canonical report itself -- otherwise this test would not be
            # exercising an excluded fact at all.
            assert report_json["summary"]["total_spending"] == 120.5

            xlsx_response = client.get("/api/reports/export?end_month=2026-07&months=1&format=xlsx")
            summary_rows = {row[0]: row[1] for row in _xlsx_rows(xlsx_response.content, "Resumo")[1:]}
            assert summary_rows["Gasto total"] == report_json["summary"]["total_spending"]

            pdf_response = client.get("/api/reports/export?end_month=2026-07&months=1&format=pdf")
            doc = pymupdf.open(stream=pdf_response.content, filetype="pdf")
            full_text = "".join(page.get_text() for page in doc)
            assert "R$ 120,50" in full_text
            assert "R$ 620,50" not in full_text  # would only appear if the investment had been added in
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_rejects_invalid_format() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Export Formato", username="admin-export-5", password="senha-export-segura-5"
            )
            _create_account(client)
            response = client.get("/api/reports/export?end_month=2026-07&months=1&format=docx")
            assert response.status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_report_export_requires_authentication() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/reports/export?end_month=2026-07&months=1&format=xlsx")
            assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)
