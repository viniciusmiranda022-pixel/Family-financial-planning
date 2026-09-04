"""Regression suite from anonymized/synthesized copies of real supported
document structures (`docs/WORK_ORDER_ANONYMIZED_REAL_DOCUMENT_TESTS.md`).

Every fixture here is either reused from `tests/fixtures/pdf_documents.py`
(already-committed synthetic PDFs modeling Itaú's real layout, per that
module's own docstring) or newly added following the exact same convention:
plain-text templates rendered into a PDF *at test time*, or hand-built
CSV/OFX bytes matching this project's own export shape. No real account,
card, employer, employee, CPF, document or amount appears anywhere below;
real financial documents are gitignored (`*.pdf`/`*.csv`/`*.ofx` are never
committed) and are not needed here since every parser accepts plain bytes.

Unlike `tests/test_pdf_parsers.py` / `tests/test_reconciliation.py` /
`tests/test_duplicates.py` (which call `parse_document_contract` /
`reconcile_parsed_document` / `register_transaction_duplicates` directly),
every test in this module drives the *same* canonical HTTP entrypoints
production uses (`POST /api/imports`, matching `tests/test_batch_import.py`'s
own isolated-engine pattern) so the full parse -> classify -> persist ->
duplicate-detect -> reconcile -> audit pipeline is exercised end to end, and
every assertion below reads a fact the real pipeline actually derived and
persisted -- never a hand-typed stand-in for it. No parser, calculation or
reconciliation logic is duplicated here; only assertions against the
existing, already-shipped contracts.

Coverage against the Work Order's minimum test list:
  1. a representative bank statement that reconciles, and one with
     insufficient evidence that honestly stays `unknown`;
  2. a representative invoice with purchases/refund/payment, proving a
     card-bill payment never becomes a new expense (INV-002) and a refund
     never becomes operating income (INV-016), with card competence
     preserved per INV-017 -- the estorno/reversal case the Work Order lists
     separately is covered here, on the same fixture, rather than as its own
     test: `ITAU_CREDIT_CARD_LEFT` already carries one real refund line, and
     splitting it out would need a second, artificial fixture to exercise
     the exact same `REFUND_PATTERN` code path;
  3. a representative payroll document proving gross - deductions = declared
     net and that the consignado (payroll loan) is captured exactly once,
     never duplicated as a second financial fact;
  4. a cross-format (CSV + OFX) reimport of the same real-world movement,
     proving non-destructive duplicate handling (INV-014/INV-015);
  5. malformed input proving no raw content leaks into the response;
  6. household isolation reaching API + persistence;
  7. a byte-identical PDF reimport, rejected against the already-persisted
     document (the cross-format case above proves the *derived-evidence*
     duplicate path; this one is the exact-hash path, still previously
     untested for the PDF format specifically -- only CSV/OFX had it, in
     `tests/test_batch_import.py`);
  8. a *probable* (not exact-fingerprint) duplicate, below the `strong`
     threshold -- the cross-format case above only reaches the `strong`
     band (identical fingerprint), which resolves to a canonical/supporting
     split; this exercises the different code path INV-014's own text
     names explicitly ("confiança a partir de 0,60"): neither side is
     elected canonical yet, so both are flagged pending review and neither
     is excluded from totals;
  9. a textual PDF carrying none of the three supported issuers' own
     identity markers (distinct from #5's unparseable-bytes case: this one
     *is* a well-formed PDF, and reaches `_detect_pdf_issuer` returning
     `"unknown"` rather than `_pdf_text` itself failing).
"""

import os
import uuid

from cryptography.fernet import Fernet

os.environ.setdefault("SECRET_KEY", "anonymized-docs-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-anonymized-docs-data-{uuid.uuid4().hex}")
# `app.db.engine` is a process-wide singleton created from `DATABASE_URL` the
# first time any test module imports `app.db`/`app.main` in this pytest run.
# This module never uses that shared engine (its own isolated `_test_engine`
# below is what every test actually runs against, exactly like
# `tests/test_batch_import.py`), but importing `app.db` still instantiates
# it -- and without this, an unset `DATABASE_URL` would fall back to the
# docker-compose default (`postgresql://.../db`), permanently poisoning the
# shared engine for every test module collected afterward in the same run
# (`tests/test_api.py` then fails to even create its own tables). Claiming a
# harmless sqlite fallback here, the same way `tests/test_api.py` claims its
# own, keeps this module's addition to the suite from depending on
# alphabetical collection order.
os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-anonymized-docs-{uuid.uuid4().hex}.sqlite")

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Document,
    DuplicateGroup,
    DuplicateGroupMember,
    Household,
    PayrollRecord,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402
