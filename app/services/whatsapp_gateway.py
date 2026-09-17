"""WA-01: WhatsApp webhook gateway primitives (`docs/WORK_ORDER_WA_01.md`,
issue #73; architecture per `docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`).

Transport/authentication foundation only -- deliberately contains no
message interpretation, no typed-action dispatch, and never touches
`Transaction`/`Obligation`/any financial model (`No WA-02+ orchestration`
prohibition in the Work Order). Every function here is pure or
near-pure/DB-only; `app/whatsapp_gateway_app.py` is the only caller and owns
the HTTP-level fail-closed decisions (status codes, uniform response body).

Key separation: `settings.whatsapp_phone_encryption_key` is exclusive to
this module, reused for two purposes -- Fernet-encrypting a linked number
for authenticated admin display, and as the HMAC key deriving the
deterministic `phone_hash` an inbound sender is looked up by -- the same
"one key, two uses" idiom as `app.services.mfa.encrypt_secret`/
`hash_recovery_code`. Never `SECRET_KEY`/`FILE_ENCRYPTION_KEY`/
`MFA_ENCRYPTION_KEY`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import WhatsAppAuthorizedNumber, WhatsAppInboundEvent, WhatsAppRateLimitBucket

_MIN_DIGITS = 8
_MAX_DIGITS = 15


class WhatsAppConfigurationError(RuntimeError):
    """Raised when `WHATSAPP_PHONE_ENCRYPTION_KEY` (or another required
    WhatsApp secret) is missing/invalid at the point of use.

    Never caught to fall back to plaintext/unsigned processing -- mirrors
    `app.services.mfa.MfaConfigurationError`. Deliberately not a Pydantic
    `Settings` required field (see `app/config.py` comment on
    `whatsapp_gateway_enabled`): the gateway is opt-in, so a deployment that
    never configured WhatsApp must keep booting normally; this error only
    ever surfaces if something tries to actually use an unconfigured key.
    """


class InvalidPhoneNumberError(ValueError):
    """Raised by `normalize_phone` for a candidate with too few/many digits."""


def _cipher() -> Fernet:
    settings = get_settings()
    try:
        return Fernet(settings.whatsapp_phone_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise WhatsAppConfigurationError(
            "WHATSAPP_PHONE_ENCRYPTION_KEY ausente ou inválida"
        ) from exc


def normalize_phone(raw: str) -> str:
    """Digits-only canonical form shared by admin-entered numbers (which may
    be typed as `"+55 11 99999-8888"`) and Meta's inbound `wa_id` (which
    arrives as `"5511999998888"`, no `+`/formatting) -- both must normalize
    to the exact same string or the same real number would hash to two
    different `phone_hash` values and the allowlist lookup would silently
    never match. Never returns a leading `+`; that is a display concern
    only (`phone_last4`/UI), not part of the hashed/encrypted value."""

    digits = re.sub(r"\D", "", raw or "")
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        raise InvalidPhoneNumberError(
            f"Número de telefone inválido: esperado {_MIN_DIGITS}-{_MAX_DIGITS} dígitos (E.164)"
        )
    return digits


def hash_phone(normalized_digits: str) -> str:
    settings = get_settings()
    if not settings.whatsapp_phone_encryption_key:
        raise WhatsAppConfigurationError("WHATSAPP_PHONE_ENCRYPTION_KEY não configurada")
    return hmac.new(
        settings.whatsapp_phone_encryption_key.encode(), normalized_digits.encode(), hashlib.sha256
    ).hexdigest()


def encrypt_phone(normalized_digits: str) -> str:
    return _cipher().encrypt(normalized_digits.encode()).decode()


def decrypt_phone(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise WhatsAppConfigurationError(
            "Não foi possível descriptografar o número (rotação de chave sem migração?)"
        ) from exc


def phone_last4(normalized_digits: str) -> str:
    return normalized_digits[-4:]


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """`X-Hub-Signature-256: sha256=<hex>` verification -- HMAC-SHA256 over
    the *raw* request body (never a re-serialized/parsed-then-dumped copy,
    which can differ byte-for-byte from what Meta actually signed), timing
    -safe compare, same pattern as `advisor/server.mjs`'s
    `authorized()`/`timingSafeEqual`. Fails closed (`False`) for a missing
    secret, missing header, wrong prefix, or malformed hex -- never raises,
    so the caller's fail-closed branch is a single `if not verify_signature(...)`."""

    if not app_secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    candidate_hex = signature_header[len(prefix) :]
    expected_hex = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    try:
        candidate = bytes.fromhex(candidate_hex)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(expected_hex))


def verify_handshake(*, mode: str | None, verify_token: str | None, configured_token: str) -> bool:
    """`GET` subscription handshake: Meta sends `hub.mode=subscribe` and
    `hub.verify_token`; only `hub.challenge` is echoed back by the caller
    when this returns `True`. Fails closed if `configured_token` is empty
    (gateway not provisioned yet) even if a candidate token happens to be
    an empty string too."""

    if not configured_token or mode != "subscribe":
        return False
    return hmac.compare_digest((verify_token or "").encode(), configured_token.encode())


@dataclass(frozen=True)
class NormalizedInboundMessage:
    provider_message_id: str
    sender_digits: str


