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

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import whatsapp_gateway as gateway
from app.services import whatsapp_media

logger = logging.getLogger(__name__)

_UNSUPPORTED_CONTENT_REPLY = (
    "Por enquanto só consigo entender mensagens em texto, foto, áudio ou PDF. Pode escrever o que "
    "você quer registrar ou perguntar?"
)
_ORCHESTRATION_FAILURE_REPLY = (
    "Não consegui processar sua mensagem agora. Tente novamente em instantes."
)
_MEDIA_UNAVAILABLE_REPLY = (
    "Não consegui buscar o arquivo enviado agora. Tente reenviar em instantes."
)
_MEDIA_NEEDS_INPUT_PREFIX = "Recebi o arquivo, mas "
_MEDIA_REQUIRES_ACCOUNT_REPLY = (
    "Recebi o arquivo e entendi o lançamento, mas não consegui identificar a conta ou o cartão. "
    "Reenvie mencionando o nome do banco/cartão na legenda, ou finalize pelo site."
)
_MEDIA_NO_PENDING_CAPTURE_REPLY = "Essa captura não está mais disponível para confirmação."
_MEDIA_PENDING_CAPTURE_REPLY = (
    'Você já tem um lançamento aguardando confirmação. Responda "confirmar" ou "cancelar" '
    "para essa captura antes de enviar um novo arquivo."
)
_MEDIA_CANCELLED_REPLY = "Combinado, cancelei essa captura. Nada foi lançado."
_MEDIA_CONFIRMED_REPLY = "Lançamento confirmado com sucesso."
_MEDIA_CONFIRM_FAILED_PREFIX = "Não consegui confirmar: "
_MEDIA_DUPLICATE_FILE_REPLY = (
    "Você já enviou esse mesmo arquivo antes. Confira a captura existente pelo site."
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


_METRICS_WINDOW_MINUTES = 60


@gateway_app.get("/health/metrics")
def health_metrics(db: Session = Depends(get_db)) -> dict:
    """WA-07 (`docs/WORK_ORDER_WA_07.md`, issue #79): operational
    availability/failure/latency visibility without ever exposing a
    financial payload, a phone number, a message body or a secret --
    exactly the same "sanitized observability" contract this module's other
    logging already follows (see `_finalize_and_log` below).

    Aggregated counts only, grouped by the already-sanitized `status`
    column of `WhatsAppInboundEvent` (`app.models.WhatsAppInboundEvent`'s
    own docstring: never message text, never a phone number in recoverable
    form) over a fixed, short trailing window -- never a per-message
    listing, which could otherwise let a caller reconstruct traffic timing
    for one specific household by narrowing the window. `outbound_configured`
    mirrors `/health`'s own `gateway_enabled` shape: a plain boolean, never
    the credential itself.
    """

    from app.models import WhatsAppInboundEvent

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(minutes=_METRICS_WINDOW_MINUTES)
    rows = db.execute(
        select(WhatsAppInboundEvent.status, func.count())
        .where(WhatsAppInboundEvent.received_at >= cutoff)
        .group_by(WhatsAppInboundEvent.status)
    ).all()
    return {
        "window_minutes": _METRICS_WINDOW_MINUTES,
        "inbound_by_status": {status: count for status, count in rows},
        "outbound_configured": gateway.resolve_provider() is not None,
        "gateway_enabled": _ready(settings),
    }


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
        await _process_one_message(db, message, settings=settings)
    if messages:
        db.commit()
    return JSONResponse(content=_UNIFORM_OK)


async def _process_one_message(
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

    await _run_assistant_reply(db, message=message, authorized=authorized, trace_id=trace_id, event=event)


async def _run_assistant_reply(
    db,
    *,
    message: gateway.NormalizedInboundMessage,
    authorized,
    trace_id: str,
    event,
) -> None:
    """WA-03/WA-07: the one dispatch point that decides what an authorized,
    non-duplicate, non-rate-limited inbound message reaches next --
    `plan_and_execute` (free text, unchanged since WA-03) for a `"text"`
    message; the media capture pipeline (`_run_media_reply`, new in WA-07)
    for an `image`/`audio`/`document` message; the pending-capture
    confirm/cancel gate (`_resolve_pending_capture_reply`, new in WA-07) for
    a text reply while this number has a `pending_capture_id`; or the
    unsupported-content reply for anything else. Every branch fixes
    `household_id`/authorization from `user` (looked up here, from
    `authorized.user_id`, never from anything in `message`/the raw webhook
    payload -- Work Order item 11) before doing anything else. `plan_and_execute`
    already never raises for a planning/tool failure; the `except Exception`
    around it below is this module's own outermost safety net (Work Order
    item 12, "falha parcial") for something genuinely unexpected (e.g. a
    database error), not the expected fail-closed path -- `_run_media_reply`
    and `_resolve_pending_capture_reply` apply the same discipline for their
    own new failure modes (guarded/logged below, not re-raised here).
    """

    from app.models import User

    logger.info(
        "whatsapp_gateway.inbound_accepted",
        extra={
            "trace_id": trace_id,
            "kind": "media" if message.media_id else ("text" if message.text else "unsupported"),
            "media_kind": message.media_kind,
        },
    )

    # WA-05 (`docs/WORK_ORDER_WA_05.md`, issue #77): `authorized.id` is the
    # stable `WhatsAppAuthorizedNumber` row id for this one phone-user link
    # -- exactly one per active number (unique index on `user_id` and on
    # `phone_hash`), so it doubles as this channel's conversation key
    # without adding a second lookup. Two authorized numbers linked to the
    # same household (e.g. Vinicius and Kelly) always get distinct keys, and
    # a relinked number after deactivation gets a brand-new row/id -- old
    # conversational memory never carries over to a different person taking
    # over a number (Work Order "different households are strictly
    # isolated" / separate-per-user state, applied within one household).
    user = db.get(User, authorized.user_id)
    if user is None or user.household_id != authorized.household_id:
        # The allowlist row's target user/household no longer matches (e.g.
        # the user was removed/moved after the number was linked) -- fail
        # closed rather than orchestrate under a stale authorization.
        logger.error("whatsapp_gateway.stale_authorized_number", extra={"trace_id": trace_id})
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return

    if message.media_id:
        await _run_media_reply(db, message=message, authorized=authorized, user=user, trace_id=trace_id, event=event)
        return

    if message.text and authorized.pending_capture_id:
        resolved = _resolve_pending_capture_reply(
            db, message=message, authorized=authorized, user=user, trace_id=trace_id, event=event
        )
        if resolved:
            return
        # `resolved=False`: the reply text was not a confirm/cancel/override
        # match (`whatsapp_media.interpret_confirmation_reply` returned
        # `None`) -- falls through to the normal text orchestrator below,
        # exactly like any other message. The pending pointer is left
        # untouched so a later "sim"/"não" can still resolve it.

    if not message.text:
        _send_reply(to_digits=message.sender_digits, body=_UNSUPPORTED_CONTENT_REPLY)
        gateway.finalize_inbound_event(db, event, status="unsupported_content", trace_id=trace_id)
        return

    from app.services.assistant_orchestrator import plan_and_execute

    try:
        result = plan_and_execute(
            db,
            user=user,
            message=message.text,
            conversation_id=f"whatsapp:{authorized.id}",
            channel="whatsapp",
            trace_id=trace_id,
        )
    except Exception:
        logger.exception("whatsapp_gateway.orchestration_failed", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_ORCHESTRATION_FAILURE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return

    _send_reply(to_digits=message.sender_digits, body=result.answer)
    outcome_status = "needs_clarification" if result.needs_clarification else "processed"
    gateway.finalize_inbound_event(db, event, status=outcome_status, trace_id=trace_id)
    logger.info(
        "whatsapp_gateway.orchestration_completed",
        extra={"trace_id": trace_id, "status": outcome_status},
    )


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


# --- WA-07: media capture (image/audio/document) --------------------------

_MOVEMENT_LABELS_PT = {
    "expense": "despesa",
    "income": "receita",
    "investment": "aplicação",
    "redemption": "resgate",
    "refund": "reembolso/estorno",
    "transfer": "transferência interna",
    "reconciliation": "pagamento/conciliação",
}
_LARGE_AMOUNT_DETAIL_PREFIX = "Há um lançamento de valor elevado"


def _format_capture_preview(capture) -> str:
    """Sanitized-for-WhatsApp rendering of a `CaptureDraft`'s stored
    proposal -- shown so an explicit "confirmar"/"cancelar" reply is an
    informed one (Work Order item 2: "mesmo fluxo de draft + prévia +
    confirmação"). Summarizes only what the reply itself needs to decide --
    kind, amount, date, description -- never the full `extracted_text`
    (which can hold a document's entire OCR/transcription output, or a
    bank/card statement's full line-by-line detail)."""

    from app.services.notification_templates import format_brl

    try:
        items = json.loads(capture.proposal_json or "[]")
    except (TypeError, ValueError):
        items = []
    if not items:
        return "Recebi o arquivo, mas não encontrei nenhum lançamento para revisar."
    lines: list[str] = []
    for item in items[:5]:
        kind = item.get("kind")
        description = str(item.get("description") or "")[:120]
        try:
            amount_text = format_brl(Decimal(str(item.get("amount", 0))))
        except (TypeError, ValueError, ArithmeticError):
            amount_text = "R$ 0,00"
        if kind == "transaction":
            label = _MOVEMENT_LABELS_PT.get(item.get("movement_type"), "lançamento")
            lines.append(f"- {label}: {amount_text} em {item.get('booked_at')} - {description}")
        elif kind == "obligation":
            lines.append(f"- boleto/obrigação: {amount_text}, vence {item.get('due_date')} - {description}")
        else:
            lines.append(f"- holerite: {amount_text}, competência {item.get('competence')} - {description}")
    suffix = "\n(e mais itens; confira todos no site antes de confirmar)" if len(items) > 5 else ""
    return (
        "Recebi o arquivo e entendi isto:\n"
        + "\n".join(lines)
        + suffix
        + '\n\nResponda "confirmar" para lançar ou "cancelar" para descartar.'
    )


def _load_actionable_pending_capture(db, authorized):
    """WA-07 fix (engineering review finding on PR #109, 2026-09-21): the
    fast-path half of the "no silent pointer overwrite" guard. Returns the
    `CaptureDraft` `authorized.pending_capture_id` still points at, but only
    when it is still actionable (`status == "preview"`) -- `None` when there
    is no pointer, or the pointer is stale (the draft was already resolved
    another way, e.g. confirmed/cancelled from the web UI's own `/captures`
    screen). Never mutates `authorized.pending_capture_id` itself: a stale
    pointer is left for whichever caller is about to replace it to clear
    explicitly, so this helper stays a pure read usable from a cheap
    pre-check before any fetch/OCR/transcription is attempted."""

    if not authorized.pending_capture_id:
        return None
    from app.models import CaptureDraft

    return db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == authorized.pending_capture_id,
            CaptureDraft.household_id == authorized.household_id,
            CaptureDraft.status == "preview",
        )
    )


def _claim_pending_capture_slot(db, authorized, capture_id: str) -> bool:
    """WA-07 fix (engineering review finding on PR #109, 2026-09-21): the
    locked half of the "no silent pointer overwrite" guard -- the actual
    correctness barrier for two near-simultaneous media messages from the
    same number, which `_load_actionable_pending_capture`'s earlier,
    unlocked pre-check cannot by itself close (two racers can both pass that
    check before either commits).

    Takes a row lock on the `WhatsAppAuthorizedNumber` row itself
    (`SELECT ... FOR UPDATE`) and re-reads `pending_capture_id` under that
    lock before deciding whether to claim it for `capture_id`. On
    PostgreSQL this genuinely serializes two concurrent requests: whichever
    one acquires the lock second sees the first's already-claimed pointer
    and backs off instead of overwriting it. On SQLite (unit tests) `FOR
    UPDATE` is a no-op, but SQLite tests never exercise real thread
    concurrency for this path anyway -- the dedicated race coverage runs
    against real PostgreSQL with real threads
    (`tests/test_postgresql_integration.py`).

    Returns `True` when the claim succeeded (`capture_id` is now this
    number's pending pointer) and `False` when another still-actionable
    draft already holds the slot. On `False`, `capture_id`'s own
    `CaptureDraft` is left exactly as `_analyze_and_persist_capture` already
    committed it (still a fully valid, auditable, non-financial `preview`
    row) -- just not reachable from a bare WhatsApp "confirmar"/"cancelar"
    on this channel; the caller is expected to tell the household to finish
    it from the web UI's own `/captures` screen instead, same as any other
    capture this channel's pointer does not currently reach."""

    from app.models import CaptureDraft, WhatsAppAuthorizedNumber

    locked = db.execute(
        select(WhatsAppAuthorizedNumber).where(WhatsAppAuthorizedNumber.id == authorized.id).with_for_update()
    ).scalar_one()
    if locked.pending_capture_id:
        still_actionable = db.scalar(
            select(CaptureDraft.id).where(
                CaptureDraft.id == locked.pending_capture_id,
                CaptureDraft.household_id == authorized.household_id,
                CaptureDraft.status == "preview",
            )
        )
        if still_actionable is not None:
            db.commit()
            return False
    locked.pending_capture_id = capture_id
    db.commit()
    return True


async def _run_media_reply(
    db,
    *,
    message: gateway.NormalizedInboundMessage,
    authorized,
    user,
    trace_id: str,
    event,
) -> None:
    """WA-07 (`docs/WORK_ORDER_WA_07.md`, issue #79): turns an authorized,
    non-duplicate `image`/`audio`/`document` message into a `CaptureDraft`,
    reusing the exact same canonical pipeline the authenticated web upload
    flow uses end to end (`app.api._analyze_and_persist_capture` ->
    `app.services.smart_capture.preview_capture` ->
    `extract_document_text`/`transcribe_audio`/`detect_document_type`/
    `parse_financial_document` -- never a second OCR/transcription/parser).
    Nothing here computes a financial fact or writes a
    `Transaction`/`Obligation`/`PayrollRecord`; that only ever happens later,
    from an explicit "confirmar" reply, via `_resolve_pending_capture_reply`
    -> `app.api._confirm_capture_items` (the same canonical write path the
    web confirmation form uses).

    Replay protection for the media fetch itself needs no dedicated
    mechanism: this function is only ever reached once per
    `provider_message_id`, because `_process_one_message`'s idempotency
    claim (`uq_whatsapp_inbound_events_message_id`) already committed before
    this function is called -- a genuine redelivery of the same
    `provider_message_id` never reaches past that claim, so
    `WhatsAppProvider.fetch_media` is never called twice for one inbound
    event. A *different* message that happens to carry the same underlying
    file (a household resending the same photo) is instead caught by the
    `Document.sha256` uniqueness check below, and replied to without
    creating a second draft.

    WA-07 fix (engineering review finding on PR #109, 2026-09-21): new media
    is rejected up front, before any fetch/OCR/transcription, while this
    number already has a still-actionable pending capture
    (`_load_actionable_pending_capture`) -- a second media message can no
    longer silently replace `authorized.pending_capture_id` and orphan the
    draft the household's next "confirmar"/"cancelar" was actually meant to
    resolve. `_claim_pending_capture_slot` is the corresponding locked
    re-check right before the pointer is actually set, closing the race
    between two near-simultaneous media messages that both pass this
    earlier, unlocked check.
    """

    pending = _load_actionable_pending_capture(db, authorized)
    if pending is not None:
        logger.info("whatsapp_gateway.media_rejected_pending_capture", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_PENDING_CAPTURE_REPLY)
        gateway.finalize_inbound_event(db, event, status="needs_clarification", trace_id=trace_id)
        return

    from app.models import Document
    from app.services.crypto import EncryptedDocumentStore
    from app.services.importer import file_sha256

    settings = get_settings()
    provider = _resolve_provider()
    if provider is None:
        logger.warning("whatsapp_gateway.media_fetch_not_configured", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_UNAVAILABLE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return

    fetch_started = time.perf_counter()
    fetch_result = provider.fetch_media(message.media_id)
    fetch_duration_ms = round((time.perf_counter() - fetch_started) * 1000)
    if not fetch_result.ok or not fetch_result.payload:
        # Never logs `fetch_result.error` for anything but the transport-
        # level classification `whatsapp_gateway._is_transient_send_error`
        # already produces (an exception class name or `http_<code>`) --
        # never a raw response body/URL.
        logger.warning(
            "whatsapp_gateway.media_fetch_failed",
            extra={"trace_id": trace_id, "error": fetch_result.error, "duration_ms": fetch_duration_ms},
        )
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_UNAVAILABLE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return
    logger.info(
        "whatsapp_gateway.media_fetched",
        extra={"trace_id": trace_id, "media_kind": message.media_kind, "duration_ms": fetch_duration_ms},
    )

    mime_type = fetch_result.mime_type or message.media_mime_type
    media_kind = message.media_kind or "document"
    try:
        whatsapp_media.guard_media_bytes(
            media_kind=media_kind,
            mime_type=mime_type,
            payload=fetch_result.payload,
            max_bytes=settings.whatsapp_media_max_mb * 1024 * 1024,
        )
    except whatsapp_media.MediaRejected as exc:
        logger.warning("whatsapp_gateway.media_rejected", extra={"trace_id": trace_id, "reason": exc.reason})
        _send_reply(to_digits=message.sender_digits, body=exc.user_message)
        gateway.finalize_inbound_event(db, event, status="unsupported_content", trace_id=trace_id)
        return

    filename = whatsapp_media.media_filename(media_kind, mime_type, message.media_filename)
    digest = file_sha256(fetch_result.payload)
    existing_document_id = db.scalar(
        select(Document.id).where(Document.household_id == user.household_id, Document.sha256 == digest)
    )
    if existing_document_id is not None:
        logger.info("whatsapp_gateway.media_duplicate_content", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_DUPLICATE_FILE_REPLY)
        gateway.finalize_inbound_event(db, event, status="processed", trace_id=trace_id)
        return

    document = Document(
        household_id=user.household_id,
        original_name=filename,
        document_type="smart_capture",
        sha256=digest,
        encrypted_path="pending",
        status="capture_processing",
    )
    db.add(document)
    db.flush()
    document.encrypted_path = EncryptedDocumentStore().save(document.id, fetch_result.payload)

    from app.api import _analyze_and_persist_capture

    analysis_started = time.perf_counter()
    try:
        capture = await _analyze_and_persist_capture(
            db,
            user,
            document,
            text=message.media_caption,
            filename=filename,
            content_type=mime_type,
            payload=fetch_result.payload,
            document_type="auto",
            account=None,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("whatsapp_gateway.media_capture_failed", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_UNAVAILABLE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return
    analysis_duration_ms = round((time.perf_counter() - analysis_started) * 1000)
    logger.info(
        "whatsapp_gateway.capture_created",
        extra={
            "trace_id": trace_id,
            "capture_status": capture.status,
            "detected_type": capture.detected_type,
            "duration_ms": analysis_duration_ms,
        },
    )

    if capture.status == "needs_input":
        # `capture.notes` here is `str(CaptureParseError(...))` -- a fixed,
        # already-user-facing Portuguese sentence written by
        # `app.services.smart_capture` for exactly this situation (e.g. "Não
        # encontrei um valor...", "O áudio não contém fala reconhecível"),
        # never raw extracted document text. Zero DB mutation beyond this
        # non-financial `CaptureDraft`/`Document` pair -- Work Order
        # acceptance criterion "ambiguidade... deve esclarecer e não deve
        # escrever".
        _send_reply(
            to_digits=message.sender_digits,
            body=_MEDIA_NEEDS_INPUT_PREFIX + (capture.notes or "não consegui entender o conteúdo."),
        )
        gateway.finalize_inbound_event(db, event, status="needs_clarification", trace_id=trace_id)
        return

    try:
        items = json.loads(capture.proposal_json or "[]")
    except (TypeError, ValueError):
        items = []
    if any(item.get("requires_account") for item in items):
        # The same account-resolution pass every capture already goes
        # through (`app.api._enrich_capture_items`) could not resolve a
        # unique account/card from the household's accounts, the message
        # description, or the card's last four digits. WhatsApp has no
        # editable-preview UI in this slice to let the household pick one
        # inline, so this stays a clarification, never a write -- the
        # household finishes it from the web UI's own `/captures` screen,
        # or resends mentioning the account/card by name.
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_REQUIRES_ACCOUNT_REPLY)
        gateway.finalize_inbound_event(db, event, status="needs_clarification", trace_id=trace_id)
        return

    if not _claim_pending_capture_slot(db, authorized, capture.id):
        # Lost the race: another message (media processed concurrently, or
        # a confirm/cancel that resolved a *different* draft and then a
        # third message claimed the slot first) already holds a still-
        # actionable pending capture under the lock. `capture` itself stays
        # a fully committed, auditable `preview` draft -- just not this
        # channel's pointer -- so nothing financial is lost, only WhatsApp
        # reachability for this one draft.
        logger.info("whatsapp_gateway.media_pending_capture_race_lost", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_PENDING_CAPTURE_REPLY)
        gateway.finalize_inbound_event(db, event, status="needs_clarification", trace_id=trace_id)
        return

    _send_reply(to_digits=message.sender_digits, body=_format_capture_preview(capture))
    gateway.finalize_inbound_event(db, event, status="processed", trace_id=trace_id)


def _resolve_pending_capture_reply(
    db,
    *,
    message: gateway.NormalizedInboundMessage,
    authorized,
    user,
    trace_id: str,
    event,
) -> bool:
    """WA-07: the confirm/cancel gate for a media-derived `CaptureDraft`
    already previewed to this number (`authorized.pending_capture_id`).

    Returns `True` when this reply was consumed as a confirm/cancel/large-
    amount-override action (the caller, `_run_assistant_reply`, must not
    also run the text orchestrator on it) and `False` when the reply did
    not match any of those closed phrases, in which case the caller falls
    through to the normal free-text flow unchanged -- see
    `app.services.whatsapp_media`'s own module docstring for why this is a
    small, deterministic, non-LLM-routed gate on an already-shown preview,
    not the "no static question catalog" free-text/financial-command
    vocabulary that prohibition is actually about.

    The pending pointer is cleared as soon as this function decides to act
    on it (confirm, cancel, or a stale/missing/already-resolved draft) --
    never left set after a terminal outcome, so a later stray "sim" cannot
    resolve a capture that was already resolved another way (e.g. from the
    web UI's own `/captures` screen). On the large-amount guard
    specifically, the pointer is deliberately restored (not cleared) so the
    immediate next "confirmar valor alto" reply still resolves this exact
    draft -- see `whatsapp_media.LARGE_AMOUNT_OVERRIDE_PHRASES`'s own
    docstring for why a bare "sim" is never enough on its own to waive that
    guard, mirroring the web confirmation form's own two-step UX for it.
    """

    from app.models import CaptureDraft

    large_override = whatsapp_media.is_large_amount_override(message.text)
    intent = whatsapp_media.interpret_confirmation_reply(message.text)
    if not large_override and intent.action is None:
        return False

    capture = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == authorized.pending_capture_id,
            CaptureDraft.household_id == authorized.household_id,
        )
    )
    authorized.pending_capture_id = None
    if capture is None or capture.status != "preview":
        db.commit()
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_NO_PENDING_CAPTURE_REPLY)
        gateway.finalize_inbound_event(db, event, status="processed", trace_id=trace_id)
        logger.info("whatsapp_gateway.pending_capture_stale", extra={"trace_id": trace_id})
        return True

    if intent.action == "cancel":
        from app.api import audit

        capture.status = "cancelled"
        if capture.document:
            capture.document.status = "capture_cancelled"
        audit(
            db,
            user,
            "capture.cancel",
            "capture_draft",
            capture.id,
            {"source": "whatsapp"},
            trace_id=trace_id,
            source="whatsapp",
        )
        db.commit()
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_CANCELLED_REPLY)
        gateway.finalize_inbound_event(db, event, status="processed", trace_id=trace_id)
        logger.info("whatsapp_gateway.capture_cancelled", extra={"trace_id": trace_id})
        return True

    # `intent.action == "confirm"`, or the message was only the large-amount
    # override phrase (which itself always implies "confirm" -- a household
    # would not send it otherwise; `interpret_confirmation_reply` need not
    # separately recognize it).
    from app.api import CaptureItemRequest, _confirm_capture_items

    try:
        items_raw = json.loads(capture.proposal_json or "[]")
        items = [CaptureItemRequest.model_validate(item) for item in items_raw]
    except (TypeError, ValueError, PydanticValidationError):
        db.rollback()
        logger.exception("whatsapp_gateway.capture_confirm_malformed_proposal", extra={"trace_id": trace_id})
        _send_reply(
            to_digits=message.sender_digits,
            body=_MEDIA_CONFIRM_FAILED_PREFIX + "os dados ficaram inválidos. Tente enviar o arquivo de novo.",
        )
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return True

    try:
        _confirm_capture_items(
            db,
            user,
            capture,
            items,
            confirmed_large_amount=large_override,
            audit_source="whatsapp",
            audit_trace_id=trace_id,
        )
    except HTTPException as exc:
        db.rollback()
        detail = str(exc.detail)
        if exc.status_code == 409 and detail.startswith(_LARGE_AMOUNT_DETAIL_PREFIX):
            authorized.pending_capture_id = capture.id
            db.commit()
            _send_reply(
                to_digits=message.sender_digits,
                body=f'{detail} Se está correto mesmo assim, responda "confirmar valor alto".',
            )
            gateway.finalize_inbound_event(db, event, status="needs_clarification", trace_id=trace_id)
            logger.info("whatsapp_gateway.capture_large_amount_guard", extra={"trace_id": trace_id})
            return True
        logger.warning(
            "whatsapp_gateway.capture_confirm_rejected",
            extra={"trace_id": trace_id, "status_code": exc.status_code},
        )
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_CONFIRM_FAILED_PREFIX + detail)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return True
    except Exception:
        db.rollback()
        logger.exception("whatsapp_gateway.capture_confirm_failed", extra={"trace_id": trace_id})
        _send_reply(to_digits=message.sender_digits, body=_MEDIA_UNAVAILABLE_REPLY)
        gateway.finalize_inbound_event(db, event, status="error", trace_id=trace_id)
        return True

    _send_reply(to_digits=message.sender_digits, body=_MEDIA_CONFIRMED_REPLY)
    gateway.finalize_inbound_event(db, event, status="processed", trace_id=trace_id)
    logger.info("whatsapp_gateway.capture_confirmed", extra={"trace_id": trace_id})
    return True