from tests.fixtures import pdf_documents as fx  # noqa: E402
from tests.fixtures.payroll_documents import (  # noqa: E402
    payroll_holerite_pdf,
    payroll_holerite_pdf_missing_identification,
)

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


# A structural variant of the already-committed Itaú statement fixture
# (`tests/fixtures/pdf_documents.py::ITAU_BANK_STATEMENT_LINES`) missing its
# "SALDO ANTERIOR" line. Real statements are sometimes clipped by OCR/export
# tooling before the opening balance; the parser must never invent one.
ITAU_BANK_STATEMENT_MISSING_OPENING_BALANCE_LINES = (
    "Itau Unibanco S.A. - www.itau.com.br",
    "05/08/2026 Deposito Salario 3000,00",
    "06/08/2026 Pagamento Boleto -200,00",
    "SALDO FINAL EM 06/08/2026 2800,00",
)


# ---------------------------------------------------------------------------
# 1. Bank statement: representative reconciliation, and honest `unknown`
#    when a required declared field is missing.
# ---------------------------------------------------------------------------


def test_representative_bank_statement_pdf_reconciles_through_canonical_pipeline() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Extrato", username="admin-extrato", password="senha-extrato-1"
            )
            account = _create_account(client)

            response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato.pdf", fx.itau_bank_statement_pdf(), "application/pdf")},
            )
            assert response.status_code == 201
            body = response.json()
            assert body["records"] == 3
            assert body["reconciliation"]["status"] == "reconciled"
            assert body["reconciliation"]["opening_balance"] == "1000.00"
            assert body["reconciliation"]["closing_balance_declared"] == "3750.00"
            assert body["reconciliation"]["difference"] == "0.00"

            with _TestSessionLocal() as db:
                transactions = db.scalars(
                    select(Transaction).where(Transaction.document_id == body["document_id"])
                ).all()
                assert len(transactions) == 3
                amounts = {t.description: t.amount for t in transactions}
                assert amounts["Deposito Salario"] == Decimal("3000.00")
                assert amounts["Pagamento Boleto"] == Decimal("-200.00")
                assert amounts["Compra Debito"] == Decimal("-50.00")
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_bank_statement_pdf_missing_opening_balance_stays_unknown_not_fabricated() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client,
                household_name="Família Extrato Incompleto",
                username="admin-extrato-incompleto",
                password="senha-extrato-2",
            )
            account = _create_account(client)

            response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-incompleto.pdf",
                        fx.render_single_column_pdf(ITAU_BANK_STATEMENT_MISSING_OPENING_BALANCE_LINES),
                        "application/pdf",
                    )
                },
            )
            assert response.status_code == 201
            body = response.json()
            # The two real transaction lines are still parsed and persisted --
            # a missing balance rejects *reconciliation*, never the import.
            assert body["records"] == 2
            assert body["reconciliation"]["status"] == "unknown"
            assert body["reconciliation"]["reason"] == "missing_required_statement_balance"
            assert body["reconciliation"]["opening_balance"] is None
            # Never fabricated: no invented opening balance, no invented
            # reconciled status.
            assert body["reconciliation"]["closing_balance_declared"] == "2800.00"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 2. Credit-card invoice: purchases, refund (estorno) and payment, proving
#    INV-002 (payment is reconciliation, not a new expense), INV-016 (refund
#    offsets expense without becoming income) and INV-017 (card purchases use
#    the statement's competence, original date preserved as lineage).
# ---------------------------------------------------------------------------


