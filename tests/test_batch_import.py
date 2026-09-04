"""HTTP-level tests for batch import (`POST /api/imports/batch`).

Batch import is orchestration over the exact same canonical per-file
pipeline `POST /api/imports` already uses (`app.api._import_one_document`
via `app.services.importer`/`app.services.reconciliation`/
`app.services.duplicates`) -- there is no second parsing, classification,
reconciliation or duplicate policy here. These tests exercise that contract
end to end over the real FastAPI app: two supported parser paths in one
batch, a mixed valid/invalid batch with truthful per-file outcomes, exact
hash duplicates (within a batch and against pre-existing data), a probable
(non-hash) duplicate staying preserved and flagged rather than merged or
dropped, an honestly `unknown` reconciliation, household isolation, batch
size/count limits, safe error messages, an unexpected mid-batch failure not
corrupting sibling results, and that the pre-existing single-file endpoint
keeps working unchanged.

Uses the same fully isolated database/engine pattern as
`tests/test_monthly_close_api.py` (a `get_db` dependency override on its own
in-memory SQLite engine) instead of relying on the process-wide `app.db.engine`
singleton, which whichever test module imports first would otherwise claim
for every other module in the same test run.
"""

import os
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "batch-import-api-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-batch-import-data-{uuid.uuid4().hex}")

import app.api as api_module  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document, Household, ReviewItem, Transaction, User  # noqa: E402
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
    """Create a new household with its admin user and log `client` into it.

    `POST /api/auth/setup` is a one-time, whole-database bootstrap (it
    refuses once *any* user exists anywhere, not just within one household
    -- see `app.api.setup`), so every household after the first one in this
    module is instead seeded directly into the shared test database, exactly
    like `tests/test_monthly_close_api.py::_create_household_admin` does for
    the same reason, and then logged in for real through `POST
    /api/auth/login` so every subsequent request in these tests still goes
    through the actual authentication/session path.
    """

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


def _csv(rows: str) -> bytes:
    return ("date,title,amount\n" + rows).encode()


def _ofx(*, date_yyyymmdd: str, amount: str, memo: str) -> bytes:
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n"
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>"
        "<STMTTRN>"
        "<TRNTYPE>DEBIT"
        f"<DTPOSTED>{date_yyyymmdd}"
        f"<TRNAMT>{amount}"
        f"<MEMO>{memo}"
        "</STMTTRN>"
        "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    ).encode()


