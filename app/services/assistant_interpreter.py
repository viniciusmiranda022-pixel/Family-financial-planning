"""Codex `/v1/interpret` orchestrator -- the Python-side authority boundary
for the Assistente Financeiro's natural-language understanding step (P0 #87,
October Go-Live Slice 4).

Same discipline as `app.services.codex_audit`: `StructuredInterpretation`
has no field that could authorize a mutation -- no account/category/
obligation/invoice id, no `confirmed`, no amount. `_coerce_interpretation`
below is the only function allowed to read a `/v1/interpret` HTTP response,
and it only ever looks at `intent`/`extracted_fields`/`missing_fields`/
`clarifying_question`/`confidence`. Even if the sidecar's own schema
validation were ever bypassed, this module has no code path that reads an
out-of-contract field, so it cannot reach a caller through this module.

`app.services.assistant_actions` is the only caller that turns a
`StructuredInterpretation` into anything real: it re-resolves every
`extracted_fields` hint against actual household data (never trusting a
Codex-provided id, because there never is one) and only ever executes a
typed action the human explicitly confirmed. This module never writes to
the database and never calls anything in `app.api`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.assistant_sanitizer import KNOWN_INTENTS, build_interpret_payload
from app.services.codex_client import CodexAdvisorClient

_ALLOWED_EXTRACTED_FIELD_KEYS = frozenset(
    {
        "amount_text",
        "description",
        "account_hint",
        "counterparty_hint",
        "category_hint",
        "date_text",
        "funding_source_hint",
        "target_hint",
    }
)
_ALLOWED_FUNDING_SOURCE_HINTS = frozenset({"account", "privilege", "unspecified"})

MAX_MISSING_FIELDS = 8
MAX_CLARIFYING_QUESTION_LENGTH = 300


@dataclass(frozen=True, slots=True)
class StructuredInterpretation:
    """The only shape a Codex NLU interpretation can ever take.

    ``available=False`` covers every fail-safe path uniformly: Codex
    disabled, sidecar unreachable, timeout, provider error, or a response
    that failed schema/authority validation. Callers must treat all of
    these identically -- as "interpretation unavailable" -- and fall back
    to asking the user directly, never to a guessed typed action.
    """

    available: bool
    reason: str | None
    intent: str | None = None
    extracted_fields: Mapping[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    clarifying_question: str | None = None
    confidence: float | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "intent": self.intent,
            "extracted_fields": dict(self.extracted_fields),
            "missing_fields": list(self.missing_fields),
            "clarifying_question": self.clarifying_question,
            "confidence": self.confidence,
            "model": self.model,
        }


def _unavailable(reason: str) -> StructuredInterpretation:
    return StructuredInterpretation(available=False, reason=reason)


def _coerce_extracted_fields(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    fields: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_EXTRACTED_FIELD_KEYS or not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if key == "funding_source_hint" and cleaned not in _ALLOWED_FUNDING_SOURCE_HINTS:
            continue
        fields[key] = cleaned[:300]
    return fields


def _coerce_interpretation(raw: Any, *, model: str | None) -> StructuredInterpretation:
    """Turn a raw sidecar response into a `StructuredInterpretation`.

    Reads exactly five keys off the response
    (`intent`/`extracted_fields`/`missing_fields`/`clarifying_question`/
    `confidence`) and nothing else -- an id, an amount, a `confirmed` flag,
    or any other out-of-contract field the response might carry is never
    looked at here, by construction.
    """

    if not isinstance(raw, Mapping):
        return _unavailable("malformed_response")

    intent = raw.get("intent")
    if not isinstance(intent, str) or intent not in KNOWN_INTENTS:
        return _unavailable("invalid_schema")

    confidence_raw = raw.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        return _unavailable("invalid_schema")
    confidence = max(0.0, min(1.0, float(confidence_raw)))

    missing_raw = raw.get("missing_fields")
    if not isinstance(missing_raw, list):
        return _unavailable("invalid_schema")
    missing_fields = tuple(
        str(item)[:60] for item in missing_raw[:MAX_MISSING_FIELDS] if isinstance(item, str) and item
    )

    clarifying_question_raw = raw.get("clarifying_question")
    clarifying_question = (
        clarifying_question_raw.strip()[:MAX_CLARIFYING_QUESTION_LENGTH]
        if isinstance(clarifying_question_raw, str) and clarifying_question_raw.strip()
        else None
    )

    return StructuredInterpretation(
        available=True,
        reason=None,
        intent=intent,
        extracted_fields=_coerce_extracted_fields(raw.get("extracted_fields")),
        missing_fields=missing_fields,
        clarifying_question=clarifying_question,
        confidence=confidence,
        model=str(model) if isinstance(model, str) and model else None,
    )


def interpret_message(
    *,
    message: str,
    history: Sequence[Any] = (),
    trace_id: str | None = None,
    client: CodexAdvisorClient | None = None,
) -> StructuredInterpretation:
    """Request one Codex NLU interpretation and return a fail-safe
    `StructuredInterpretation`. Never raises."""

    interpret_client = client or CodexAdvisorClient()
    if not interpret_client.configured:
        return _unavailable("codex_disabled")

    payload = build_interpret_payload(message=message, history=history, trace_id=trace_id)

    try:
        result = interpret_client.interpret(payload)
    except Exception:
        return _unavailable("provider_unreachable")
    if result.payload is None:
        return _unavailable("provider_unreachable")

    model = result.payload.get("model") if isinstance(result.payload, Mapping) else None
    return _coerce_interpretation(result.payload, model=model)
