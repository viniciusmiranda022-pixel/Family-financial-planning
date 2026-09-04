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
from app.models import AuditEvent, Category, Household, User  # noqa: E402
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

            # Only an active rule can be deactivated.
            work_category_id = _create_category(household_id, "Trabalho")
            edited = admin_client.patch(
                f"/api/classification-rules/{rule_id}",
                json={"category_id": work_category_id, "reason": "Categoria mais precisa"},
            )
            assert edited.status_code == 200
            assert edited.json()["category_id"] == work_category_id
            assert edited.json()["category"] == "Trabalho"

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
            edit_event = events_by_type["classification_rule.edit"]
            assert edit_event.before_state["category_id"] == category_id
            assert edit_event.after_state["category_id"] == work_category_id
            assert edit_event.reason == "Categoria mais precisa"
            assert edit_event.user_id is not None

            deactivate_event = events_by_type["classification_rule.deactivate"]
            assert deactivate_event.before_state == {"status": "active", "active": True}
            assert deactivate_event.after_state == {"status": "inactive", "active": False}
            assert deactivate_event.reason == "Regra não é mais aplicável"
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
