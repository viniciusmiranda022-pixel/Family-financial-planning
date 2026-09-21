"""WA-07 (`docs/WORK_ORDER_WA_07.md`, issue #79): media resource-limit guards
and the deterministic yes/no confirmation-reply classifier the WhatsApp
gateway uses once a media-derived `CaptureDraft` preview has been sent.

Deliberately its own small module rather than folded into
`app.services.whatsapp_gateway` (transport primitives) or
`app.whatsapp_gateway_app` (HTTP wiring): both of those already carry a
clear, narrow responsibility documented in their own module docstrings, and
"is this media safe to hand to the OCR/transcription pipeline" /
"did the household just say yes or no" are policy decisions independent of
either transport or HTTP-routing concerns -- easy to unit-test in isolation,
and easy to find again the next time either policy needs to change.

Nothing here parses financial content, calls an LLM, or writes to the
database -- see `app.services.smart_capture` (the one canonical
OCR/transcription/classification pipeline this module's callers hand
guarded bytes to) and `app.api._confirm_capture_items` (the one canonical
write path a "yes" reply here eventually reaches).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

# Meta Cloud API's own documented supported media types for each inbound
# message kind (WA-07 Work Order risk item: "MIME/size limits"). A stricter
# allowlist than "whatever Meta happens to forward" is deliberate: this
# gateway only ever hands bytes to `app.services.smart_capture`'s
# `extract_document_text`/`transcribe_audio`, which only know how to handle
# these formats anyway (see that module's own `SUPPORTED_DOCUMENT_TYPES` and
# `extract_document_text`'s suffix/content-type dispatch) -- allowing a
# format neither pipeline can use would just turn into a `CaptureParseError`
# several steps later, after already having downloaded and held the bytes in
# memory. Rejecting it here, before ever calling `fetch_media`'s caller's own
# guard, is both cheaper and a smaller, more auditable trust boundary.
_IMAGE_MIME_ALLOWLIST = frozenset({"image/jpeg", "image/png", "image/webp"})
_AUDIO_MIME_ALLOWLIST = frozenset(
    {
        "audio/ogg",
        "audio/opus",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/amr",
    }
)
_DOCUMENT_MIME_ALLOWLIST = frozenset({"application/pdf"})

_MIME_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".m4a",
    "audio/amr": ".amr",
    "application/pdf": ".pdf",
}


def _base_mime(raw: str | None) -> str:
    """WhatsApp sends `audio/ogg; codecs=opus` for voice notes -- strip any
    `; parameter` suffix before comparing against the allowlist, the same
    normalization every other MIME-sniffing consumer in this codebase
    (`app.services.smart_capture.extract_document_text`'s `content_type`
    checks) already gets for free from `UploadFile.content_type`, which
    never carries parameters. Meta's Graph API media metadata does."""

    return (raw or "").split(";", 1)[0].strip().lower()


