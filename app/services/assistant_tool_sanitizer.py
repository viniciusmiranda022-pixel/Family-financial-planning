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
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.services.assistant_tool_catalog import ALLOWED_TOOLS, TOOL_ARGUMENT_KEYS, catalog_for_prompt
from app.services.audit_sanitizer import scan_for_forbidden_keys

PLAN_SCHEMA_VERSION = "1.0.0"

MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CONTENT_LENGTH = 1000
MAX_CONTEXT_HINTS = 3
MAX_CONTEXT_ARGUMENT_LENGTH = 300


def _clean_text(value: Any, *, max_length: int) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(character for character in text if character == " " or character.isprintable())
    text = " ".join(text.split())
    return text[:max_length]


def _clean_context_hints(context_hints: Sequence[Any]) -> list[dict[str, Any]]:
    """WA-05 (`docs/WORK_ORDER_WA_05.md`, issue #77): the most recent
    read-only tool calls this same conversation already resolved --
    `app.services.assistant_conversation_context` is the only producer of
    this sequence, and already restricts `tool` to a read-only subset and
    `arguments` to that tool's own allowlisted keys, but this function
    re-validates both from scratch regardless of what the caller already
    checked (the same "widening a schema alone cannot widen what is
    trusted" idiom every sanitizer in this codebase already follows) --
    a corrupted/legacy row can never smuggle an unknown tool name or
    argument key into the sidecar prompt through this path.

    Purely additive, structured *hints*, never an instruction and never a
    financial fact: `arguments` here is the exact same flat, free-text bag
    `app.services.assistant_tools.run_tool` already re-validates before any
    tool executes -- nothing here can authorize a mutation, select a
    household, or confirm/cancel/undo anything (those tools are not in the
    allowlist this function accepts)."""

    clean: list[dict[str, Any]] = []
    for item in list(context_hints)[-MAX_CONTEXT_HINTS:]:
        tool = getattr(item, "tool", None)
        if tool is None and isinstance(item, Mapping):
            tool = item.get("tool")
        if not isinstance(tool, str) or tool not in ALLOWED_TOOLS:
            continue
        arguments_raw = getattr(item, "arguments", None)
        if arguments_raw is None and isinstance(item, Mapping):
            arguments_raw = item.get("arguments")
        allowed_keys = TOOL_ARGUMENT_KEYS.get(tool, frozenset())
        arguments: dict[str, str] = {}
        if isinstance(arguments_raw, Mapping):
            for key, value in arguments_raw.items():
                if key in allowed_keys and isinstance(value, str):
                    cleaned = _clean_text(value, max_length=MAX_CONTEXT_ARGUMENT_LENGTH)
                    if cleaned:
                        arguments[key] = cleaned
        clean.append({"tool": tool, "arguments": arguments})
    return clean


def build_plan_payload(
    *,
    message: str,
    history: Sequence[Any] = (),
    context_hints: Sequence[Any] = (),
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

    `context_hints` (WA-05) is additive to `history`: `history` is plain
    prior chat text, `context_hints` is the short, structured list of the
    most recent read-only tool calls this conversation already resolved
    (dimension/period/metric/hints), letting the sidecar fill in what a
    follow-up message leaves implicit without re-deriving it from prose.
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
        "context_hints": _clean_context_hints(context_hints),
        "trace_id": _clean_text(trace_id, max_length=64) or None,
    }
    scan_for_forbidden_keys(payload)
    return payload
