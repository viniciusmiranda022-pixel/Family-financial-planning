"""HTTP-level tests for MAIL-04 (`docs/WORK_ORDER_MAIL_04_UI_SMTP_CONFIG.md`,
issue #110): `GET`/`PUT /api/smtp-config` over the real FastAPI app.
Admin-gating and cross-household isolation live in
`tests/test_role_based_authorization.py`, mirroring the split already used
for MAIL-00 (`tests/test_notification_settings_api.py`). Unit-level
encryption/precedence tests live in `tests/test_smtp_config.py`.
"""

import json
import os
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "smtp-config-api-test-secret-that-is-long-enough-ok")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-smtp-config-api-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, SmtpSenderConfig  # noqa: E402
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


def _setup_household(client, *, username="admin-smtp-config"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família SMTP HTTP",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def _valid_payload(**overrides) -> dict:
    payload = {
        "enabled": True,
        "host": "smtp.gmail.com",
        "port": 587,
        "username": "remetente@gmail.com",
        "from_email": "remetente@gmail.com",
        "use_starttls": True,
        "timeout_seconds": 15,
        "app_password": "minha-app-password-secreta",
    }
    payload.update(overrides)
    return payload


def _last_smtp_config_event(session_factory) -> AuditEvent:
    with session_factory() as db:
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "smtp_config.update").order_by(AuditEvent.created_at.desc())
        )
        assert event is not None, "expected a smtp_config.update audit event"
        return event


def test_get_returns_unconfigured_defaults_before_any_save() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.get("/api/smtp-config")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is False
        assert body["app_password_configured"] is False
        assert body["updated_at"] is None
        assert "app_password" not in body


def test_put_then_get_reflects_saved_config_without_leaking_password() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        saved = client.put("/api/smtp-config", json=_valid_payload())
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["enabled"] is True
        assert body["host"] == "smtp.gmail.com"
        assert body["app_password_configured"] is True
        assert "app_password" not in body
        assert "minha-app-password-secreta" not in saved.text

        fetched = client.get("/api/smtp-config").json()
        assert fetched["host"] == "smtp.gmail.com"
        assert fetched["app_password_configured"] is True
        assert "minha-app-password-secreta" not in json.dumps(fetched)


def test_empty_app_password_on_edit_preserves_existing_secret() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())

        edited = client.put(
            "/api/smtp-config",
            json=_valid_payload(host="smtp2.gmail.com", app_password=None),
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["host"] == "smtp2.gmail.com"
        assert edited.json()["app_password_configured"] is True


def test_remove_app_password_clears_it() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())

        removed = client.put(
            "/api/smtp-config",
            json=_valid_payload(enabled=False, app_password=None, remove_app_password=True),
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["app_password_configured"] is False


def test_enabling_without_required_fields_returns_422_and_does_not_persist() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        rejected = client.put(
            "/api/smtp-config",
            json=_valid_payload(host="", app_password=None),
        )
        assert rejected.status_code == 422, rejected.text
        unchanged = client.get("/api/smtp-config").json()
        assert unchanged["enabled"] is False
        assert unchanged["app_password_configured"] is False


def test_replace_and_remove_password_together_returns_422() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        rejected = client.put(
            "/api/smtp-config",
            json=_valid_payload(remove_app_password=True),
        )
        assert rejected.status_code == 422, rejected.text


def test_saving_twice_reuses_the_singleton_row() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())
        client.put("/api/smtp-config", json=_valid_payload(port=465))

    with session_factory() as db:
        assert db.query(SmtpSenderConfig).count() == 1


def test_put_audits_change_without_leaking_password_in_before_after_or_details() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        response = client.put("/api/smtp-config", json=_valid_payload())
        assert response.status_code == 200, response.text

    event = _last_smtp_config_event(session_factory)
    assert event.entity_type == "smtp_sender_config"
    details = json.loads(event.details)
    assert details == {"app_password_changed": True}
    blob = f"{event.details}{event.before_state}{event.after_state}"
    assert "minha-app-password-secreta" not in blob
    # `before_state`/`after_state` are JSON columns -- already dicts, not
    # JSON-encoded strings like `details`.
    assert "app_password" not in (event.after_state or {})