class MediaRejected(ValueError):
    """Raised by `guard_media_bytes` for anything WA-07 must refuse before
    ever handing bytes to the capture pipeline: an unsupported/unexpected
    MIME type for the message's declared kind, or a payload over the
    configured size ceiling. Always a policy rejection, never a transport
    failure (`whatsapp_gateway.MediaFetchResult(ok=False, ...)` already
    covers those) -- the caller replies to the household explaining what
    was rejected and why, and finalizes the inbound event without ever
    calling `smart_capture`."""

    def __init__(self, reason: str, *, user_message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_message = user_message


def media_filename(media_kind: str, mime_type: str | None, provided_filename: str | None) -> str:
    """A synthetic filename for the capture pipeline, which dispatches on
    suffix as well as content-type (`smart_capture.extract_document_text`).
    Never trusts `provided_filename` beyond its extension being safe to
    reuse -- WhatsApp's `document` message type is the only one that ever
    carries one (from the sender's own device, arbitrary/attacker-controlled
    text), and this value is never used for filesystem paths (
    `app.services.crypto.EncryptedDocumentStore.save` names the file by
    `document_id`, never by `original_name`)."""

    extension = _MIME_EXTENSION.get(_base_mime(mime_type), "")
    if not extension and provided_filename:
        suffix = provided_filename.rsplit(".", 1)
        if len(suffix) == 2 and 1 <= len(suffix[1]) <= 5 and suffix[1].isalnum():
            extension = f".{suffix[1].lower()}"
    return f"whatsapp-{media_kind}{extension or '.bin'}"


def guard_media_bytes(
    *,
    media_kind: str,
    mime_type: str | None,
    payload: bytes,
    max_bytes: int,
) -> None:
    """Fail-closed resource-limit gate applied to already-downloaded media
    bytes, before any OCR/transcription work starts (WA-07 Work Order risk
    item: "decompression/resource exhaustion"). Two checks only, both cheap
    and both independent of file *content* (never opens/decodes the
    payload -- that happens later, inside `smart_capture`, which already
    inherits Pillow's own built-in decompression-bomb guard
    (`Image.MAX_IMAGE_PIXELS`) for the image/PDF-rasterization paths, since
    this module does not duplicate that decoding):

    1. declared MIME type must be in the allowlist for `media_kind` --
       rejects a message whose Meta-reported `mime_type` does not match one
       this deployment's OCR/transcription pipeline actually supports;
    2. byte length must not exceed `max_bytes` -- the actual bytes fetched,
       never a caller-supplied/provider-declared size, so a provider lying
       about `Content-Length` cannot bypass this.
    """

    allowlist = {
        "image": _IMAGE_MIME_ALLOWLIST,
        "audio": _AUDIO_MIME_ALLOWLIST,
        "document": _DOCUMENT_MIME_ALLOWLIST,
    }.get(media_kind)
    if allowlist is None:
        raise MediaRejected(
            "unsupported_media_kind",
            user_message="Não consigo processar esse tipo de mensagem ainda.",
        )
    normalized_mime = _base_mime(mime_type)
    if normalized_mime not in allowlist:
        raise MediaRejected(
            "mime_not_allowed",
            user_message=(
                "Não consigo processar esse formato de arquivo. Envie uma foto (JPEG/PNG), "
                "um áudio (ogg/mp3/m4a) ou um PDF."
            ),
        )
    if len(payload) > max_bytes:
        max_mb = max_bytes / (1024 * 1024)
        raise MediaRejected(
            "media_too_large",
            user_message=f"Esse arquivo é maior que o limite de {max_mb:.0f} MB. Envie um arquivo menor.",
        )
    if not payload:
        raise MediaRejected(
            "media_empty",
            user_message="O arquivo enviado chegou vazio. Pode tentar enviar de novo?",
        )


# --- Confirmation-reply classifier ------------------------------------------

# WA-07 hardens the media path with an explicit, human-readable confirmation
# gate: after a media-derived `CaptureDraft` preview is sent, nothing is
# persisted until the household replies with one of these words (or the
# household cancels). This is a small, closed *confirmation* vocabulary --
# distinct from the "no static question catalog" prohibition
# (`docs/WHATSAPP_AI_ASSISTANT_PLAN.md` §3), which is about free-form
# financial questions/commands never being matched by a fixed phrase
# dictionary. A yes/no gate on an already-shown preview is a standard,
# closed-domain UX confirmation (the same shape a "Tem certeza? (S/N)"
# prompt anywhere else in this codebase would use), not an attempt to
# recognize open-ended intent -- see this Work Order's own docstring in
# `app.whatsapp_gateway_app._interpret_pending_capture_reply` for why this
# was chosen over routing the reply through the LLM planner.
_CONFIRM_WORDS = frozenset(
    {
        "sim",
        "confirmo",
        "confirmar",
        "confirma",
        "ok",
        "certo",
        "isso",
        "isso mesmo",
        "pode lancar",
        "pode confirmar",
        "lancar",
        "yes",
    }
)
_CANCEL_WORDS = frozenset(
    {
        "nao",
        "cancelar",
        "cancela",
        "cancelo",
        "descartar",
        "descarta",
        "errado",
        "para",
        "no",
    }
)


def _normalize_reply(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    stripped = re.sub(r"[^\w\s]", "", stripped.lower()).strip()
    return re.sub(r"\s+", " ", stripped)


@dataclass(frozen=True)
class ConfirmationIntent:
    action: Literal["confirm", "cancel"] | None


def interpret_confirmation_reply(text: str | None) -> ConfirmationIntent:
    """Deterministic (never LLM-routed) classification of a WhatsApp text
    reply against a pending media capture's confirm/cancel gate. Returns
    `action=None` for anything that does not unambiguously match -- the
    caller's contract (`app.whatsapp_gateway_app._run_assistant_reply`) is
    that `None` here means "not a confirmation reply", and the message
    falls through to the normal text orchestrator instead, never silently
    treated as either a yes or a no. Matches the whole normalized message
    against the vocabulary (never a substring search) so a longer message
    that merely happens to contain "sim"/"nao" elsewhere is not
    misclassified -- e.g. "confirma que hoje é sexta?" is correctly `None`.
    """

    if not text:
        return ConfirmationIntent(action=None)
    normalized = _normalize_reply(text)
    if not normalized:
        return ConfirmationIntent(action=None)
    if normalized in _CONFIRM_WORDS:
        return ConfirmationIntent(action="confirm")
    if normalized in _CANCEL_WORDS:
        return ConfirmationIntent(action="cancel")
    return ConfirmationIntent(action=None)


__all__ = [
    "MediaRejected",
    "media_filename",
    "guard_media_bytes",
    "ConfirmationIntent",
    "interpret_confirmation_reply",
]
