"""Unit tests for MAIL-01 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`,
issue #68) `app.services.email_delivery`: the SMTP transport adapter and
the process-local test-email throttle. Never touches a real network --
`smtplib.SMTP` is always monkeypatched to a deterministic fake, per the
Work Order's "CI usa fake/in-memory SMTP adapter e nunca tenta autenticar
no Gmail real".
"""

from __future__ import annotations

import smtplib

import pytest

from app.config import Settings
from app.services.email_delivery import (
    EmailErrorCode,
    EmailSendThrottle,
    OutboundEmail,
    SmtpEmailAdapter,
    email_error_message,
)
from app.services.notification_templates import render_test_email


def _settings(**overrides) -> Settings:
    base = dict(
        secret_key="x" * 40,
        file_encryption_key="y" * 44,
        mfa_encryption_key="z" * 44,
        alert_email_enabled=True,
        alert_smtp_host="smtp.gmail.com",
        alert_smtp_port=587,
        alert_smtp_username="sender@example.com",
        alert_smtp_app_password="app-password",
        alert_email_from="sender@example.com",
        alert_email_use_starttls=True,
        alert_email_timeout_seconds=15,
    )
    base.update(overrides)
    return Settings(**base)


class _FakeSmtpSuccess:
    instances: list[_FakeSmtpSuccess] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent_message = None
        _FakeSmtpSuccess.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_message = message


def _make_outbound() -> OutboundEmail:
    return OutboundEmail(to_email="destino@example.com", rendered=render_test_email())


def test_not_configured_never_touches_smtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed when unconfigured")

    monkeypatch.setattr(smtplib, "SMTP", _fail_if_called)
    adapter = SmtpEmailAdapter(_settings(alert_email_enabled=False))
    assert adapter.configured is False
    result = adapter.send(_make_outbound())
    assert result.ok is False
    assert result.error_code == EmailErrorCode.NOT_CONFIGURED


@pytest.mark.parametrize(
    "missing_field",
    ["alert_smtp_host", "alert_smtp_username", "alert_smtp_app_password", "alert_email_from"],
)
def test_configured_requires_every_credential_field(missing_field: str) -> None:
    adapter = SmtpEmailAdapter(_settings(**{missing_field: ""}))
    assert adapter.configured is False


def test_send_success_uses_starttls_login_and_multipart_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSmtpSuccess.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpSuccess)
    adapter = SmtpEmailAdapter(_settings())

    result = adapter.send(_make_outbound())

    assert result.ok is True
    assert result.error_code is None
    assert result.message_id

    fake = _FakeSmtpSuccess.instances[-1]
    assert fake.host == "smtp.gmail.com"
    assert fake.started_tls is True
    assert fake.login_args == ("sender@example.com", "app-password")
    assert fake.sent_message["To"] == "destino@example.com"
    assert fake.sent_message["From"] == "sender@example.com"
    assert fake.sent_message["Message-ID"] == result.message_id
    assert fake.sent_message.is_multipart()
    payloads = {part.get_content_type() for part in fake.sent_message.walk()}
    assert "text/plain" in payloads
    assert "text/html" in payloads


def test_send_without_starttls_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSmtpSuccess.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpSuccess)
    adapter = SmtpEmailAdapter(_settings(alert_email_use_starttls=False))

    result = adapter.send(_make_outbound())

    assert result.ok is True
    assert _FakeSmtpSuccess.instances[-1].started_tls is False


def test_send_maps_auth_failure_without_leaking_password(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSmtpAuthFailure(_FakeSmtpSuccess):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(534, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpAuthFailure)
    adapter = SmtpEmailAdapter(_settings(alert_smtp_app_password="the-real-secret"))

    result = adapter.send(_make_outbound())

    assert result.ok is False
    assert result.error_code == EmailErrorCode.AUTH_FAILED
    assert "the-real-secret" not in email_error_message(result.error_code)
    assert "the-real-secret" not in repr(result)


def test_send_maps_recipient_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSmtpRecipientRefused(_FakeSmtpSuccess):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused({"destino@example.com": (550, b"mailbox unavailable")})

    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpRecipientRefused)
    result = SmtpEmailAdapter(_settings()).send(_make_outbound())

    assert result.ok is False
    assert result.error_code == EmailErrorCode.RECIPIENT_REFUSED


def test_send_maps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSmtpTimeout:
        def __init__(self, host, port, timeout=None):
            raise TimeoutError("timed out")

    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpTimeout)
    result = SmtpEmailAdapter(_settings()).send(_make_outbound())

    assert result.ok is False
    assert result.error_code == EmailErrorCode.TIMEOUT


def test_send_maps_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSmtpConnectionRefused:
        def __init__(self, host, port, timeout=None):
            raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpConnectionRefused)
    result = SmtpEmailAdapter(_settings()).send(_make_outbound())

    assert result.ok is False
    assert result.error_code == EmailErrorCode.CONNECTION_FAILED


def test_send_maps_generic_smtp_exception_to_send_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSmtpGenericFailure(_FakeSmtpSuccess):
        def send_message(self, message):
            raise smtplib.SMTPException("unexpected server response")

    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpGenericFailure)
    result = SmtpEmailAdapter(_settings()).send(_make_outbound())

    assert result.ok is False
    assert result.error_code == EmailErrorCode.SEND_FAILED


def test_email_error_message_never_returns_none_for_unknown_code() -> None:
    assert email_error_message("some_unmapped_code")
    assert email_error_message(EmailErrorCode.NOT_CONFIGURED)


def test_throttle_blocks_second_call_within_window() -> None:
    throttle = EmailSendThrottle()
    assert throttle.allow("household-a", min_interval_seconds=60) is True
    assert throttle.allow("household-a", min_interval_seconds=60) is False


def test_throttle_is_independent_per_household() -> None:
    throttle = EmailSendThrottle()
    assert throttle.allow("household-a", min_interval_seconds=60) is True
    assert throttle.allow("household-b", min_interval_seconds=60) is True


def test_throttle_allows_again_after_interval_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.email_delivery as module

    current_time = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: current_time[0])

    throttle = EmailSendThrottle()
    assert throttle.allow("household-a", min_interval_seconds=60) is True
    assert throttle.allow("household-a", min_interval_seconds=60) is False

    current_time[0] += 61
    assert throttle.allow("household-a", min_interval_seconds=60) is True
