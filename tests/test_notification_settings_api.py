"""HTTP-level tests for MAIL-00 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
issue #67) that do not need a second household -- those (admin-gating,
cross-household isolation) live in `tests/test_role_based_authorization.py`
alongside every other role-boundary/isolation test. This module exercises
validation, the settings/recipient CRUD contract, and audit trail over the
real FastAPI app for a single household, mirroring the HTTP-level section
of `tests/test_investments_slice6.py`.
"""

import os
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "notification-settings-api-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-notification-settings-api-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), session_factory


def _setup_household(client, *, username="admin-notification-settings"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Alertas HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def test_get_notification_settings_returns_defaults_and_empty_recipients() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.get("/api/notification-settings")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is False
        assert body["send_time_local"] == "08:00"
        assert body["timezone"] == "America/Sao_Paulo"
        assert body["recipients"] == []


def test_update_settings_then_get_reflects_new_values() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        updated = client.put(
            "/api/notification-settings",
            json={"enabled": True, "send_time_local": "07:15", "timezone": "America/Manaus"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {"enabled": True, "send_time_local": "07:15", "timezone": "America/Manaus"}

        fetched = client.get("/api/notification-settings").json()
        assert fetched["enabled"] is True
        assert fetched["send_time_local"] == "07:15"
        assert fetched["timezone"] == "America/Manaus"


def test_update_settings_with_invalid_time_returns_422_and_does_not_persist() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        rejected = client.put(
            "/api/notification-settings",
            json={"enabled": True, "send_time_local": "9:00am", "timezone": "America/Sao_Paulo"},
        )
        assert rejected.status_code == 422, rejected.text
        unchanged = client.get("/api/notification-settings").json()
        assert unchanged["send_time_local"] == "08:00"


def test_update_settings_with_unsupported_timezone_returns_422() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        rejected = client.put(
            "/api/notification-settings",
            json={"enabled": True, "send_time_local": "08:00", "timezone": "Mars/Olympus_Mons"},
        )
        assert rejected.status_code == 422, rejected.text


def test_create_recipient_then_list_via_settings() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/notification-recipients",
            json={"email": "vinicius@exemplo.com", "notify_d1": True, "notify_d0": False},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["email"] == "vinicius@exemplo.com"
        assert body["active"] is True
        assert body["notify_d1"] is True
        assert body["notify_d0"] is False

        settings = client.get("/api/notification-settings").json()
        assert [item["id"] for item in settings["recipients"]] == [body["id"]]


def test_create_recipient_with_invalid_email_returns_422() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        rejected = client.post("/api/notification-recipients", json={"email": "not-an-email"})
        assert rejected.status_code == 422, rejected.text
        assert client.get("/api/notification-settings").json()["recipients"] == []


def test_create_duplicate_recipient_email_returns_422_case_insensitive() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        first = client.post("/api/notification-recipients", json={"email": "kelly@exemplo.com"})
        assert first.status_code == 201, first.text
        duplicate = client.post("/api/notification-recipients", json={"email": "Kelly@Exemplo.com"})
        assert duplicate.status_code == 422, duplicate.text
        assert len(client.get("/api/notification-settings").json()["recipients"]) == 1


def test_patch_recipient_updates_only_given_fields() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post("/api/notification-recipients", json={"email": "kelly@exemplo.com"}).json()

        patched = client.patch(f"/api/notification-recipients/{created['id']}", json={"notify_d0": False})
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["notify_d0"] is False
        assert body["notify_d1"] is True
        assert body["email"] == "kelly@exemplo.com"
        assert body["active"] is True


def test_patch_unknown_recipient_returns_404() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.patch("/api/notification-recipients/does-not-exist", json={"active": False})
        assert response.status_code == 404, response.text


def test_delete_recipient_removes_it() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post("/api/notification-recipients", json={"email": "kelly@exemplo.com"}).json()
        deleted = client.delete(f"/api/notification-recipients/{created['id']}")
        assert deleted.status_code == 200, deleted.text
        assert client.get("/api/notification-settings").json()["recipients"] == []


def test_delete_unknown_recipient_returns_404() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.delete("/api/notification-recipients/does-not-exist")
        assert response.status_code == 404, response.text


def test_notification_mutations_are_audited_without_leaking_email_as_a_secret() -> None:
    """Audit events must exist for traceability, but this slice has no
    secret to leak in the first place (no SMTP credential exists yet --
    that is MAIL-01); this only asserts the audit trail records the
    expected event types and entity linkage."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post("/api/notification-recipients", json={"email": "kelly@exemplo.com"}).json()
        client.patch(f"/api/notification-recipients/{created['id']}", json={"active": False})
        client.delete(f"/api/notification-recipients/{created['id']}")
        client.put(
            "/api/notification-settings",
            json={"enabled": True, "send_time_local": "08:00", "timezone": "America/Sao_Paulo"},
        )

    with session_factory() as db:
        events = list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.entity_type.in_(["notification_recipient", "notification_settings"]))
            )
        )
        event_types = {event.event_type for event in events}
        assert event_types == {
            "notification_recipient.create",
            "notification_recipient.update",
            "notification_recipient.delete",
            "notification_settings.update",
        }
        assert all(event.entity_id for event in events)
