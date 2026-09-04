"""HTTP-level tests for editable merchant classification rules
(docs/WORK_ORDER_EDITABLE_MERCHANT_RULES.md).

Covers what only the real FastAPI app can prove: admin-only authorization
on activate/deactivate/edit, household isolation at the endpoint boundary,
audit-trail actor/before/after/reason coverage, and that the smart-capture
preview endpoint applies an active household rule through the very same
`classify_with_local_rules` path the importer uses -- not a second
classification policy (acceptance criterion 4). The confirmation-count
arithmetic and the invariant-precedence guard are unit-tested in isolation
in `tests/test_classification_learning.py`; evidence here is seeded
directly through that same service function rather than by re-driving the
whole document-import pipeline, exactly as `tests/test_monthly_close_api.py`
seeds a duplicate transaction directly instead of re-parsing a document.

This module gives itself a fully isolated database via a FastAPI
`dependency_overrides` on `get_db` (see that file's docstring for why).
"""

import os
import uuid
from decimal import Decimal

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "classification-rules-api-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-classification-rules-api-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    Category,
    FinancialProfile,
    Household,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services.classification_learning import record_confirmed_correction  # noqa: E402

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


def _create_household_admin(*, household_name: str, username: str, password: str) -> str:
    with _TestSessionLocal() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Admin",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
        return household.id


def _create_category(household_id: str, name: str) -> str:
    with _TestSessionLocal() as db:
        category = Category(household_id=household_id, name=name)
        db.add(category)
        db.commit()
        return category.id


def _seed_pending_rule(
    household_id: str, *, description: str, category_id: str, movement_type: str = "expense"
) -> str:
    """Three distinct confirmed corrections, seeded directly through the
    same service the API's `PATCH /transactions/{id}` correction flow
    calls, exactly as `test_monthly_close_api.py` seeds duplicate rows
    directly instead of re-driving a whole upload."""
    with _TestSessionLocal() as db:
        rule = None
        for index in range(3):
            rule = record_confirmed_correction(
                db,
                household_id=household_id,
                transaction_id=f"seed-{uuid.uuid4().hex}-{index}",
                description=description,
                category_id=category_id,
                movement_type=movement_type,
            )
        db.commit()
        return rule.id