def test_representative_credit_card_invoice_payment_and_refund_are_not_operating_facts() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Fatura", username="admin-fatura", password="senha-fatura-1"
            )
            account = _create_account(client, name="Cartão Itaú", account_type="credit_card")

            response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "credit_card"},
                files={"file": ("fatura.pdf", fx.itau_credit_card_pdf(), "application/pdf")},
            )
            assert response.status_code == 201
            body = response.json()
            assert body["records"] == 5
            reconciliation = body["reconciliation"]
            assert reconciliation["status"] == "reconciled"
            assert reconciliation["declared_total"] == "400.00"
            assert reconciliation["purchases_total"] == "430.00"
            assert reconciliation["refunds_total"] == "30.00"
            assert reconciliation["payments_total"] == "500.00"

            with _TestSessionLocal() as db:
                transactions = db.scalars(
                    select(Transaction).where(Transaction.document_id == body["document_id"])
                ).all()
                by_description = {t.description: t for t in transactions}

                # INV-002: the invoice-payment row is reconciliation, not a
                # new expense -- zero operating effect (`excluded=True`).
                payment = by_description["Pagamento recebido"]
                assert payment.transaction_type == "reconciliation"
                assert payment.excluded is True
                assert payment.amount == Decimal("500.00")

                # INV-016: the refund (estorno) offsets the original expense;
                # it is its own category, never plain operating income.
                refund = by_description["Estorno Compra Alfa"]
                assert refund.transaction_type == "refund"
                assert refund.excluded is False
                assert refund.amount == Decimal("30.00")

                # INV-017: the purchase's competence follows the statement's
                # own reference month (EMISSAO: 10/08/2026 -> 2026-08), while
                # the original purchase date is preserved as lineage.
                purchase = by_description["Supermercado Alfa"]
                assert purchase.amount == Decimal("-250.00")
                assert purchase.booked_at == date(2026, 8, 1)
                assert purchase.competence == "2026-08"
                assert purchase.occurred_at == date(2026, 8, 5)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 3. Payroll (holerite): gross - deductions = declared net, and the
#    consignado (payroll loan) is captured exactly once as its own fact --
#    no Transaction/expense is ever created from a payroll document, so it
#    cannot be duplicated by anything downstream (INV-011).
# ---------------------------------------------------------------------------


def test_representative_payroll_reconciles_and_never_duplicates_consignado() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Holerite", username="admin-holerite", password="senha-holerite-1"
            )

            response = client.post(
                "/api/imports",
                data={"document_type": "payroll"},
                files={"file": ("holerite.pdf", payroll_holerite_pdf(), "application/pdf")},
            )
            assert response.status_code == 201
            body = response.json()
            assert body["records"] == 1
            reconciliation = body["reconciliation"]
            assert reconciliation["status"] == "reconciled"
            assert reconciliation["declared_total"] == "3245.67"
            assert reconciliation["reconstructed_total"] == "3245.67"
            assert reconciliation["credits_total"] == "4500.00"
            assert reconciliation["debits_total"] == "1254.33"

            with _TestSessionLocal() as db:
                records = db.scalars(
                    select(PayrollRecord).where(PayrollRecord.document_id == body["document_id"])
                ).all()
                assert len(records) == 1
                record = records[0]
                assert record.gross_amount == Decimal("4500.00")
                assert record.deductions == Decimal("1254.33")
                assert record.net_amount == Decimal("3245.67")
                assert record.payroll_loan == Decimal("150.00")
                assert record.competence == date(2026, 8, 1)

                # The payroll import creates no Transaction at all -- the
                # consignado exists as exactly one fact (`PayrollRecord.
                # payroll_loan`); there is nothing here that could double-
                # count it as a second expense/deduction downstream.
                transactions = db.scalars(
                    select(Transaction).where(Transaction.document_id == body["document_id"])
                ).all()
                assert transactions == []
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_payroll_missing_identification_is_rejected_for_review_not_fabricated() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client,
                household_name="Família Holerite Incompleto",
                username="admin-holerite-incompleto",
                password="senha-holerite-2",
            )

            response = client.post(
                "/api/imports",
                data={"document_type": "payroll"},
                files={
                    "file": (
                        "holerite-incompleto.pdf",
                        payroll_holerite_pdf_missing_identification(),
                        "application/pdf",
                    )
                },
            )
            assert response.status_code == 201
            body = response.json()
            assert body["status"] == "review_required"
            assert body["records"] == 0
            assert body["reconciliation"]["status"] == "unknown"

            with _TestSessionLocal() as db:
                assert (
                    db.scalars(
                        select(PayrollRecord).where(PayrollRecord.document_id == body["document_id"])
                    ).all()
                    == []
                )
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 5. Duplicate / reimport: the same real-world movement, captured through two
#    different formats (a CSV export and an OFX export -- the "planilha e
#    documento histórico" scenario `docs/FINANCIAL_RULES.md` describes),
#    must be preserved in both copies, never merged or deleted, with the
#    later one excluded from totals until a human resolves it (INV-014 /
#    INV-015).
# ---------------------------------------------------------------------------


