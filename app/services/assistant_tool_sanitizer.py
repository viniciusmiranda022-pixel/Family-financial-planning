"""Allowlist sanitizer for the Codex `/v1/plan` sidecar contract (WA-02,
`docs/WORK_ORDER_WA_02.md`, issue #74).

Mirrors `app.services.assistant_sanitizer`'s discipline exactly, for the
Tool Layer's planning contract instead of the single-intent typed-action
contract: `/v1/plan` only ever receives the user's own message text, a
short prior-turn history, the fixed tool catalog
(`app.services.assistant_tool_catalog`) and today's date in
`America/Sao_Paulo` -- never a financial fact, household id, account id or
balance. Zero SQLAlchemy/FastAPI/HTTP imports -- this module only shapes
data another layer already has in hand.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.services.assistant_tool_catalog import catalog_for_prompt
from app.services.audit_sanitizer import scan_for_forbidden_keys

PLAN_SCHEMA_VERSION = "1.0.0"

MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CONTENT_LENGTH = 1000


def _clean_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(character for character in text if character == " " or character.isprintable())
    text = " ".join(text.split())
    return text[:max_length]


def build_plan_payload(
    *,
    message: str,
    history: Sequence[Any] = (),
    today: date,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the exact, allowlisted body sent to `POST /v1/plan`.

    `today` must be the caller's own `America/Sao_Paulo` "today" (see
    `app.services.assistant_tools.sao_paulo_today`) -- this module never
    computes a timezone itself, it only forwards the date its caller
    already resolved, as `today_iso`, for the model to interpret relative
    date expressions against. The backend's own deterministic period
    resolution (`app.services.assistant_tools.resolve_period`) never trusts
    anything the model does with it either.
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
        "schema_version": PLAN_SCHEMA_VERSION,
        "tools_catalog": catalog_for_prompt(),
        "today_iso": today.isoformat(),
        "message": clean_message,
        "history": clean_history,
        "trace_id": _clean_text(trace_id, max_length=64) or None,
    }
    scan_for_forbidden_keys(payload)
    return payload