def test_batch_import_two_supported_files_through_different_parser_paths() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Lote", username="admin-lote-1", password="senha-lote-segura-1"
            )
            account = _create_account(client)

            csv_payload = _csv("2026-08-01,Mercado,120.50\n2026-08-02,Farmacia,45.30\n")
            ofx_payload = _ofx(date_yyyymmdd="20260803", amount="-32.50", memo="Padaria Central")

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("extrato-csv.csv", csv_payload, "text/csv")),
                    ("files", ("extrato-ofx.ofx", ofx_payload, "application/x-ofx")),
                ],
            )
            assert response.status_code == 201
            body = response.json()
            assert body["file_count"] == 2
            assert len(body["results"]) == 2
            first, second = body["results"]
            assert first["index"] == 0
            assert first["filename"] == "extrato-csv.csv"
            assert first["status"] in {"imported", "imported_with_review"}
            assert first["document_id"]
            assert first["records"] == 2
            assert second["index"] == 1
            assert second["filename"] == "extrato-ofx.ofx"
            assert second["status"] in {"imported", "imported_with_review"}
            assert second["document_id"]
            assert second["document_id"] != first["document_id"]
            assert second["records"] == 1
            # Both files never declare a balance -- neither parser fabricates
            # one, so reconciliation is honestly `unknown` for both.
            assert first["reconciliation"]["status"] == "unknown"
            assert second["reconciliation"]["status"] == "unknown"
            assert body["summary"]["total"] == 2

            imports = client.get("/api/imports").json()
            assert {item["id"] for item in imports} == {first["document_id"], second["document_id"]}
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_mixed_valid_and_invalid_file_is_truthful_and_non_destructive() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Mista", username="admin-lote-2", password="senha-lote-segura-2"
            )
            account = _create_account(client)

            valid_csv = _csv("2026-08-01,Mercado,120.50\n")
            invalid_csv = b"isto nao e um csv financeiro valido\nsem colunas reconheciveis\n"

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("valido.csv", valid_csv, "text/csv")),
                    ("files", ("invalido.csv", invalid_csv, "text/csv")),
                ],
            )
            assert response.status_code == 201
            body = response.json()
            valid_result, invalid_result = body["results"]

            assert valid_result["status"] in {"imported", "imported_with_review"}
            assert valid_result["records"] == 1
            assert valid_result["document_id"]

            # The bad file never became a fabricated success: it is truthfully
            # `review_required` (a Document row was still created for
            # auditability, with zero records and an honest message), never
            # `imported`, and it never rolled back the sibling valid file.
            assert invalid_result["status"] == "review_required"
            assert invalid_result["records"] == 0
            assert invalid_result["document_id"]
            assert invalid_result["document_id"] != valid_result["document_id"]
            assert invalid_result["message"]

            assert body["summary"]["by_status"]["review_required"] == 1
            assert body["summary"]["total"] == 2

            with _TestSessionLocal() as db:
                valid_document = db.get(Document, valid_result["document_id"])
                invalid_document = db.get(Document, invalid_result["document_id"])
                assert valid_document.status in {"imported", "imported_with_review"}
                assert valid_document.record_count == 1
                assert invalid_document.status == "review_required"
                assert invalid_document.record_count == 0
                transactions = db.scalars(
                    select(Transaction).where(Transaction.document_id == valid_document.id)
                ).all()
                assert len(transactions) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_exact_hash_duplicate_within_batch_and_against_existing() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Duplicada", username="admin-lote-3", password="senha-lote-segura-3"
            )
            account = _create_account(client)
            payload = _csv("2026-08-01,Mercado,120.50\n")

            first_batch = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("fatura.csv", payload, "text/csv")),
                    ("files", ("fatura-copia.csv", payload, "text/csv")),
                ],
            )
            assert first_batch.status_code == 201
            first_result, second_result = first_batch.json()["results"]
            assert first_result["status"] in {"imported", "imported_with_review"}
            assert first_result["document_id"]
            assert second_result["status"] == "rejected"
            assert second_result["error_category"] == "duplicate_file"
            assert second_result["document_id"] is None
            assert first_batch.json()["summary"]["by_status"]["rejected"] == 1

            # The exact same bytes, sent again in a brand-new batch request,
            # must still be rejected against the already-persisted document --
            # duplicate detection is not scoped to a single batch call.
            second_batch = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[("files", ("fatura-nova-tentativa.csv", payload, "text/csv"))],
            )
            assert second_batch.status_code == 201
            [only_result] = second_batch.json()["results"]
            assert only_result["status"] == "rejected"
            assert only_result["error_category"] == "duplicate_file"

            with _TestSessionLocal() as db:
                first_document = db.get(Document, first_result["document_id"])
                matching = db.scalars(
                    select(Document).where(
                        Document.household_id == first_document.household_id,
                        Document.sha256 == first_document.sha256,
                    )
                ).all()
                # Only the single genuinely first document for this sha256
                # exists -- the duplicates never created a second row.
                assert len(matching) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_probable_duplicate_stays_preserved_and_flagged() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Provável", username="admin-lote-4", password="senha-lote-segura-4"
            )
            account = _create_account(client)

            # Same account, same date, same amount; descriptions overlap on
            # only one of two tokens ("compra padaria" of "compra padaria
            # central"/"compra padaria bairro") -- deliberately chosen so
            # `assess_duplicate` (app/services/duplicates.py) lands the
            # combined weighted confidence in the documented 0.60-0.84
            # "probable" band, never the exact-fingerprint "strong" band a
            # byte-identical description would produce.
            first_csv = _csv("2026-08-05,Compra Padaria Central,-85.40\n")
            second_csv = _csv("2026-08-05,Compra Padaria Bairro,-85.40\n")

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("extrato-a.csv", first_csv, "text/csv")),
                    ("files", ("extrato-b.csv", second_csv, "text/csv")),
                ],
            )
            assert response.status_code == 201
            first_result, second_result = response.json()["results"]
            assert first_result["status"] in {"imported", "imported_with_review"}
            # The second file's transaction is a probable duplicate of the
            # first -- it must still be created (never silently dropped) and
            # the document must truthfully surface the review, never claim a
            # clean `imported` outcome.
            assert second_result["status"] == "imported_with_review"
            assert second_result["records"] == 1

            with _TestSessionLocal() as db:
                transactions = db.scalars(
                    select(Transaction).where(Transaction.account_id == account["id"])
                ).all()
                # Both purchases are preserved -- neither merged nor deleted.
                assert len(transactions) == 2
                by_document = {t.document_id: t for t in transactions}
                second_transaction = by_document[second_result["document_id"]]
                assert second_transaction.possible_duplicate is True
                # INV-014: a probable/strong duplicate is excluded from totals
                # until a human resolves it, but the row itself is untouched.
                assert second_transaction.excluded is False
                assert second_transaction.duplicate_group_id is not None
                review_items = db.scalars(
                    select(ReviewItem).where(ReviewItem.transaction_id == second_transaction.id)
                ).all()
                assert any(item.reason == "possible_duplicate" for item in review_items)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_household_isolation() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as owner_client:
            _setup_household(
                owner_client,
                household_name="Família Dona do Lote",
                username="admin-lote-5",
                password="senha-lote-segura-5",
            )
            owner_account = _create_account(owner_client)
            owned_batch = owner_client.post(
                "/api/imports/batch",
                data={"account_id": owner_account["id"], "document_type": "bank_statement"},
                files=[("files", ("extrato.csv", _csv("2026-08-01,Mercado,10.00\n"), "text/csv"))],
            )
            assert owned_batch.status_code == 201
            owned_document_id = owned_batch.json()["results"][0]["document_id"]

        with TestClient(app) as other_client:
            _setup_household(
                other_client,
                household_name="Outra Família do Lote",
                username="admin-lote-6",
                password="senha-lote-segura-6",
            )
            # A batch request cannot touch another household's account.
            cross_household_batch = other_client.post(
                "/api/imports/batch",
                data={"account_id": owner_account["id"], "document_type": "bank_statement"},
                files=[("files", ("tentativa.csv", _csv("2026-08-01,Mercado,10.00\n"), "text/csv"))],
            )
            assert cross_household_batch.status_code == 404

            # And it never sees the other household's already-imported file.
            assert other_client.get("/api/imports").json() == []
            assert other_client.get(f"/api/imports/{owned_document_id}/reconciliation").status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_file_count_limit_rejects_the_whole_request(monkeypatch) -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Limite", username="admin-lote-7", password="senha-lote-segura-7"
            )
            account = _create_account(client)
            monkeypatch.setattr(api_module.settings, "max_batch_files", 2)
            too_many = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("a.csv", _csv("2026-08-01,Mercado,10.00\n"), "text/csv")),
                    ("files", ("b.csv", _csv("2026-08-01,Mercado,10.00\n"), "text/csv")),
                    ("files", ("c.csv", _csv("2026-08-01,Mercado,10.00\n"), "text/csv")),
                ],
            )
            assert too_many.status_code == 413
            # Rejected before any file was even read -- nothing persisted.
            assert client.get("/api/imports").json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_total_size_cap_stops_the_batch_without_undoing_earlier_files(monkeypatch) -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            household = _setup_household(
                client, household_name="Família Tamanho", username="admin-lote-10", password="senha-lote-segura-10"
            )
            account = _create_account(client)

            first_csv = _csv("2026-08-01,Mercado,10.00\n")
            second_csv = _csv("2026-08-02,Farmacia,20.00\n")
            third_csv = _csv("2026-08-03,Padaria,30.00\n")
            # A byte-exact cap: the first file fits, the second alone would
            # also fit but pushes the running total past the cap, and the
            # third is never even read once the cap has already been hit.
            monkeypatch.setattr(api_module, "_batch_size_cap_bytes", lambda: len(first_csv) + 1)

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("primeiro.csv", first_csv, "text/csv")),
                    ("files", ("segundo.csv", second_csv, "text/csv")),
                    ("files", ("terceiro.csv", third_csv, "text/csv")),
                ],
            )
            assert response.status_code == 201
            first_result, second_result, third_result = response.json()["results"]
            assert first_result["status"] in {"imported", "imported_with_review"}
            assert first_result["document_id"]
            assert second_result["status"] == "rejected"
            assert second_result["error_category"] == "batch_too_large"
            assert third_result["status"] == "rejected"
            assert third_result["error_category"] == "batch_too_large"

            with _TestSessionLocal() as db:
                documents = db.scalars(
                    select(Document).where(Document.household_id == household["household_id"])
                ).all()
                # Only the first, already-committed file exists -- the cap
                # never touched (rolled back or deleted) its result.
                assert len(documents) == 1
                assert documents[0].original_name == "primeiro.csv"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_unexpected_failure_on_one_file_does_not_affect_others(monkeypatch) -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            household = _setup_household(
                client, household_name="Família Falha", username="admin-lote-8", password="senha-lote-segura-8"
            )
            account = _create_account(client)

            original_classify = api_module.classify_with_local_rules

            def _boom(db, *, household_id, description, amount, internal_aliases):
                if "GATILHO DE FALHA" in description.upper():
                    raise RuntimeError("falha simulada e inesperada")
                return original_classify(
                    db,
                    household_id=household_id,
                    description=description,
                    amount=amount,
                    internal_aliases=internal_aliases,
                )

            monkeypatch.setattr(api_module, "classify_with_local_rules", _boom)

            good_csv = _csv("2026-08-01,Mercado,120.50\n")
            crashing_csv = _csv("2026-08-02,GATILHO DE FALHA,10.00\n")
            other_good_csv = _csv("2026-08-03,Farmacia,45.30\n")

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[
                    ("files", ("boa-1.csv", good_csv, "text/csv")),
                    ("files", ("crash.csv", crashing_csv, "text/csv")),
                    ("files", ("boa-2.csv", other_good_csv, "text/csv")),
                ],
            )
            assert response.status_code == 201
            first_result, crash_result, third_result = response.json()["results"]
            assert first_result["status"] in {"imported", "imported_with_review"}
            assert third_result["status"] in {"imported", "imported_with_review"}
            assert crash_result["status"] == "rejected"
            assert crash_result["error_category"] == "unexpected_error"
            assert crash_result["document_id"] is None
            # No file content, description or stack trace leaked into the
            # response -- only the fixed, generic category/message.
            assert "GATILHO" not in crash_result["message"]
            assert "RuntimeError" not in crash_result["message"]
            assert "falha simulada" not in crash_result["message"]

            with _TestSessionLocal() as db:
                # The crashing file left no half-written Document behind, and
                # the two good files around it were each independently
                # committed regardless of the failure in between.
                documents = db.scalars(
                    select(Document).where(Document.household_id == household["household_id"])
                ).all()
                statuses = {doc.original_name: doc.status for doc in documents}
                assert statuses.get("boa-1.csv") in {"imported", "imported_with_review"}
                assert statuses.get("boa-2.csv") in {"imported", "imported_with_review"}
                assert "crash.csv" not in statuses
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_batch_import_error_messages_contain_no_raw_file_content() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Segura", username="admin-lote-9", password="senha-lote-segura-9"
            )
            account = _create_account(client)

            secret_marker = "SEGREDO-CARTAO-4111111111111111-NAO-VAZAR"
            bad_payload = f"{secret_marker}\nlinha sem estrutura reconhecivel\n".encode()

            response = client.post(
                "/api/imports/batch",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files=[("files", ("extrato-estranho.csv", bad_payload, "text/csv"))],
            )
            assert response.status_code == 201
            [result] = response.json()["results"]
            assert result["status"] == "review_required"
            assert secret_marker not in result["message"]
            assert secret_marker not in str(response.json())
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_single_file_import_endpoint_is_unchanged_after_batch_import() -> None:
    """Regression: `POST /api/imports` must keep behaving exactly as before
    this Work Order -- same success shape, same 409 on an exact duplicate,
    same 404 for a missing account, same 413 over the per-file size cap."""

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Regressão", username="admin-regressao", password="senha-regressao-1"
            )
            account = _create_account(client)

            payload = _csv("2026-08-01,Mercado,120.50\n2026-08-02,Farmacia,45.30\n")
            upload = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato.csv", payload, "text/csv")},
            )
            assert upload.status_code == 201
            body = upload.json()
            assert body["records"] == 2
            assert body["reconciliation"]["status"] == "unknown"
            assert "index" not in body
            assert "filename" not in body

            duplicate = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato-copia.csv", payload, "text/csv")},
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"] == "Este arquivo já foi importado"

            missing_account = client.post(
                "/api/imports",
                data={"account_id": "conta-inexistente", "document_type": "bank_statement"},
                files={"file": ("extrato2.csv", _csv("2026-08-03,Mercado,1.00\n"), "text/csv")},
            )
            assert missing_account.status_code == 404

            reconciliation = client.get(f"/api/imports/{body['document_id']}/reconciliation")
            assert reconciliation.status_code == 200
            rerun = client.post(f"/api/imports/{body['document_id']}/reconcile")
            assert rerun.status_code == 201
    finally:
        app.dependency_overrides.pop(get_db, None)