def test_cross_format_reimport_of_the_same_movement_preserves_both_copies() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Reimportação", username="admin-reimport", password="senha-reimport-1"
            )
            account = _create_account(client)

            csv_response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-agosto.csv",
                        _csv("2026-08-10,Supermercado Center,-125.00\n"),
                        "text/csv",
                    )
                },
            )
            assert csv_response.status_code == 201
            csv_document_id = csv_response.json()["document_id"]

            ofx_response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-agosto.ofx",
                        _ofx(date_yyyymmdd="20260810", amount="-125.00", memo="Supermercado Center"),
                        "application/x-ofx",
                    )
                },
            )
            assert ofx_response.status_code == 201
            ofx_body = ofx_response.json()
            ofx_document_id = ofx_body["document_id"]
            # Not silently dropped: the transaction is created and the
            # document truthfully surfaces the review, never a clean
            # `imported` outcome.
            assert ofx_body["status"] == "imported_with_review"
            assert ofx_body["records"] == 1

            with _TestSessionLocal() as db:
                transactions = db.scalars(
                    select(Transaction).where(Transaction.account_id == account["id"])
                ).all()
                # Both copies preserved -- neither merged nor deleted.
                assert len(transactions) == 2
                by_document = {t.document_id: t for t in transactions}
                csv_transaction = by_document[csv_document_id]
                ofx_transaction = by_document[ofx_document_id]

                # The two formats describe the exact same real-world
                # movement (same account/date/amount/normalized description),
                # so the deterministic fingerprint -- computed identically
                # regardless of source format -- matches exactly.
                assert csv_transaction.fingerprint == ofx_transaction.fingerprint

                # Canonical precedence: the first-imported copy stays
                # canonical; the later one is preserved but excluded from
                # totals, never deleted (INV-014/INV-015).
                assert csv_transaction.canonical_status == "canonical"
                assert csv_transaction.excluded is False
                assert ofx_transaction.canonical_status == "supporting"
                assert ofx_transaction.excluded is True
                assert ofx_transaction.possible_duplicate is True
                assert ofx_transaction.duplicate_group_id is not None
                assert ofx_transaction.duplicate_group_id == csv_transaction.duplicate_group_id

                group = db.get(DuplicateGroup, ofx_transaction.duplicate_group_id)
                assert group is not None
                assert group.confidence == Decimal("1.0000")
                assert group.canonical_transaction_id == csv_transaction.id
                members = db.scalars(
                    select(DuplicateGroupMember).where(DuplicateGroupMember.group_id == group.id)
                ).all()
                assert {member.transaction_id for member in members} == {
                    csv_transaction.id,
                    ofx_transaction.id,
                }
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 6. Malformed input: an unreadable PDF for each canonical document type must
#    be honestly rejected for review, never crash the request, and never let
#    its raw bytes reach the response or a document's own persisted notes.
# ---------------------------------------------------------------------------


def test_malformed_pdf_documents_never_leak_raw_content() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client, household_name="Família Documento Inválido", username="admin-invalido", password="senha-invalido-1"
            )
            account = _create_account(client, account_type="credit_card")

            secret_marker = "SEGREDO-HOLERITE-MATRICULA-99887766-NAO-VAZAR"

            for document_type, data in (
                ("bank_statement", {"account_id": account["id"], "document_type": "bank_statement"}),
                ("credit_card", {"account_id": account["id"], "document_type": "credit_card"}),
                ("payroll", {"document_type": "payroll"}),
            ):
                # Distinct bytes per file: an identical payload would be
                # rejected as an exact-hash duplicate (409) on the second
                # attempt, which is a different, already-tested behavior --
                # not what this test is exercising.
                garbage = f"%PDF-FAKE {secret_marker} isto nao e um pdf valido ({document_type})".encode()
                response = client.post(
                    "/api/imports",
                    data=data,
                    files={"file": (f"documento-{document_type}.pdf", garbage, "application/pdf")},
                )
                assert response.status_code == 201
                body = response.json()
                assert body["status"] == "review_required"
                assert secret_marker not in body["message"]
                assert secret_marker not in str(body)

                with _TestSessionLocal() as db:
                    document = db.get(Document, body["document_id"])
                    assert document.status == "review_required"
                    assert secret_marker not in (document.notes or "")
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 7. Household isolation reaching API + persistence, for the document types
#    this suite adds coverage for (credit card PDF, payroll) -- not just the
#    CSV/batch paths `tests/test_batch_import.py` already covers.
# ---------------------------------------------------------------------------


