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
process against the same schema.

Every response is intentionally identical regardless of *why* a request was
not fully processed (disabled, bad signature, unauthorized sender,
duplicate, rate-limited) -- Work Order acceptance criterion "número não
autorizado não consulta nem altera dados" and "não vaza PII/existência de
household". The only branch with a different HTTP status is a failed
signature/handshake check, which is a transport-authentication failure, not
a business-authorization outcome.

WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75) extends this module past pure
transport: an authorized, non-duplicate, non-rate-limited text message now
reaches `app.services.assistant_orchestrator.plan_and_execute` -- the exact
same Tool Layer entry point `POST /assistant/ask` already calls for the web
Assistant (Work Order item 10, "proibido criar parser/roteador financeiro
paralelo") -- and its natural-language answer is sent back through a
`WhatsAppProvider`. Still zero financial logic of its own: this module never
computes a fact, resolves a hint, or dispatches a typed action; it only
decides *whether* to call the orchestrator (authorized? duplicate? has
text?) and *what to do with its answer* (reply, record outcome).

Transaction design (engineering note, WA-03): claiming a message's
idempotency slot (`gateway.record_inbound_event` + `db.commit()`) and
running the orchestrator are two separate transactions, not one savepoint
wrapping both, unlike WA-01's original `begin_nested()` shape. Reason:
`plan_and_execute`/`execute_typed_action`/`persist_action_proposal` all
issue their own `db.commit()` internally (the same contract `POST
/assistant/ask` already relies on) -- nesting that inside a
`session.begin_nested()` savepoint would have the *first* internal commit
release the savepoint early, silently turning every later write in the same
per-message block into a top-level commit instead of a rollback-able nested
one, and would raise on the savepoint context manager's own `__exit__`.
Committing the claim first instead means: (a) the message-level idempotency
barrier (`uq_whatsapp_inbound_events_message_id`) is still exactly as
strong -- a concurrent redelivery's `record_inbound_event` still fails with
`IntegrityError` before either racer can reach the orchestrator; (b) a
crash *during* orchestration leaves the claim committed (so Meta's own
redelivery-on-timeout would, if it happened, be silently swallowed by the
dedup check rather than retried) -- acceptable because every mutation
`plan_and_execute` can reach is itself idempotent on `proposal_id`
(`execute_typed_action`'s `consumed_at` guard), so nothing here is a
financial-integrity gap, only a "the household might need to resend their
message" UX gap, logged (`whatsapp_gateway.orchestration_failed`) for
operator visibility.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import whatsapp_gateway as gateway

logger = logging.getLogger(__name__)

_UNSUPPORTED_CONTENT_REPLY = (
    "Por enquanto só consigo entender mensagens em texto. Pode escrever o que você quer "
    "registrar ou perguntar?"
)
_ORCHESTRATION_FAILURE_REPLY = (
    "Não consegui processar sua mensagem agora. Tente novamente em instantes."
)

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
    immediately, inside the `try` below). A concurrent-redelivery
    `IntegrityError` rolls back this message's own writes -- including
    whatever it had just done to the rate-limit bucket, which is exactly the
    accounting a duplicate should not have counted -- and this function
    simply returns; nothing from this racer's attempt survives.

    Claiming the message (this function) and running the orchestrator
    (`_run_assistant_reply`) are two separate, sequential transactions --
    see this module's own docstring, "Transaction design", for why the
    original WA-01 `begin_nested()` shape cannot safely wrap a call into
    `plan_and_execute`.
    """

    if gateway.is_duplicate_message(db, provider_message_id=message.provider_message_id):
        return

    try:
        phone_hash = gateway.hash_phone(message.sender_digits)
    except gateway.WhatsAppConfigurationError:
        logger.error("whatsapp_gateway.configuration_error_at_use")
        return

    try:
        if not gateway.check_rate_limit(db, bucket_key=phone_hash):
            gateway.record_inbound_event(
                db,
                provider_message_id=message.provider_message_id,
                sender_phone_hash=phone_hash,
                status="rate_limited",
                household_id=None,
                user_id=None,
            )
            db.commit()
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
            db.commit()
            return

        trace_id = str(uuid.uuid4())
        event = gateway.record_inbound_event(
            db,
            provider_message_id=message.provider_message_id,
            sender_phone_hash=phone_hash,
            status="accepted",
            household_id=authorized.household_id,
            user_id=authorized.user_id,
            trace_id=trace_id,
        )
        db.commit()
    except IntegrityError:
        # Lost the race to a concurrent delivery of the exact same
        # provider_message_id that reached record_inbound_event first --
        # that delivery's row is the durable fact; this one is the
        # duplicate. Nothing else was written in this transaction before
        # the claim above (it is always this function's first write for a
        # given message), so a full rollback discards only this racer's
        # own, uncounted attempt.
        db.rollback()
        return

    _run_assistant_reply(db, message=message, authorized=authorized, trace_id=trace_id, event=event)


def _run_assistant_reply(
    db,
    *,
    message: gateway.NormalizedInboundMessage,
    authorized,
    trace_id: str,
    event,
) -> None:
    """WA-03: the one call site that reaches the Tool Layer from WhatsApp --
    `plan_and_execute` itself already fixes `household_id`/authorization
    from `user`, never from anything in `message`/the raw webhook payload
    (Work Order item 11), and already never raises for a planning/tool
    failure; the `except Exception` below is this module's own outermost
    safety net (Work Order item 12, "falha parcial") for something
    genuinely unexpected (e.g. a database error), not the expected
    fail-closed path.
    """

    from app.models import User
    from app.services.assistant_orchestrator import plan_and_execute

    if not message.text:
        _send_reply(to_digits=message.sender_digits, body=_UNSUPPORTED_CONTENT_REPLY)
        gateway.finalize_inbound_event(db, event, status="unsupported_content", trace_id=trace_id)
        return

    user = db.get(User, authorized.user_id)
    if user is None or user.household_id != authorized.household_id:
        # The allowlist row's target user/household no longer matches (e.g.
        # the user was removed/moved after the number was linked) -- fail
        # closed rather than orchestrate under a stale authorization.
        logger.error("whatsapp_gateway.stale_authorized_number", extra={"trace_id": trace_id})
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return

    try:
        result = plan_and_execute(db, user=user, message=message.text, trace_id=trace_id)
    except Exception:
        logger.exception("whatsapp_gateway.orchestration_failed", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_ORCHESTRATION_FAILURE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return

    _send_reply(to_digits=message.sender_digits, body=result.answer)
    outcome_status = "needs_clarification" if result.needs_clarification else "processed"
    gateway.finalize_inbound_event(db, event, status=outcome_status, trace_id=trace_id)


def _resolve_provider() -> gateway.WhatsAppProvider | None:
    """Thin, monkeypatchable indirection over `gateway.resolve_provider` --
    the seam `tests/test_whatsapp_gateway.py` substitutes a
    `gateway.FakeWhatsAppProvider` through, the same
    override-a-module-level-callable idiom already used for `get_settings`
    elsewhere in this test suite. Production code never overrides it."""

    return gateway.resolve_provider()


def _send_reply(*, to_digits: str, body: str) -> None:
    """Best-effort outbound send -- never raises. A WhatsApp delivery
    failure (unconfigured provider, Meta API error, network timeout) must
    never turn an already-committed/finalized inbound event into a 500 for
    the webhook caller; it is only ever logged for operator visibility."""

    provider = _resolve_provider()
    if provider is None:
        logger.warning("whatsapp_gateway.send_not_configured")
        return
    result = provider.send_message(gateway.build_outbound_text_message(to_digits=to_digits, body=body))
    if not result.ok:
        logger.error("whatsapp_gateway.send_failed", extra={"error": result.error})
