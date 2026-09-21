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
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import WhatsAppAuthorizedNumber, WhatsAppInboundEvent, WhatsAppRateLimitBucket

logger = logging.getLogger(__name__)

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


_MAX_INBOUND_TEXT_CHARS = 2000
_MAX_MEDIA_CAPTION_CHARS = 1024
_MAX_MEDIA_ID_CHARS = 200
_MAX_MIME_TYPE_CHARS = 200
_MAX_MEDIA_FILENAME_CHARS = 255

# WA-07 (`docs/WORK_ORDER_WA_07.md`, issue #79): the three WhatsApp Cloud API
# message types this slice reuses the existing capture/OCR/transcription
# pipeline for. Deliberately a closed set matching exactly what
# `app.services.smart_capture.preview_capture` already knows how to route
# (audio -> `transcribe_audio`, image/document -> `extract_document_text`) --
# `location`/`contacts`/`interactive`/`sticker`/... stay unsupported, same as
# every other non-text type WA-01 already tolerated.
_MEDIA_MESSAGE_TYPES = ("image", "audio", "document")


@dataclass(frozen=True)
class NormalizedInboundMessage:
    provider_message_id: str
    sender_digits: str
    # WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75): `None` for anything WA-01
    # already tolerated (a non-`"text"` message type, or a `"text"` entry
    # with no/blank `text.body`) -- the caller must treat `None` as
    # "nothing to interpret", never as an empty-but-valid message, exactly
    # the same "hint absent, never guessed" contract the Tool Layer already
    # uses elsewhere.
    text: str | None = None
    # WA-07: populated instead of `text` for an `image`/`audio`/`document`
    # message. `media_id` is Meta's opaque reference -- never the media
    # bytes themselves, which are fetched separately (and only once, after
    # this message has already won the idempotency claim -- see
    # `app.whatsapp_gateway_app._run_media_reply`) via
    # `WhatsAppProvider.fetch_media`. `media_kind` is one of
    # `_MEDIA_MESSAGE_TYPES`; `media_caption` is the optional free-text
    # caption WhatsApp lets an `image`/`document` message carry (never
    # populated for `audio`, which has no caption field) and is treated
    # exactly like the `text` field of a text message -- a hint, bounded and
    # never trusted beyond that.
    media_id: str | None = None
    media_mime_type: str | None = None
    media_kind: str | None = None
    media_caption: str | None = None
    media_filename: str | None = None