def test_household_isolation_for_credit_card_and_payroll_imports() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as owner_client:
            owner = _setup_household(
                owner_client, household_name="Família Dona", username="admin-dona", password="senha-dona-1"
            )
            owner_account = _create_account(owner_client, account_type="credit_card")

            card_response = owner_client.post(
                "/api/imports",
                data={"account_id": owner_account["id"], "document_type": "credit_card"},
                files={"file": ("fatura.pdf", fx.itau_credit_card_pdf(), "application/pdf")},
            )
            assert card_response.status_code == 201
            card_document_id = card_response.json()["document_id"]

            payroll_response = owner_client.post(
                "/api/imports",
                data={"document_type": "payroll"},
                files={"file": ("holerite.pdf", payroll_holerite_pdf(), "application/pdf")},
            )
            assert payroll_response.status_code == 201
            payroll_document_id = payroll_response.json()["document_id"]

        with TestClient(app) as other_client:
            _setup_household(
                other_client, household_name="Outra Família", username="admin-outra", password="senha-outra-1"
            )

            # A different household's account_id is simply not found.
            cross_household = other_client.post(
                "/api/imports",
                data={"account_id": owner_account["id"], "document_type": "credit_card"},
                files={"file": ("tentativa.pdf", fx.itau_credit_card_pdf(), "application/pdf")},
            )
            assert cross_household.status_code == 404

            assert other_client.get("/api/imports").json() == []
            assert other_client.get(f"/api/imports/{card_document_id}/reconciliation").status_code == 404
            assert other_client.get(f"/api/imports/{payroll_document_id}/reconciliation").status_code == 404

            with _TestSessionLocal() as db:
                other_household_id = db.scalar(
                    select(User.household_id).where(User.username == "admin-outra")
                )
                assert (
                    db.scalars(
                        select(PayrollRecord).where(PayrollRecord.household_id == other_household_id)
                    ).all()
                    == []
                )
                assert (
                    db.scalars(
                        select(Transaction).where(Transaction.household_id == other_household_id)
                    ).all()
                    == []
                )
                # The owning household's own data is untouched and still
                # reachable by its own id.
                assert db.get(Document, card_document_id).household_id == owner["household_id"]
                assert db.get(Document, payroll_document_id).household_id == owner["household_id"]
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 7. Exact-hash PDF reimport: the same byte-identical duplicate-file check
#    `tests/test_batch_import.py` already proves for CSV/OFX, exercised here
#    for the PDF format specifically -- this pipeline stage hashes the raw
#    upload before any parsing happens, so it is format-agnostic by
#    construction, but nothing before this Work Order proved that for a PDF.
# ---------------------------------------------------------------------------