def normalize_inbound_messages(payload: dict) -> list[NormalizedInboundMessage]:
    """Extracts every text/message entry from a WhatsApp Cloud API webhook
    body (`entry[].changes[].value.messages[]`) into the minimal shape this
    slice needs. Deliberately ignores everything else in the payload
    (`statuses[]` delivery receipts, contact profile fields, other
    `changes[].field` values, unknown message types) -- WA-01 does not
    process message content, so nothing beyond "who sent it, what is its
    canonical id" is extracted; there is nothing here to normalize an
    *outbound* reply from because WA-01 never sends one automatically (see
    `build_outbound_text_message` for the outbound half of this adapter,
    used only by tests/future orchestration, never called from this
    module). Tolerant of a malformed/unexpected shape: returns whatever
    valid entries it can find rather than raising, since a webhook handler
    must never 500 on an unexpected-but-Meta-signed payload shape."""

    messages: list[NormalizedInboundMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                sender_raw = message.get("from")
                if not message_id or not sender_raw:
                    continue
                try:
                    sender_digits = normalize_phone(str(sender_raw))
                except InvalidPhoneNumberError:
                    continue
                messages.append(
                    NormalizedInboundMessage(
                        provider_message_id=str(message_id), sender_digits=sender_digits
                    )
                )
    return messages


def build_outbound_text_message(*, to_digits: str, body: str) -> dict:
    """Builds a WhatsApp Cloud API-shaped outbound text-message request body
    (`POST /{phone-number-id}/messages`). Pure, never called by
    `app/whatsapp_gateway_app.py` in this slice -- WA-01 only receives; an
    orchestrator that decides *what* to reply (WA-02+) sends this shape
    through a `WhatsAppProvider`. Exists now because the Work Order's scope
    item 3 ("Normalização de mensagens inbound/outbound") asks for the
    adapter's outbound envelope, not just inbound parsing."""

    return {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": "text",
        "text": {"body": body},
    }


@dataclass(frozen=True)
class ProviderSendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


class WhatsAppProvider(Protocol):
    """Adapter boundary a real Meta Cloud API client and the fake test
    provider both implement -- mirrors `advisor/providers/fakeProvider.mjs`'s
    role for the Codex adapter. No implementation of a real
    Meta-credentialed provider ships in this slice (Work Order prohibition:
    "no real Meta credentials/numbers in repository/tests"); a future slice
    that needs to actually send a WhatsApp message implements this
    `Protocol` against the real Cloud API without changing any caller."""

    def send_message(self, payload: dict) -> ProviderSendResult: ...


class FakeWhatsAppProvider:
    """In-memory recorder for tests -- never calls any network. Records
    every payload passed to it so a test can assert exactly what an
    orchestrator attempted to send, same role as `fakeProvider.mjs` for the
    Codex adapter's CI-safe testing."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, payload: dict) -> ProviderSendResult:
        self.sent.append(payload)
        return ProviderSendResult(ok=True, provider_message_id=f"fake-{len(self.sent)}")


def find_authorized_sender(db: Session, *, phone_hash: str) -> WhatsAppAuthorizedNumber | None:
    return db.scalar(
        select(WhatsAppAuthorizedNumber).where(
            WhatsAppAuthorizedNumber.phone_hash == phone_hash,
            WhatsAppAuthorizedNumber.active.is_(True),
        )
    )


def is_duplicate_message(db: Session, *, provider_message_id: str) -> bool:
    return (
        db.scalar(
            select(WhatsAppInboundEvent.id).where(
                WhatsAppInboundEvent.provider_message_id == provider_message_id
            )
        )
        is not None
    )


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite (unit tests) always returns a naive `datetime` from a
    `DateTime(timezone=True)` column even though an aware one was written;
    PostgreSQL round-trips awareness correctly. Same normalization idiom as
    `app.services.capture_worker`'s helper of the same shape -- only
    Python-level arithmetic/comparison needs this."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def check_rate_limit(db: Session, *, bucket_key: str, now: datetime | None = None) -> bool:
    """Atomic fixed-window rate limiter. Returns `True` if the caller may
    proceed, `False` if `bucket_key` is over
    `settings.whatsapp_rate_limit_max_per_window` for the current window.

    Locks the single bucket row for `bucket_key` with `SELECT ... FOR
    UPDATE` before reading-and-incrementing it, inside the caller's
    transaction -- the same row-lock idiom `app/services/assistant_actions.py`
    already uses to serialize concurrent execution of one proposal. On
    SQLite (unit tests) `with_for_update()` is accepted but does not take a
    real row lock -- acceptable here for the same reason the rest of this
    codebase accepts that limitation: correctness under true concurrent
    processes is exercised against real PostgreSQL
    (`tests/test_postgresql_integration.py`), not SQLite.
    """

    settings = get_settings()
    moment = now or datetime.now(UTC)
    bucket = db.scalar(
        select(WhatsAppRateLimitBucket)
        .where(WhatsAppRateLimitBucket.bucket_key == bucket_key)
        .with_for_update()
    )
    window = timedelta(seconds=settings.whatsapp_rate_limit_window_seconds)
    if bucket is None:
        db.add(WhatsAppRateLimitBucket(bucket_key=bucket_key, window_start=moment, count=1))
        db.flush()
        return True
    if moment - _as_aware_utc(bucket.window_start) >= window:
        bucket.window_start = moment
        bucket.count = 1
        return True
    if bucket.count >= settings.whatsapp_rate_limit_max_per_window:
        bucket.count += 1
        return False
    bucket.count += 1
    return True


def record_inbound_event(
    db: Session,
    *,
    provider_message_id: str,
    sender_phone_hash: str,
    status: str,
    household_id: str | None,
    user_id: str | None,
) -> WhatsAppInboundEvent:
    event = WhatsAppInboundEvent(
        provider_message_id=provider_message_id,
        sender_phone_hash=sender_phone_hash,
        status=status,
        household_id=household_id,
        user_id=user_id,
    )
    db.add(event)
    return event