def test_classification_rule_lifecycle_requires_admin() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Regras", username="admin-rules", password="senha-local-segura"
        )
        category_id = _create_category(household_id, "Transporte")
        rule_id = _seed_pending_rule(
            household_id, description="Posto Avenida", category_id=category_id
        )

        with TestClient(app) as admin_client:
            login = admin_client.post(
                "/api/auth/login", json={"username": "admin-rules", "password": "senha-local-segura"}
            )
            assert login.status_code == 200

            member = admin_client.post(
                "/api/users",
                json={
                    "name": "Kelly",
                    "username": "kelly-rules",
                    "password": "senha-kelly-segura",
                    "is_admin": False,
                },
            )
            assert member.status_code == 201

            listed = admin_client.get("/api/classification-rules").json()
            assert any(item["id"] == rule_id and item["status"] == "pending_acceptance" for item in listed)

        with TestClient(app) as member_client:
            login = member_client.post(
                "/api/auth/login", json={"username": "kelly-rules", "password": "senha-kelly-segura"}
            )
            assert login.status_code == 200

            assert member_client.post(f"/api/classification-rules/{rule_id}/activate").status_code == 403
            assert member_client.patch(
                f"/api/classification-rules/{rule_id}",
                json={"category_id": category_id, "reason": "não autorizado"},
            ).status_code == 403
            assert member_client.post(
                f"/api/classification-rules/{rule_id}/deactivate", json={"reason": "não autorizado"}
            ).status_code == 403

        with TestClient(app) as admin_client:
            admin_client.post(
                "/api/auth/login", json={"username": "admin-rules", "password": "senha-local-segura"}
            )
            activated = admin_client.post(f"/api/classification-rules/{rule_id}/activate")
            assert activated.status_code == 200
            assert activated.json()["status"] == "active"
            assert activated.json()["active"] is True

            deactivated = admin_client.post(
                f"/api/classification-rules/{rule_id}/deactivate",
                json={"reason": "Regra não é mais aplicável"},
            )
            assert deactivated.status_code == 200
            assert deactivated.json()["status"] == "inactive"
            assert deactivated.json()["active"] is False

            # An inactive rule cannot be deactivated again.
            assert admin_client.post(
                f"/api/classification-rules/{rule_id}/deactivate", json={"reason": "de novo"}
            ).status_code == 409

            # An inactive rule is still eligible for editing. Changing the
            # category must not let the three confirmations recorded for
            # "Transporte" pass as evidence for "Trabalho".
            work_category_id = _create_category(household_id, "Trabalho")
            edited = admin_client.patch(
                f"/api/classification-rules/{rule_id}",
                json={"category_id": work_category_id, "reason": "Categoria mais precisa"},
            )
            assert edited.status_code == 200
            assert edited.json()["category_id"] == work_category_id
            assert edited.json()["category"] == "Trabalho"
            assert edited.json()["confirmation_count"] == 0
            assert edited.json()["status"] == "observed"
            assert edited.json()["active"] is False

            # Regression guard: zero confirmed corrections for "Trabalho"
            # must not be activatable just because the rule id previously
            # had three confirmations for a different category.
            assert (
                admin_client.post(f"/api/classification-rules/{rule_id}/activate").status_code == 409
            )

        with _TestSessionLocal() as db:
            events = db.scalars(
                select(AuditEvent).where(AuditEvent.entity_id == rule_id)
            ).all()
            events_by_type = {event.event_type: event for event in events}
            assert set(events_by_type) == {
                "classification_rule.activate",
                "classification_rule.edit",
                "classification_rule.deactivate",
            }
            activate_event = events_by_type["classification_rule.activate"]
            assert activate_event.before_state == {"status": "pending_acceptance", "active": False}
            assert activate_event.after_state == {"status": "active", "active": True}
            assert activate_event.reason == "Aceite explícito de regra após três correções consistentes"
            assert activate_event.user_id is not None

            edit_event = events_by_type["classification_rule.edit"]
            assert edit_event.before_state["category_id"] == category_id
            assert edit_event.after_state["category_id"] == work_category_id
            assert edit_event.reason == "Categoria mais precisa"
            assert edit_event.user_id is not None
            # The edit is also a governance/lifecycle reset (evidence no
            # longer supports the new category -- see
            # `edit_classification_rule`'s docstring): the audit record
            # must prove that transition, not just the category change.
            assert edit_event.before_state["status"] == "inactive"
            assert edit_event.before_state["active"] is False
            assert edit_event.before_state["confirmation_count"] == 3
            assert edit_event.after_state["status"] == "observed"
            assert edit_event.after_state["active"] is False
            assert edit_event.after_state["confirmation_count"] == 0

            deactivate_event = events_by_type["classification_rule.deactivate"]
            assert deactivate_event.before_state == {"status": "active", "active": True}
            assert deactivate_event.after_state == {"status": "inactive", "active": False}
            assert deactivate_event.reason == "Regra não é mais aplicável"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_preview_rule_does_not_cross_movement_type() -> None:
    """The canonical classification path (`classify_with_local_rules`, also
    used by `/api/captures/preview`) must not let an active rule learned
    from expense corrections apply to a positive event for the same
    normalized merchant -- a rule's evidence proves its category only for
    the movement type it was confirmed against. Exercised through the real
    import/smart-capture endpoint, not only the service helper, per
    acceptance criterion 4."""
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Captura Cruzada",
            username="admin-capture-cross",
            password="senha-local-segura",
        )
        category_id = _create_category(household_id, "Papelaria")
        rule_id = _seed_pending_rule(
            household_id,
            description="Papelaria Central",
            category_id=category_id,
            movement_type="expense",
        )

        with TestClient(app) as client:
            client.post(
                "/api/auth/login",
                json={"username": "admin-capture-cross", "password": "senha-local-segura"},
            )
            activated = client.post(f"/api/classification-rules/{rule_id}/activate")
            assert activated.status_code == 200

            # A negative event for the same merchant takes the active
            # expense rule's category.
            expense_preview = client.post(
                "/api/captures/preview",
                data={"document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-despesa.csv",
                        b"date,title,amount\n2026-08-01,Papelaria Central,-45.00\n",
                        "text/csv",
                    )
                },
            )
            assert expense_preview.status_code == 201
            expense_item = expense_preview.json()["items"][0]
            assert expense_item["category_name"] == "Papelaria"

            # A positive event for the *same normalized merchant* must NOT
            # inherit the expense rule's category: the rule never proved
            # anything about an income event, and the deterministic
            # classifier resolves an unrecognized positive amount to
            # ordinary income.
            income_preview = client.post(
                "/api/captures/preview",
                data={"document_type": "bank_statement"},
                files={
                    "file": (
                        "extrato-receita.csv",
                        b"date,title,amount\n2026-08-02,Papelaria Central,45.00\n",
                        "text/csv",
                    )
                },
            )
            assert income_preview.status_code == 201
            income_item = income_preview.json()["items"][0]
            assert income_item["category_name"] != "Papelaria"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_preview_text_path_uses_active_income_household_rule() -> None:
    """Engineer review on `85ca6f1` (P0): `_category_for_text()` short-circuited
    to a hardcoded `"Receitas"` category whenever the free-text natural-language
    capture path (`POST /api/captures/preview` with `text=`, not a CSV/document
    upload) detected an income phrase, so it never consulted
    `classify_with_local_rules` at all -- a second classification policy inside
    smart capture, contradicting the normative sentence this PR added to
    `docs/FINANCIAL_RULES.md` ("Todo caminho que classifica automaticamente uma
    descrição ... passa pela mesma função"). `test_smart_capture_preview_*`
    above only exercise the structured bank-statement/CSV path
    (`_parsed_transaction_item`), which already called the canonical function
    for every movement type.

    This proves, through the real natural-language text path specifically:
    1. an active household rule recorded for `movement_type="income"` applies
       to a matching free-text income capture;
    2. an active rule recorded for a *different* movement type that happens to
       share the identical normalized description never cross-applies (the
       movement-type compatibility guard also holds on this path, not only on
       the structured-document path already covered above);
    3. a merchant with no matching active rule for the event's movement type
       still falls back to the deterministic canonical classifier -- not the
       removed hardcoded shortcut.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Captura Texto",
            username="admin-capture-text",
            password="senha-local-segura",
        )
        income_category_id = _create_category(household_id, "Consultoria recorrente")
        expense_category_id = _create_category(household_id, "Serviços diversos")
        capture_text = "Recebi 500 de Consultoria Alfa"

        # Seeded with the identical normalized description as the text that
        # will actually be captured below, but for a different movement type
        # -- proves the movement-type guard, not merely string matching.
        expense_rule_id = _seed_pending_rule(
            household_id,
            description=capture_text,
            category_id=expense_category_id,
            movement_type="expense",
        )
        income_rule_id = _seed_pending_rule(
            household_id,
            description=capture_text,
            category_id=income_category_id,
            movement_type="income",
        )

        with TestClient(app) as client:
            client.post(
                "/api/auth/login",
                json={"username": "admin-capture-text", "password": "senha-local-segura"},
            )
            assert client.post(f"/api/classification-rules/{expense_rule_id}/activate").status_code == 200
            assert client.post(f"/api/classification-rules/{income_rule_id}/activate").status_code == 200

            preview = client.post(
                "/api/captures/preview",
                data={"text": capture_text, "document_type": "auto"},
            )
            assert preview.status_code == 201
            item = preview.json()["items"][0]
            assert item["movement_type"] == "income"
            # Must take the income-typed rule's category -- never the
            # expense-typed rule's, even though both were recorded against
            # the identical normalized description.
            assert item["category_name"] == "Consultoria recorrente"

            # A merchant with no active rule for this movement type still
            # falls back to the deterministic canonical classifier (the
            # shared "Revisar"/low-confidence path ordinary unmatched
            # expense text already goes through), not a hardcoded category.
            unmatched_preview = client.post(
                "/api/captures/preview",
                data={"text": "Recebi 300 de Cliente Desconhecido", "document_type": "auto"},
            )
            assert unmatched_preview.status_code == 201
            unmatched_item = unmatched_preview.json()["items"][0]
            assert unmatched_item["movement_type"] == "income"
            assert unmatched_item["category_name"] != "Consultoria recorrente"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_classification_rule_endpoints_are_household_isolated() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_a = _create_household_admin(
            household_name="Família Regras A", username="admin-rules-a", password="senha-local-segura"
        )
        _create_household_admin(
            household_name="Família Regras B", username="admin-rules-b", password="senha-local-segura"
        )
        category_a = _create_category(household_a, "Transporte")
        rule_id = _seed_pending_rule(household_a, description="Loja X", category_id=category_a)

        with TestClient(app) as client_b:
            login = client_b.post(
                "/api/auth/login", json={"username": "admin-rules-b", "password": "senha-local-segura"}
            )
            assert login.status_code == 200

            listed = client_b.get("/api/classification-rules").json()
            assert all(item["id"] != rule_id for item in listed)

            assert client_b.post(f"/api/classification-rules/{rule_id}/activate").status_code == 404
            assert client_b.patch(
                f"/api/classification-rules/{rule_id}",
                json={"category_id": category_a, "reason": "tentativa cruzada"},
            ).status_code == 404
            assert client_b.post(
                f"/api/classification-rules/{rule_id}/deactivate", json={"reason": "tentativa cruzada"}
            ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_preview_uses_active_household_rule() -> None:
    """`POST /api/captures/preview` must classify through the same active
    household rule the importer uses -- no second classification policy in
    smart capture (acceptance criterion 4). Uses the same
    `date,title,amount` CSV shape as the ordinary `/api/imports` path so
    the assertion exercises the shared classification call, not a quirk of
    OCR/free-text parsing.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Captura", username="admin-capture", password="senha-local-segura"
        )
        category_id = _create_category(household_id, "Assinaturas do escritório")
        rule_id = _seed_pending_rule(
            household_id, description="Papelaria Central", category_id=category_id
        )
        csv_payload = b"date,title,amount\n2026-08-01,Papelaria Central,-45.00\n"

        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "admin-capture", "password": "senha-local-segura"}
            )
            activated = client.post(f"/api/classification-rules/{rule_id}/activate")
            assert activated.status_code == 200

            preview = client.post(
                "/api/captures/preview",
                data={"document_type": "bank_statement"},
                files={"file": ("extrato.csv", csv_payload, "text/csv")},
            )
            assert preview.status_code == 201
            item = preview.json()["items"][0]
            assert item["category_name"] == "Assinaturas do escritório"

            # Deactivating stops the rule from applying to *future* capture
            # previews -- it never rewrites what already happened. A
            # distinct byte payload avoids the unrelated "same file already
            # uploaded" hash guard, not the classification path under test.
            client.post(
                f"/api/classification-rules/{rule_id}/deactivate", json={"reason": "teste"}
            )
            csv_payload_2 = b"date,title,amount\n2026-08-02,Papelaria Central,-45.00\n"
            preview_after = client.post(
                "/api/captures/preview",
                data={"document_type": "bank_statement"},
                files={"file": ("extrato2.csv", csv_payload_2, "text/csv")},
            )
            assert preview_after.status_code == 201
            assert preview_after.json()["items"][0]["category_name"] != "Assinaturas do escritório"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_confirm_preserves_active_income_rule_category() -> None:
    """Engineer review on `df37ee5` (P0): an active household income rule
    was applied by `POST /api/captures/preview` but silently discarded by
    `POST /api/captures/{id}/confirm`, whose income branch hardcoded
    `category_for(db, household_id, "Receitas")` and ignored
    `proposal.category_id`/`proposal.category_name`, unlike the expense
    branch. The persisted `Transaction` is the financial fact; a
    preview-only effect proves nothing about the system's real behavior.

    Proves, round-tripping the exact preview items through confirm exactly
    as the real client does:
    1. an active income-typed rule's category survives confirmation instead
       of being overwritten by the removed "Receitas" shortcut;
    2. an income event with no matching active rule persists with the
       canonical classifier's own conservative "Revisar" category -- an
       unmatched proposal must not be silently promoted to any rule's
       category, nor to "Receitas".
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Confirmação Receita",
            username="admin-confirm-income",
            password="senha-local-segura",
        )
        income_category_id = _create_category(household_id, "Consultoria recorrente")
        rule_id = _seed_pending_rule(
            household_id,
            description="Recebi 500 de Consultoria Alfa",
            category_id=income_category_id,
            movement_type="income",
        )

        with TestClient(app) as client:
            client.post(
                "/api/auth/login",
                json={"username": "admin-confirm-income", "password": "senha-local-segura"},
            )
            account = client.post(
                "/api/accounts",
                json={"name": "Conta corrente", "account_type": "checking", "owner_label": "Família"},
            )
            assert account.status_code == 201
            account_id = account.json()["id"]

            assert client.post(f"/api/classification-rules/{rule_id}/activate").status_code == 200

            preview = client.post(
                "/api/captures/preview",
                data={
                    "text": "Recebi 500 de Consultoria Alfa",
                    "account_id": account_id,
                    "document_type": "auto",
                },
            )
            assert preview.status_code == 201
            capture = preview.json()
            item = capture["items"][0]
            assert item["movement_type"] == "income"
            assert item["category_name"] == "Consultoria recorrente"

            confirmed = client.post(
                f"/api/captures/{capture['id']}/confirm", json={"items": capture["items"]}
            )
            assert confirmed.status_code == 200
            transaction_id = confirmed.json()["result"]["transactions"][0]

            unmatched_preview = client.post(
                "/api/captures/preview",
                data={
                    "text": "Recebi 300 de Cliente Desconhecido",
                    "account_id": account_id,
                    "document_type": "auto",
                },
            )
            assert unmatched_preview.status_code == 201
            unmatched_capture = unmatched_preview.json()
            unmatched_item = unmatched_capture["items"][0]
            assert unmatched_item["movement_type"] == "income"
            assert unmatched_item["category_name"] == "Revisar"

            unmatched_confirmed = client.post(
                f"/api/captures/{unmatched_capture['id']}/confirm",
                json={"items": unmatched_capture["items"]},
            )
            assert unmatched_confirmed.status_code == 200
            unmatched_transaction_id = unmatched_confirmed.json()["result"]["transactions"][0]

        with _TestSessionLocal() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.transaction_type == "income"
            assert transaction.category_id == income_category_id

            unmatched_transaction = db.get(Transaction, unmatched_transaction_id)
            assert unmatched_transaction.transaction_type == "income"
            unmatched_category = db.get(Category, unmatched_transaction.category_id)
            assert unmatched_category.name == "Revisar"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_text_capture_reconciliation_survives_preview_and_confirm() -> None:
    """Engineer review on `df37ee5` (P0): `_category_for_text()` classified
    free text through the canonical classifier but discarded its structural
    `transaction_type`/`excluded`, so `parse_text_capture()` kept whatever
    the separate, narrower `_movement_type()` regex guessed instead. A
    card-bill payment description that regex guesses as ordinary "expense"
    is exactly the invariant-protected "reconciliation" movement (INV-002)
    the canonical classifier itself already recognizes (`PAYMENT_PATTERN`).
    Silently keeping the regex guess would let a free-text capture record a
    card payment as an ordinary expense.

    Proves, through the real text-capture preview + confirm endpoints, that
    the persisted transaction keeps the invariant-protected `transaction_type`
    and stays excluded, and is not counted as an ordinary expense.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        _create_household_admin(
            household_name="Família Conciliação Texto",
            username="admin-text-reconciliation",
            password="senha-local-segura",
        )

        with TestClient(app) as client:
            client.post(
                "/api/auth/login",
                json={"username": "admin-text-reconciliation", "password": "senha-local-segura"},
            )
            account = client.post(
                "/api/accounts",
                json={"name": "Nubank", "account_type": "credit_card", "owner_label": "Família"},
            )
            assert account.status_code == 201
            account_id = account.json()["id"]

            preview = client.post(
                "/api/captures/preview",
                data={
                    "text": "Fatura paga do cartão Nubank, R$ 800",
                    "account_id": account_id,
                    "document_type": "auto",
                },
            )
            assert preview.status_code == 201
            capture = preview.json()
            item = capture["items"][0]
            assert item["movement_type"] == "reconciliation"
            assert item["category_name"] == "Conciliação"

            confirmed = client.post(
                f"/api/captures/{capture['id']}/confirm", json={"items": capture["items"]}
            )
            assert confirmed.status_code == 200
            transaction_id = confirmed.json()["result"]["transactions"][0]

        with _TestSessionLocal() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.transaction_type == "reconciliation"
            assert transaction.excluded is True
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_smart_capture_text_capture_patrimonial_transfer_survives_preview_and_confirm() -> None:
    """Same P0 as above, for the "transfer" structural outcome: free text
    naming an investment product (e.g. "Privilège DI") without any of the
    explicit investir/aplicar/resgatar verbs `_movement_type()` recognizes
    still matches the canonical classifier's own patrimonial-transfer
    pattern. That structural result must not be silently discarded into an
    ordinary expense either.
    """
    app.dependency_overrides[get_db] = _override_get_db
    try:
        household_id = _create_household_admin(
            household_name="Família Transferência Texto",
            username="admin-text-transfer",
            password="senha-local-segura",
        )

        with TestClient(app) as client:
            client.post(
                "/api/auth/login",
                json={"username": "admin-text-transfer", "password": "senha-local-segura"},
            )
            account = client.post(
                "/api/accounts",
                json={"name": "Conta corrente", "account_type": "checking", "owner_label": "Família"},
            )
            assert account.status_code == 201
            account_id = account.json()["id"]

            preview = client.post(
                "/api/captures/preview",
                data={
                    "text": "500 no Privilege DI",
                    "account_id": account_id,
                    "document_type": "auto",
                },
            )
            assert preview.status_code == 201
            capture = preview.json()
            item = capture["items"][0]
            assert item["movement_type"] == "investment"
            assert item["category_name"] == "Transferência patrimonial"

            confirmed = client.post(
                f"/api/captures/{capture['id']}/confirm", json={"items": capture["items"]}
            )
            assert confirmed.status_code == 200
            transaction_id = confirmed.json()["result"]["transactions"][0]

        with _TestSessionLocal() as db:
            transaction = db.get(Transaction, transaction_id)
            assert transaction.transaction_type == "transfer"
            assert transaction.excluded is True
            profile = db.scalar(
                select(FinancialProfile).where(FinancialProfile.household_id == household_id)
            )
            assert profile.investment_balance == Decimal("500.00")
    finally:
        app.dependency_overrides.pop(get_db, None)
