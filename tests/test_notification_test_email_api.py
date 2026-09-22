"""HTTP-level tests for MAIL-01 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
issue #68) `POST /notification-settings/test-email`. Mirrors the
`_client()`/`_setup_household()` harness in
`tests/test_notification_settings_api.py`; admin-gating and cross-household
isolation for this endpoint live in `tests/test_role_based_authorization.py`
alongside every other role-boundary/isolation test.

`app.api.SmtpEmailAdapter` is always monkeypatched here -- this module never
constructs a real `smtplib.SMTP` connection (see `tests/test_email_delivery.py`
for the adapter's own transport-level tests against a fake SMTP server).
"""

import json
import os
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "notification-test-email-api-test-secret-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-notification-test-email-api-data-{uuid.uuid4().hex}")

import app.api as api_module  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent  # noqa: E402
from app.services.email_delivery import EmailDeliveryResult, EmailErrorCode  # noqa: E402
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


def _setup_household(client, *, username="admin-test-email"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Teste E-mail",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def _create_recipient(client, email: str = "destino@exemplo.com") -> str:
    created = client.post("/api/notification-recipients", json={"email": email})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _last_test_email_event(session_factory) -> AuditEvent:
    with session_factory() as db:
        event = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "notification_settings.test_email")
            .order_by(AuditEvent.created_at.desc())
        )
        assert event is not None, "expected a notification_settings.test_email audit event"
        return event


def test_test_email_missing_recipient_returns_404() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.post("/api/notification-settings/test-email", json={"recipient_id": "missing"})
        assert response.status_code == 404, response.text


def test_test_email_not_configured_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: False))
    client, _ = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client)
        response = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert response.status_code == 503, response.text


def test_test_email_success_returns_ok_and_audits_without_leaking_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: True))
    monkeypatch.setattr(
        api_module.SmtpEmailAdapter,
        "send",
        lambda self, message: EmailDeliveryResult(ok=True, message_id="<test-message@family-finance.local>"),
    )
    client, session_factory = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client, email="destino-secreto@exemplo.com")
        response = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}

    event = _last_test_email_event(session_factory)
    assert event.entity_type == "notification_recipient"
    assert event.entity_id == recipient_id
    details = json.loads(event.details)
    # MAIL-04: `source` records which config `resolve_effective_smtp_settings`
    # resolved (no DB config saved in this test -> "env").
    assert details == {"result": "sent", "error_code": None, "source": "env"}
    # The recipient's e-mail address is personal data; the audit trail must
    # reference it only by id (Work Order "Privacidade e logs").
    assert "destino-secreto@exemplo.com" not in event.details


def test_test_email_adapter_failure_returns_502_and_audits_sanitized_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: True))
    monkeypatch.setattr(
        api_module.SmtpEmailAdapter,
        "send",
        lambda self, message: EmailDeliveryResult(ok=False, error_code=EmailErrorCode.AUTH_FAILED),
    )
    client, session_factory = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client)
        response = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert response.status_code == 502, response.text
        assert "senha" not in response.text.lower()
        assert "password" not in response.text.lower()

    event = _last_test_email_event(session_factory)
    details = json.loads(event.details)
    assert details == {"result": "failed", "error_code": EmailErrorCode.AUTH_FAILED, "source": "env"}


def test_test_email_second_call_within_window_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: True))
    monkeypatch.setattr(
        api_module.SmtpEmailAdapter,
        "send",
        lambda self, message: EmailDeliveryResult(ok=True, message_id="<test-message@family-finance.local>"),
    )
    client, _ = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client)
        first = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert first.status_code == 200, first.text
        second = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert second.status_code == 429, second.text


# UX-01 (docs/WORK_ORDER_UX_01_MAIL_TEST_CHAT_WRITE.md, issue #114):
# reproduced real-world regression -- the throttle used to be keyed only by
# household, so testing recipient A blocked an immediate test of recipient
# B in the same household.
def test_test_email_different_recipients_are_not_cross_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: True))
    monkeypatch.setattr(
        api_module.SmtpEmailAdapter,
        "send",
        lambda self, message: EmailDeliveryResult(ok=True, message_id="<test-message@family-finance.local>"),
    )
    client, _ = _client()
    with client:
        _setup_household(client)
        recipient_a = _create_recipient(client, email="destino-a@exemplo.com")
        recipient_b = _create_recipient(client, email="destino-b@exemplo.com")
        first = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_a})
        assert first.status_code == 200, first.text
        second = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_b})
        assert second.status_code == 200, second.text
        # The same recipient A, right after, is still protected (double-click guard).
        third = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_a})
        assert third.status_code == 429, third.text


def test_test_email_rate_limited_response_reports_remaining_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module.SmtpEmailAdapter, "configured", property(lambda self: True))
    monkeypatch.setattr(
        api_module.SmtpEmailAdapter,
        "send",
        lambda self, message: EmailDeliveryResult(ok=True, message_id="<test-message@family-finance.local>"),
    )
    client, _ = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client)
        first = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert first.status_code == 200, first.text
        second = client.post("/api/notification-settings/test-email", json={"recipient_id": recipient_id})
        assert second.status_code == 429, second.text
        # UI must be able to show the wait time (Work Order "UI deve mostrar
        # tempo restante quando throttled"), and a standard `Retry-After` is
        # also present for any non-browser caller.
        assert "s" in second.json()["detail"]
        assert any(char.isdigit() for char in second.json()["detail"])
        assert second.headers.get("retry-after", "").isdigit()


def test_test_email_rejects_arbitrary_host_username_password_fields() -> None:
    """The Work Order is explicit: this endpoint "não aceita host/username/
    password arbitrários enviados pelo navegador". `NotificationTestEmailRequest`
    only has `recipient_id`, so extra fields are simply ignored by pydantic,
    not honored -- confirm they cannot smuggle a different destination."""

    client, _ = _client()
    with client:
        _setup_household(client)
        recipient_id = _create_recipient(client)
        response = client.post(
            "/api/notification-settings/test-email",
            json={
                "recipient_id": recipient_id,
                "host": "evil.example.com",
                "username": "attacker",
                "password": "hunter2",
            },
        )
        # Unconfigured in this test (no adapter monkeypatch) -> 503, not a
        # 422 that would suggest the extra fields were validated/rejected
        # specifically; the point is they have no effect on the outcome.
        assert response.status_code == 503, response.text
