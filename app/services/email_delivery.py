"""SMTP delivery adapter for due-date e-mail alerts (MAIL-01,
`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #68).

This module only knows how to *transport* an already-rendered message
(`app.services.notification_templates`). It never decides who gets
alerted or when -- that is MAIL-02's outbox/scheduler -- and it never
persists anything, and it never decides *where its own credential comes
from*: MAIL-04 (`app.services.smtp_config.resolve_effective_smtp_settings`)
owns the DB-vs-`ALERT_SMTP_*` precedence decision and hands this module an
already-resolved `SmtpSettingsLike` object -- either `app.config.Settings`
itself (env-only, `get_settings()` default) or MAIL-04's
`EffectiveSmtpSettings` (DB-backed, already decrypted). Nothing in this
module reads the database or accepts a credential from a request body or
a response.

Errors are collapsed to a small, fixed set of sanitized codes
(`EmailErrorCode`). The raw `smtplib`/socket exception text is deliberately
never surfaced to callers, logs, or the audit trail: an SMTP server's
response line is untrusted content the delivery attempt does not control,
and the Work Order requires the failure trail stay free of secrets even
though `smtplib` itself does not echo the password back on auth failure.
"""

from __future__ import annotations

import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.services.notification_templates import RenderedEmail


@runtime_checkable
class SmtpSettingsLike(Protocol):
    """The exact surface `SmtpEmailAdapter` reads off a config object --
    `app.config.Settings` and MAIL-04's `EffectiveSmtpSettings` both
    satisfy this structurally, so the adapter (the one and only send
    engine, MAIL-04 Work Order: "Não criar segundo motor de envio") never
    needs to know or care which source resolved the credential."""

    alert_email_enabled: bool
    alert_smtp_host: str
    alert_smtp_port: int
    alert_smtp_username: str
    alert_smtp_app_password: str
    alert_email_from: str
    alert_email_use_starttls: bool
    alert_email_timeout_seconds: int


class EmailErrorCode:
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "smtp_auth_failed"
    CONNECTION_FAILED = "smtp_connection_failed"
    TIMEOUT = "smtp_timeout"
    RECIPIENT_REFUSED = "smtp_recipient_refused"
    SEND_FAILED = "smtp_send_failed"


_ERROR_MESSAGES: dict[str, str] = {
    EmailErrorCode.NOT_CONFIGURED: "Envio de e-mail não configurado neste ambiente",
    EmailErrorCode.AUTH_FAILED: "Falha de autenticação no servidor SMTP",
    EmailErrorCode.CONNECTION_FAILED: "Não foi possível conectar ao servidor SMTP",
    EmailErrorCode.TIMEOUT: "Tempo esgotado ao contatar o servidor SMTP",
    EmailErrorCode.RECIPIENT_REFUSED: "O servidor SMTP recusou o destinatário",
    EmailErrorCode.SEND_FAILED: "Falha ao enviar o e-mail",
}


def email_error_message(error_code: str) -> str:
    return _ERROR_MESSAGES.get(error_code, _ERROR_MESSAGES[EmailErrorCode.SEND_FAILED])


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to_email: str
    rendered: RenderedEmail


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    ok: bool
    message_id: str | None = None
    error_code: str | None = None


class SmtpEmailAdapter:
    """Thin SMTP/STARTTLS transport. No template knowledge, no persistence,
    no retry (MAIL-02 owns retry/backoff over repeated `send` calls)."""

    def __init__(self, settings: SmtpSettingsLike | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(
            s.alert_email_enabled
            and s.alert_smtp_host
            and s.alert_smtp_username
            and s.alert_smtp_app_password
            and s.alert_email_from
        )

    def send(self, message: OutboundEmail) -> EmailDeliveryResult:
        if not self.configured:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.NOT_CONFIGURED)

        s = self.settings
        rendered = message.rendered
        email_message = EmailMessage()
        email_message["Subject"] = rendered.subject
        email_message["From"] = s.alert_email_from
        email_message["To"] = message.to_email
        message_id = make_msgid(domain="family-finance.local")
        email_message["Message-ID"] = message_id
        email_message.set_content(rendered.text_body)
        email_message.add_alternative(rendered.html_body, subtype="html")

        try:
            with smtplib.SMTP(
                s.alert_smtp_host, s.alert_smtp_port, timeout=s.alert_email_timeout_seconds
            ) as client:
                if s.alert_email_use_starttls:
                    client.starttls(context=ssl.create_default_context())
                client.login(s.alert_smtp_username, s.alert_smtp_app_password)
                client.send_message(email_message)
            return EmailDeliveryResult(ok=True, message_id=message_id)
        except smtplib.SMTPAuthenticationError:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.AUTH_FAILED)
        except smtplib.SMTPRecipientsRefused:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.RECIPIENT_REFUSED)
        except TimeoutError:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.TIMEOUT)
        except smtplib.SMTPConnectError:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.CONNECTION_FAILED)
        # `smtplib.SMTPException` subclasses `OSError` (stdlib detail, not
        # obvious from the exception name), so the specific/narrow branches
        # above -- and this one -- must all be checked before the generic
        # `(ConnectionError, OSError)` fallback below, or they would never
        # be reached.
        except smtplib.SMTPException:
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.SEND_FAILED)
        except (ConnectionError, OSError):
            return EmailDeliveryResult(ok=False, error_code=EmailErrorCode.CONNECTION_FAILED)


class EmailSendThrottle:
    """Process-local minimum-interval guard for the administrative
    test-email endpoint ("rate-limited de forma simples" -- Work Order).

    Deliberately in-memory, not a DB column: `scripts/entrypoint.sh` runs a
    single `uvicorn` process with no `--workers`, so a per-process counter
    is an effective control for this deployment, and it avoids adding a
    migration/column for a value nothing else needs to persist or audit.
    Restarting the process resets the throttle; that is an accepted
    trade-off for "simples", not a durability requirement of this slice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sent_at: dict[str, float] = {}

    def allow(self, household_id: str, *, min_interval_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_sent_at.get(household_id)
            if last is not None and (now - last) < min_interval_seconds:
                return False
            self._last_sent_at[household_id] = now
            return True


test_email_throttle = EmailSendThrottle()