def test_replacing_an_existing_app_password_audits_changed_true() -> None:
    """Engineering review on the PR for issue #110 (2026-09-21): comparing
    only `app_password_configured` before/after produces `true -> true`
    (unchanged) when an already-configured password is *replaced* by a
    different one, silently under-reporting the change. `app_password_changed`
    must reflect the actual replacement."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())

        response = client.put(
            "/api/smtp-config",
            json=_valid_payload(app_password="uma-app-password-diferente"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["app_password_configured"] is True

    event = _last_smtp_config_event(session_factory)
    details = json.loads(event.details)
    assert details == {"app_password_changed": True}
    blob = f"{event.details}{event.before_state}{event.after_state}"
    assert "uma-app-password-diferente" not in blob


def test_editing_without_touching_password_audits_changed_false() -> None:
    """The counterpart of the test above: editing another field while
    leaving the App Password field empty (preserving the saved secret,
    `save_smtp_sender_config`'s "keep" path) must audit `app_password_changed:
    false` -- nothing about the secret actually changed."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())

        response = client.put(
            "/api/smtp-config",
            json=_valid_payload(host="smtp2.gmail.com", app_password=None),
        )
        assert response.status_code == 200, response.text
        assert response.json()["app_password_configured"] is True

    event = _last_smtp_config_event(session_factory)
    details = json.loads(event.details)
    assert details == {"app_password_changed": False}


def test_removing_app_password_audits_changed_true() -> None:
    client, session_factory = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload())

        response = client.put(
            "/api/smtp-config",
            json=_valid_payload(enabled=False, app_password=None, remove_app_password=True),
        )
        assert response.status_code == 200, response.text

    event = _last_smtp_config_event(session_factory)
    details = json.loads(event.details)
    assert details == {"app_password_changed": True}


def test_first_time_save_without_a_password_audits_changed_false() -> None:
    """A brand new (never-before-saved) draft row with no App Password at
    all -- `enabled=False` skips the "must have a password" validation --
    must not report a password change; nothing was ever configured."""

    client, session_factory = _client()
    with client:
        _setup_household(client)
        response = client.put(
            "/api/smtp-config",
            json=_valid_payload(enabled=False, host="", username="", from_email="", app_password=None),
        )
        assert response.status_code == 200, response.text
        assert response.json()["app_password_configured"] is False

    event = _last_smtp_config_event(session_factory)
    details = json.loads(event.details)
    assert details == {"app_password_changed": False}


def test_test_email_uses_saved_db_config_over_env(monkeypatch) -> None:
    """MAIL-04 acceptance: 'Botão de teste deve usar exatamente a
    configuração efetiva salva.' Saving a DB config must make the shared
    test-email endpoint use it instead of any `ALERT_SMTP_*` env fallback."""

    import app.api as api_module
    from app.services.email_delivery import EmailDeliveryResult

    captured_settings = {}

    def _fake_send(self, message):
        captured_settings["host"] = self.settings.alert_smtp_host
        captured_settings["password"] = self.settings.alert_smtp_app_password
        return EmailDeliveryResult(ok=True, message_id="<test@family-finance.local>")

    monkeypatch.setattr(api_module.SmtpEmailAdapter, "send", _fake_send)

    client, _ = _client()
    with client:
        _setup_household(client)
        client.put("/api/smtp-config", json=_valid_payload(host="db-only-host.example.com"))
        recipient = client.post("/api/notification-recipients", json={"email": "destino@example.com"})
        recipient_id = recipient.json()["id"]

        response = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert response.status_code == 200, response.text

    assert captured_settings["host"] == "db-only-host.example.com"
    assert captured_settings["password"] == "minha-app-password-secreta"
