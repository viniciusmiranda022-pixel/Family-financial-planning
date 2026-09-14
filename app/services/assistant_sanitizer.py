"""Allowlist sanitizer for the Codex `/v1/interpret` sidecar contract (P0
#87, October Go-Live Slice 4).

Mirrors `app.services.audit_sanitizer`'s discipline for a much smaller
surface: `/v1/interpret` is pure natural-language understanding (message in,
{intent, extracted_fields, missing_fields, clarifying_question, confidence}
out) -- it never needs, and therefore never receives, any financial fact,
household id, account id, or balance. Only the user's own message text and
a short prior-turn history reach the sidecar, both truncated and stripped of
control characters; nothing here invents or forwards a database value.

Zero SQLAlchemy/FastAPI/HTTP imports -- this module only shapes data another
layer already has in hand.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Any

from app.services.audit_sanitizer import scan_for_forbidden_keys

INTERPRET_SCHEMA_VERSION = "1.0.0"

MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CONTENT_LENGTH = 1000

# Closed vocabulary the sidecar prompt is told to pick from -- sent along so
# Codex never has to guess the exact allowed spelling of an intent; the
# backend re-validates the returned `intent` against this same set
# regardless (see `app.services.assistant_interpreter`), so widening this
# list here alone could never expand what the backend accepts.
KNOWN_INTENTS = (
    "create_expense",
    "create_income",
    "create_internal_transfer",
    "pay_obligation",
    "pay_card_invoice",
    "register_refund",
    "update_asset_value",
    "register_asset_contribution",
    "query",
    "unknown",
)


def _clean_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(character for character in text if character == " " or character.isprintable())
    text = " ".join(text.split())
    return text[:max_length]


def build_interpret_payload(
    *,
    message: str,
    history: Sequence[Any] = (),
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the exact, allowlisted body sent to `POST /v1/interpret`.

    `history` items are duck-typed (`.role`/`.content` attributes, matching
    `app.schemas.AdvisorHistoryItem`, or an equivalent mapping) so this
    module never needs to import `app.schemas` (keeping it dependency-free
    like `audit_sanitizer`). Only the last `MAX_HISTORY_ITEMS` entries are
    forwarded, each truncated -- same cap `/advisor/chat` already applies
    client-side to `state.advisorHistory`.
    """

    clean_message = _clean_text(message, max_length=MAX_MESSAGE_LENGTH)

    clean_history: list[dict[str, str]] = []
    for item in list(history)[-MAX_HISTORY_ITEMS:]:
        role = getattr(item, "role", None)
        if role is None and isinstance(item, dict):
            role = item.get("role")
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        role = str(role) if role in ("user", "assistant") else "user"
        clean_history.append(
            {"role": role, "content": _clean_text(content, max_length=MAX_HISTORY_CONTENT_LENGTH)}
        )

    payload: dict[str, Any] = {
        "schema_version": INTERPRET_SCHEMA_VERSION,
        "known_intents": list(KNOWN_INTENTS),
        "message": clean_message,
        "history": clean_history,
        "trace_id": _clean_text(trace_id, max_length=64) or None,
    }
    scan_for_forbidden_keys(payload)
    return payload