def normalize_inbound_messages(payload: dict) -> list[NormalizedInboundMessage]:
    """Extracts every text/message entry from a WhatsApp Cloud API webhook
    body (`entry[].changes[].value.messages[]`) into the minimal shape this
    slice needs: who sent it, its canonical id, and -- since WA-03 -- its
    text body when the message is a plain `"text"` message. Deliberately
    ignores everything else in the payload (`statuses[]` delivery receipts,
    contact profile fields, other `changes[].field` values, non-text message
    types) -- there is nothing here to normalize an *outbound* reply from
    because this module never sends one automatically on its own (see
    `build_outbound_text_message` for the outbound envelope,
    used by `app.whatsapp_gateway_app`'s orchestration wiring and by tests).
    Tolerant of a malformed/unexpected shape: returns whatever valid entries
    it can find rather than raising, since a webhook handler must never 500
    on an unexpected-but-Meta-signed payload shape.

    A `"text"` message's body is truncated to `_MAX_INBOUND_TEXT_CHARS` --
    the same "hint, never a guess, but also never unbounded" discipline
    `app.services.assistant_orchestrator._coerce_step` already applies to
    plan-step arguments; nothing here changes the free-text interpreter's
    own budget, this only bounds what this module itself passes onward.
    """

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
                text: str | None = None
                message_type = message.get("type")
                media_id: str | None = None
                media_mime_type: str | None = None
                media_kind: str | None = None
                media_caption: str | None = None
                media_filename: str | None = None
                if message_type == "text":
                    body = message.get("text")
                    if isinstance(body, dict):
                        raw_text = body.get("body")
                        if isinstance(raw_text, str) and raw_text.strip():
                            text = raw_text.strip()[:_MAX_INBOUND_TEXT_CHARS]
                elif message_type in _MEDIA_MESSAGE_TYPES:
                    media = message.get(message_type)
                    if isinstance(media, dict):
                        raw_id = media.get("id")
                        if isinstance(raw_id, str) and raw_id.strip():
                            media_id = raw_id.strip()[:_MAX_MEDIA_ID_CHARS]
                            media_kind = message_type
                            raw_mime = media.get("mime_type")
                            if isinstance(raw_mime, str) and raw_mime.strip():
                                media_mime_type = raw_mime.strip()[:_MAX_MIME_TYPE_CHARS]
                            raw_caption = media.get("caption")
                            if isinstance(raw_caption, str) and raw_caption.strip():
                                media_caption = raw_caption.strip()[:_MAX_MEDIA_CAPTION_CHARS]
                            raw_filename = media.get("filename")
                            if isinstance(raw_filename, str) and raw_filename.strip():
                                media_filename = raw_filename.strip()[:_MAX_MEDIA_FILENAME_CHARS]
                # A media entry whose provider payload was missing/malformed
                # (no usable `id`) is treated as unsupported content, exactly
                # like a `"text"` message with no/blank body -- `media_id`
                # stays `None`, so the caller's existing "nothing to
                # interpret" branch handles it without a new code path.
                messages.append(
                    NormalizedInboundMessage(
                        provider_message_id=str(message_id),
                        sender_digits=sender_digits,
                        text=text,
                        media_id=media_id,
                        media_mime_type=media_mime_type,
                        media_kind=media_kind,
                        media_caption=media_caption,
                        media_filename=media_filename,
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


@dataclass(frozen=True)
class MediaFetchResult:
    """WA-07: result of `WhatsAppProvider.fetch_media`. `ok=False` covers
    every failure this module treats as "could not read this media right
    now" -- unconfigured provider, transient network/timeout after
    exhausting retries, an unexpected 4xx/5xx, or a media id Meta no longer
    recognizes (its CDN URLs expire) -- collapsed to one boolean so the
    caller (`app.whatsapp_gateway_app._run_media_reply`) has a single
    fail-closed branch, the same shape `ProviderSendResult` already gives
    outbound sends. `payload`/`mime_type` are only meaningful when `ok`."""

    ok: bool
    payload: bytes | None = None
    mime_type: str | None = None
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

    # WA-07: authenticated download of one inbound media attachment's raw
    # bytes, given the opaque `media_id` Meta's webhook payload carried
    # (never a media *URL* -- Meta's Cloud API requires a first
    # metadata/lookup call per media id, since the CDN URL is short-lived
    # and not itself present in the webhook body). Called at most once per
    # `provider_message_id` -- see `app.whatsapp_gateway_app._run_media_reply`'s
    # docstring on why message-level idempotency (already enforced before
    # this is ever reached) is what makes a redelivered media message never
    # re-fetch/re-process, not a second dedup mechanism here.
    def fetch_media(self, media_id: str) -> MediaFetchResult: ...


class FakeWhatsAppProvider:
    """In-memory recorder for tests -- never calls any network. Records
    every payload passed to it so a test can assert exactly what an
    orchestrator attempted to send, same role as `fakeProvider.mjs` for the
    Codex adapter's CI-safe testing."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        # media_id -> (payload_bytes, mime_type). A test registers a fixture
        # via `register_media` before delivering a webhook that references
        # it; `fetch_media_calls` records every id this provider was asked
        # to fetch (in order, including repeats) so a test can assert a
        # redelivered/duplicate message never fetches twice.
        self._media: dict[str, tuple[bytes, str]] = {}
        self.fetch_media_calls: list[str] = []

    def send_message(self, payload: dict) -> ProviderSendResult:
        self.sent.append(payload)
        return ProviderSendResult(ok=True, provider_message_id=f"fake-{len(self.sent)}")

    def register_media(self, media_id: str, payload: bytes, mime_type: str) -> None:
        self._media[media_id] = (payload, mime_type)

    def fetch_media(self, media_id: str) -> MediaFetchResult:
        self.fetch_media_calls.append(media_id)
        found = self._media.get(media_id)
        if found is None:
            return MediaFetchResult(ok=False, error="fake_media_not_registered")
        payload, mime_type = found
        return MediaFetchResult(ok=True, payload=payload, mime_type=mime_type)


class MetaCloudApiProvider:
    """WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75): the real Meta Cloud API
    `WhatsAppProvider` -- `POST https://graph.facebook.com/{version}/{phone_number_id}/messages`,
    Bearer-authenticated with `settings.whatsapp_access_token`. Same stdlib
    `urllib.request` idiom as `app.services.codex_client.CodexAdvisorClient`
    (no new HTTP dependency for one more outbound JSON call). Never raises:
    a transport/HTTP failure becomes `ProviderSendResult(ok=False, error=...)`,
    exactly like `CodexAdvisorClient._request`'s own fail-closed contract --
    a WhatsApp delivery failure must never bubble up and turn an otherwise
    already-committed financial fact into a 500 for the webhook caller.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.whatsapp_access_token and self.settings.whatsapp_phone_number_id)

    def send_message(self, payload: dict) -> ProviderSendResult:
        if not self.configured:
            return ProviderSendResult(ok=False, error="whatsapp_send_not_configured")
        url = (
            f"{self.settings.whatsapp_api_base_url.rstrip('/')}"
            f"/{self.settings.whatsapp_phone_number_id}/messages"
        )
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.whatsapp_access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def _attempt() -> ProviderSendResult:
            try:
                with urlopen(request, timeout=self.settings.whatsapp_send_timeout_seconds) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8"))
                except (ValueError, OSError):
                    detail = {}
                error = (detail.get("error") or {}).get("message") if isinstance(detail, dict) else None
                return ProviderSendResult(ok=False, error=error or f"http_{exc.code}")
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                return ProviderSendResult(ok=False, error=str(exc.__class__.__name__))
            message_id = None
            if isinstance(result, dict):
                messages = result.get("messages")
                if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                    message_id = messages[0].get("id")
            return ProviderSendResult(ok=True, provider_message_id=message_id)

        return _retry_transient(
            _attempt,
            is_transient=lambda outcome: not outcome.ok and _is_transient_send_error(outcome.error),
            max_attempts=self.settings.whatsapp_send_max_attempts,
            backoff_seconds=self.settings.whatsapp_send_backoff_seconds,
        )

    def fetch_media(self, media_id: str) -> MediaFetchResult:
        """Two-step Meta Cloud API media download: `GET /{media_id}` returns
        JSON metadata including a short-lived `url` + `mime_type` (never the
        bytes directly), then a second authenticated `GET` on that `url`
        returns the actual media bytes. Both calls are wrapped by the same
        bounded retry/backoff as `send_message` -- WA-07 hardening item 2
        ("timeouts/retries explícitos") -- but only for the transient
        failure classes `_is_transient_send_error` recognizes; a 4xx (e.g. an
        expired/invalid `media_id`) fails immediately, never retried, since
        retrying cannot make an invalid id valid and would only spend more
        of this sender's own rate-limit budget on the provider side.
        """

        if not self.configured:
            return MediaFetchResult(ok=False, error="whatsapp_media_fetch_not_configured")
        timeout = self.settings.whatsapp_media_fetch_timeout_seconds
        max_attempts = self.settings.whatsapp_media_fetch_max_attempts
        backoff = self.settings.whatsapp_media_fetch_backoff_seconds
        auth_headers = {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}

        # Internal-only carrier for the metadata step's result: reuses the
        # same `(ok, error)` shape `_retry_transient` already knows how to
        # drive, but keeps the short-lived, token-bearing CDN `url` in its
        # own named field rather than overloading `MediaFetchResult.error`
        # (which a caller might reasonably assume is always either `None`
        # or a safe-to-log failure reason).
        @dataclass(frozen=True)
        class _MetadataResult:
            ok: bool
            url: str | None = None
            mime_type: str | None = None
            error: str | None = None

        def _fetch_metadata() -> _MetadataResult:
            url = f"{self.settings.whatsapp_api_base_url.rstrip('/')}/{media_id}"
            request = Request(url, headers=auth_headers, method="GET")
            try:
                with urlopen(request, timeout=timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                return _MetadataResult(ok=False, error=f"http_{exc.code}")
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                return _MetadataResult(ok=False, error=str(exc.__class__.__name__))
            if not isinstance(body, dict) or not body.get("url"):
                return _MetadataResult(ok=False, error="media_metadata_malformed")
            return _MetadataResult(ok=True, url=body.get("url"), mime_type=body.get("mime_type"))

        metadata = _retry_transient(
            _fetch_metadata,
            is_transient=lambda outcome: not outcome.ok and _is_transient_send_error(outcome.error),
            max_attempts=max_attempts,
            backoff_seconds=backoff,
        )
        if not metadata.ok:
            return MediaFetchResult(ok=False, error=metadata.error)
        media_url = metadata.url

        def _fetch_bytes() -> MediaFetchResult:
            request = Request(media_url, headers=auth_headers, method="GET")
            try:
                with urlopen(request, timeout=timeout) as response:
                    data = response.read(self.settings.whatsapp_media_max_mb * 1024 * 1024 + 1)
            except HTTPError as exc:
                return MediaFetchResult(ok=False, error=f"http_{exc.code}")
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                return MediaFetchResult(ok=False, error=str(exc.__class__.__name__))
            return MediaFetchResult(ok=True, payload=data, mime_type=metadata.mime_type)

        return _retry_transient(
            _fetch_bytes,
            is_transient=lambda outcome: not outcome.ok and _is_transient_send_error(outcome.error),
            max_attempts=max_attempts,
            backoff_seconds=backoff,
        )


def _is_transient_send_error(error: str | None) -> bool:
    """Classifies an outbound-send/media-fetch failure as worth a bounded
    retry. `URLError`/`TimeoutError`/`OSError` (network-level: DNS, refused
    connection, timeout) and a `5xx` HTTP status are transient -- the same
    request might succeed moments later. A `4xx` (bad token, malformed
    payload, expired/unknown media id, rate-limited -- `429` is
    deliberately excluded here too: retrying immediately into an active
    rate limit would make it worse, and this module has no backoff long
    enough to meaningfully wait one out) is not retried: the exact same
    request would fail again for the exact same reason, and retrying only
    spends more of the outbound rate-limit budget for no chance of success.
    """

    if not error:
        return False
    if error in {"URLError", "TimeoutError", "OSError"}:
        return True
    if error.startswith("http_"):
        code = error[len("http_") :]
        return code.isdigit() and code.startswith("5")
    return False


def _retry_transient(attempt, *, is_transient, max_attempts: int, backoff_seconds: float):
    """Bounded linear-backoff retry loop shared by `send_message` and
    `fetch_media` -- WA-07 hardening item 2. `max_attempts` includes the
    first (non-retry) attempt, so `max_attempts=1` disables retrying
    entirely (an operator can always dial this down to 1 without special-
    casing anything). Sleeps between attempts only, never after the last
    one. `time.sleep` is a deliberate, short, synchronous block: this
    module has no async call path (the webhook handler that ultimately
    calls this is itself a synchronous FastAPI route body), and the total
    worst case (`max_attempts - 1` backoff steps, each a few hundred
    milliseconds by default) stays comfortably inside Meta's own webhook
    delivery timeout.
    """

    outcome = attempt()
    attempts = 1
    while attempts < max(1, max_attempts) and is_transient(outcome):
        time.sleep(max(0.0, backoff_seconds) * attempts)
        outcome = attempt()
        attempts += 1
    return outcome


def resolve_provider() -> WhatsAppProvider | None:
    """The one place `app.whatsapp_gateway_app` asks "which provider do I
    send replies through" -- returns `None` when outbound credentials are
    not configured (an operator may enable *receiving* -- `whatsapp_gateway_enabled`
    -- before provisioning a send-capable access token), so a caller never
    needs to know `MetaCloudApiProvider` exists to fail closed correctly.
    Reads settings itself, same "no caller-supplied settings" idiom as
    every other function in this module (`check_rate_limit`, `_cipher`, ...)."""

    provider = MetaCloudApiProvider()
    return provider if provider.configured else None


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

    `FOR UPDATE` cannot lock a row that does not exist yet, so a bucket's
    first-ever hit (no row for `bucket_key`) is its own race: two concurrent
    first messages from the same new sender can both see no row and both
    attempt the INSERT. Engineering review on PR #103 (WA-01): resolved with
    the same `begin_nested()`/`IntegrityError` idiom as
    `app.services.notification_scheduler.discover_due_deliveries` and
    `app.services.privilege_valuation.ingest_latest_quote` -- the loser
    re-selects `FOR UPDATE` (now that the winner's row exists) and falls
    through to the normal locked-row accounting below instead of
    short-circuiting `True`, which would silently skip this hit's own
    accounting against the bucket the winner just created.
    """

    settings = get_settings()
    moment = now or datetime.now(UTC)
    window = timedelta(seconds=settings.whatsapp_rate_limit_window_seconds)
    bucket = db.scalar(
        select(WhatsAppRateLimitBucket)
        .where(WhatsAppRateLimitBucket.bucket_key == bucket_key)
        .with_for_update()
    )
    if bucket is None:
        try:
            with db.begin_nested():
                db.add(WhatsAppRateLimitBucket(bucket_key=bucket_key, window_start=moment, count=1))
                db.flush()
            return True
        except IntegrityError:
            # Lost the race to a concurrent first-hit for the same
            # bucket_key -- its row is now committed-within-the-transaction
            # and lockable; join the normal accounting path below instead of
            # treating this hit as unaccounted.
            bucket = db.scalar(
                select(WhatsAppRateLimitBucket)
                .where(WhatsAppRateLimitBucket.bucket_key == bucket_key)
                .with_for_update()
            )
            if bucket is None:  # pragma: no cover - defensive, should be unreachable
                raise RuntimeError(
                    "whatsapp_gateway: rate limit bucket missing after IntegrityError "
                    "on its own unique constraint"
                ) from None
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
    trace_id: str | None = None,
) -> WhatsAppInboundEvent:
    """Inserts the one durable idempotency record for this
    `provider_message_id` (`uq_whatsapp_inbound_events_message_id`).
    Flushes immediately -- rather than leaving the INSERT pending for the
    caller's own flush/commit -- so a concurrent redelivery's `IntegrityError`
    surfaces here, at the caller's own claim step
    (`app.whatsapp_gateway_app._process_new_message`), instead of at some
    later, unrelated `db.commit()` where it could not be told apart from any
    other write in that request.
    """

    event = WhatsAppInboundEvent(
        provider_message_id=provider_message_id,
        sender_phone_hash=sender_phone_hash,
        status=status,
        household_id=household_id,
        user_id=user_id,
        trace_id=trace_id,
    )
    db.add(event)
    db.flush()
    return event


def finalize_inbound_event(db: Session, event: WhatsAppInboundEvent, *, status: str, trace_id: str) -> None:
    """WA-03: the one place that updates an already-claimed
    `WhatsAppInboundEvent` row with its Tool Layer outcome, after
    `app.services.assistant_orchestrator.plan_and_execute` (and, in turn,
    any reply send) has run. Commits on its own -- this update is
    intentionally independent of whatever transaction state
    `plan_and_execute`'s own internal commits left the session in, the same
    "each unit of work commits itself" idiom that module already uses."""

    event.status = status
    event.trace_id = trace_id
    db.commit()