def test_itau_pdf_exact_hash_duplicate_reimport_is_rejected_and_preserved() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client,
                household_name="Família Extrato Duplicado",
                username="admin-extrato-duplicado",
                password="senha-extrato-duplicado-1",
            )
            account = _create_account(client)
            payload = fx.itau_bank_statement_pdf()

            first = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato.pdf", payload, "application/pdf")},
            )
            assert first.status_code == 201
            first_document_id = first.json()["document_id"]

            second = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato-reenviado.pdf", payload, "application/pdf")},
            )
            assert second.status_code == 409
            assert second.json()["detail"] == "Este arquivo já foi importado"

            with _TestSessionLocal() as db:
                documents = db.scalars(select(Document).where(Document.account_id == account["id"])).all()
                assert [doc.id for doc in documents] == [first_document_id]
                transactions = db.scalars(
                    select(Transaction).where(Transaction.document_id == first_document_id)
                ).all()
                assert len(transactions) == 3
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 8. Probable (not exact-fingerprint) duplicate: below `assess_duplicate`'s
#    `strong` threshold, `_match_and_persist_group` does not yet elect a
#    canonical/supporting side -- both members are flagged
#    `possible_duplicate`, and neither is excluded, pending human review.
#    Same day/amount, partial description overlap, mirroring the CSV-only
#    precedent in `tests/test_batch_import.py::
#    test_batch_import_probable_duplicate_stays_preserved_and_flagged`, but
#    across two different documents of the same bank_statement import (a
#    plausible real scenario: two consecutive monthly exports whose date
#    ranges overlap by a day).
# ---------------------------------------------------------------------------


def test_probable_duplicate_below_strong_threshold_flags_both_sides_without_exclusion() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client,
                household_name="Família Duplicidade Provável",
                username="admin-duplicidade-provavel",
                password="senha-duplicidade-provavel-1",
            )
            account = _create_account(client)

            first_csv = _csv("2026-08-05,Compra Padaria Central,-85.40\n")
            second_csv = _csv("2026-08-05,Compra Padaria Bairro,-85.40\n")

            first = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato-a.csv", first_csv, "text/csv")},
            )
            assert first.status_code == 201
            first_document_id = first.json()["document_id"]

            second = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={"file": ("extrato-b.csv", second_csv, "text/csv")},
            )
            assert second.status_code == 201
            second_body = second.json()
            assert second_body["status"] == "imported_with_review"
            second_document_id = second_body["document_id"]

            with _TestSessionLocal() as db:
                transactions = db.scalars(
                    select(Transaction).where(Transaction.account_id == account["id"])
                ).all()
                # Both purchases preserved -- neither merged nor deleted.
                assert len(transactions) == 2
                by_document = {t.document_id: t for t in transactions}
                first_transaction = by_document[first_document_id]
                second_transaction = by_document[second_document_id]

                assert first_transaction.duplicate_group_id == second_transaction.duplicate_group_id
                group = db.get(DuplicateGroup, second_transaction.duplicate_group_id)
                assert Decimal("0.60") <= Decimal(group.confidence) < Decimal("0.85")

                # Below `strong`: neither side is elected canonical yet --
                # both are flagged pending review, and INV-014 keeps both
                # included in totals only once a human resolves the group
                # (`excluded` stays false for both, unlike the `strong`,
                # exact-fingerprint case above where the supporting copy is
                # immediately excluded).
                assert first_transaction.possible_duplicate is True
                assert second_transaction.possible_duplicate is True
                assert first_transaction.excluded is False
                assert second_transaction.excluded is False
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 9. Unsupported-issuer PDF via the real API: a well-formed textual PDF that
#    parses fine as *text* but carries none of the three supported issuers'
#    own identity markers. `_detect_pdf_issuer` resolves this as "unknown"
#    and `parse_credit_card_pdf`/`parse_bank_statement_pdf` reject it for
#    review -- already proven at the parser level in
#    `tests/test_pdf_parsers.py`, but never through the actual import
#    endpoint, and never checked for a content leak into the response.
# ---------------------------------------------------------------------------


def test_unsupported_issuer_pdf_via_api_is_review_required_without_content_leak() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            _setup_household(
                client,
                household_name="Família Emissor Não Suportado",
                username="admin-emissor-nao-suportado",
                password="senha-emissor-nao-suportado-1",
            )
            account = _create_account(client)

            response = client.post(
                "/api/imports",
                data={"account_id": account["id"], "document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-desconhecido.pdf",
                        fx.unsupported_issuer_bank_statement_pdf(),
                        "application/pdf",
                    )
                },
            )
            assert response.status_code == 201
            body = response.json()
            assert body["status"] == "review_required"
            assert body["records"] == 0
            assert body["reconciliation"]["status"] == "unknown"
            rendered = str(body)
            assert "Banco Delta" not in rendered
            assert "Compra Loja Delta" not in rendered
            assert "120,00" not in rendered
    finally:
        app.dependency_overrides.pop(get_db, None)
