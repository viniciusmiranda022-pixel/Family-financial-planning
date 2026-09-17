"""WA-01 (`docs/WORK_ORDER_WA_01.md`, issue #73): the WhatsApp webhook
gateway as its own ASGI app, deployed as its own `compose.yaml` service
(`whatsapp-gateway`, profile-gated) reusing the same image/entrypoint as
`app`/`notification-worker`/`cvm-worker` (`command: ["gateway"]`) rather than
a fourth `Dockerfile`.

Deliberately a **separate FastAPI app from `app.main.app`**, not a router
mounted onto it -- this is the one genuinely public-facing surface of the
project (`docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md` §4.2: a WhatsApp webhook
requires a publicly routable HTTPS endpoint, unlike `app`, which stays on
the household's private network). Mounting these two routes here means the
container an operator exposes to the public internet never loads
`app.api`'s ~150 other routes, the household UI, or document storage --
only what this module imports. It still reads/writes the *same* PostgreSQL
database as `app` (`app.db.SessionLocal`, `app.models`), the same way
`notification-worker`/`cvm-worker` already share that database as a second
process against the same schema; it does not read or write any financial
table (`Transaction`, `Obligation`, `Account`, ...) at all.

Every response is intentionally identical regardless of *why* a request was
not fully processed (disabled, bad signature, unauthorized sender,
duplicate, rate-limited) -- Work Order acceptance criterion "número não
autorizado não consulta nem altera dados" and "não vaza PII/existência de
household". The only branch with a different HTTP status is a failed
signature/handshake check, which is a transport-authentication failure, not
a business-authorization outcome.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import whatsapp_gateway as gateway

logger = logging.getLogger(__name__)

gateway_app = FastAPI(title="Family Finance WhatsApp Gateway", docs_url=None, redoc_url=None)

_UNIFORM_OK = {"status": "ok"}


def _ready(settings) -> bool:
    return bool(
        settings.whatsapp_gateway_enabled
        and settings.whatsapp_app_secret
        and settings.whatsapp_verify_token
        and settings.whatsapp_phone_encryption_key
    )


@gateway_app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {"status": "healthy", "gateway_enabled": _ready(settings)}


@gateway_app.get("/webhooks/whatsapp")
def verify_webhook(request: Request):
    """Meta's one-time subscription handshake. Query params use dots
    (`hub.mode`, `hub.verify_token`, `hub.challenge`) so they are read
    directly from `request.query_params` rather than a Pydantic model."""

    settings = get_settings()
    if not _ready(settings):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    params = request.query_params
    if gateway.verify_handshake(
        mode=params.get("hub.mode"),
        verify_token=params.get("hub.verify_token"),
        configured_token=settings.whatsapp_verify_token,
    ):
        return PlainTextResponse(params.get("hub.challenge") or "")
    return JSONResponse(status_code=403, content={"detail": "Forbidden"})


@gateway_app.post("/webhooks/whatsapp")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not _ready(settings):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    raw_body = await request.body()
    signature_header = request.headers.get("x-hub-signature-256")
    if not gateway.verify_signature(raw_body, signature_header, settings.whatsapp_app_secret):
        # Fail closed *before* any JSON parsing or DB access -- an
        # unsigned/forged request never reaches business logic.
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    try:
        payload = await request.json()
    except ValueError:
        # Validly signed (so genuinely from Meta) but not parseable JSON --
        # log for operator visibility, never surface details externally,
        # still ack so Meta does not retry-storm a payload shape we cannot
        # use anyway.
        logger.warning("whatsapp_gateway.malformed_payload")
        return JSONResponse(content=_UNIFORM_OK)

    messages = gateway.normalize_inbound_messages(payload if isinstance(payload, dict) else {})
    for message in messages:
        _process_one_message(db, message, settings=settings)
    if messages:
        db.commit()
    return JSONResponse(content=_UNIFORM_OK)


def _process_one_message(
    db, message: gateway.NormalizedInboundMessage, *, settings
) -> None:
    """`is_duplicate_message` is a cheap pre-check for the common case (a
    redelivery of a message some *earlier, already-committed* request fully
    processed) -- it never by itself guarantees no duplicate is processed,
    because two genuinely concurrent deliveries of the same
    `provider_message_id` can both pass it before either has committed.

    The actual idempotency barrier is `uq_whatsapp_inbound_events_message_id`
    (enforced inside `gateway.record_inbound_event`, which flushes
    immediately). Engineering review on PR #103 (WA-01): wrapping this whole
    per-message body in `db.begin_nested()` means a concurrent-redelivery
    `IntegrityError` rolls back only this message's own writes -- including
    whatever it had just done to the rate-limit bucket -- rather than
    surfacing at the request's later, request-wide `db.commit()` as an
    unhandled 500. The loser's rate-limit accounting for what turns out to
    be a duplicate is exactly what should not have counted, so rolling it
    back together with the failed insert is correct, not just convenient.
    """

    if gateway.is_duplicate_message(db, provider_message_id=message.provider_message_id):
        return
    try:
        with db.begin_nested():
            _process_new_message(db, message, settings=settings)
    except IntegrityError:
        # Lost the race to a concurrent delivery of the exact same
        # provider_message_id that reached record_inbound_event first --
        # that delivery's row is the durable fact; this one is the duplicate.
        pass


def _process_new_message(
    db, message: gateway.NormalizedInboundMessage, *, settings
) -> None:
    try:
        phone_hash = gateway.hash_phone(message.sender_digits)
    except gateway.WhatsAppConfigurationError:
        logger.error("whatsapp_gateway.configuration_error_at_use")
        return

    if not gateway.check_rate_limit(db, bucket_key=phone_hash):
        gateway.record_inbound_event(
            db,
            provider_message_id=message.provider_message_id,
            sender_phone_hash=phone_hash,
            status="rate_limited",
            household_id=None,
            user_id=None,
        )
        return

    authorized = gateway.find_authorized_sender(db, phone_hash=phone_hash)
    if authorized is None:
        gateway.record_inbound_event(
            db,
            provider_message_id=message.provider_message_id,
            sender_phone_hash=phone_hash,
            status="unauthorized",
            household_id=None,
            user_id=None,
        )
        return

    gateway.record_inbound_event(
        db,
        provider_message_id=message.provider_message_id,
        sender_phone_hash=phone_hash,
        status="accepted",
        household_id=authorized.household_id,
        user_id=authorized.user_id,
    )
