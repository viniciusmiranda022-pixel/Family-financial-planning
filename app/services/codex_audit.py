"""Codex Semantic Audit orchestrator -- the Python-side authority boundary.

This module is the second, independent enforcement point (the first is the
Advisor sidecar's own output-schema validation in ``advisor/audit.mjs``) for
the rule in docs/ARCHITECTURE.md and docs/INTEGRITY_IMPLEMENTATION_PLAN.md:
Codex can annotate a verdict, it can never produce one.

``AuditOutcome`` has no ``status``/``score``/``trusted_for_*``/``findings``
field, on purpose, forever. ``_coerce_outcome`` below is the single place
that reads a sidecar response, and it only ever looks at
``available``/``reason``/``summary``/``observations``/``confidence``. Even if
the sidecar's own schema validation were ever bypassed and a response smuggled
a ``status``/``score`` field through, this module has no code path that reads
it, so it cannot reach a caller through this module.

Callers combine an ``AuditOutcome`` with the deterministic verdict they
already computed by simply keeping both side by side in the response (see
``app/api.py``); they never let this module's output overwrite a field the
Financial Engine or Financial Integrity Engine produced.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.audit_sanitizer import AuditSanitizationError, build_audit_payload
from app.services.codex_client import CodexAdvisorClient

# Mirrors advisor/audit-schema.json exactly. Kept here (not imported from the
# Node schema) because this is an independent, redundant check: if the two
# ever drift, the stricter one wins and an audit fails safe rather than an
# out-of-contract value slipping through.
_ALLOWED_OBSERVATION_CATEGORY = frozenset(
    {
        "reconciliation",
        "projection",
        "liquidity",
        "duplicate",
        "classification",
        "budget",
        "documentation",
        "other",
    }
)
_ALLOWED_OBSERVATION_TYPE = frozenset({"observation", "hypothesis", "explanation", "recommendation"})
# Advisory severity ceiling: `critical`/`block` belong exclusively to the
# deterministic Financial Integrity Engine and are not valid here even if the
# sidecar's own schema check somehow let one through.
_ALLOWED_OBSERVATION_SEVERITY = frozenset({"info", "review"})

MAX_OBSERVATIONS = 12
MAX_SUMMARY_LENGTH = 800
MAX_MESSAGE_LENGTH = 400
MAX_RECOMMENDATION_LENGTH = 300
MAX_EVIDENCE_REFS = 5


@dataclass(frozen=True, slots=True)
class AuditObservation:
    category: str
    type: str
    severity: str
    message: str
    evidence_ref: tuple[str, ...] = ()
    recommendation: str | None = None


@dataclass(frozen=True, slots=True)
class AuditOutcome:
    """The only shape a Codex Semantic Audit result can ever take.

    ``available=False`` covers every fail-safe path uniformly: Codex
    disabled, sidecar unreachable, timeout, provider error, or a response
    that failed schema/authority validation. Callers must treat all of
    these identically -- as "semantic audit unavailable", never as
    evidence of anything about the underlying financial data.
    """

    available: bool
    reason: str | None
    summary: str | None
    observations: tuple[AuditObservation, ...] = ()
    confidence: float | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "summary": self.summary,
            "confidence": self.confidence,
            "model": self.model,
            "observations": [
                {
                    "category": item.category,
                    "type": item.type,
                    "severity": item.severity,
                    "message": item.message,
                    "evidence_ref": list(item.evidence_ref),
                    "recommendation": item.recommendation,
                }
                for item in self.observations
            ],
        }


def _unavailable(reason: str) -> AuditOutcome:
    return AuditOutcome(available=False, reason=reason, summary=None, observations=(), confidence=None)


def _coerce_observation(raw: Any) -> AuditObservation | None:
    if not isinstance(raw, Mapping):
        return None
    category = str(raw.get("category") or "")
    observation_type = str(raw.get("type") or "")
    severity = str(raw.get("severity") or "")
    message = str(raw.get("message") or "").strip()
    if category not in _ALLOWED_OBSERVATION_CATEGORY:
        return None
    if observation_type not in _ALLOWED_OBSERVATION_TYPE:
        return None
    if severity not in _ALLOWED_OBSERVATION_SEVERITY:
        return None
    if not message:
        return None

    evidence_ref = tuple(
        str(item) for item in (raw.get("evidence_ref") or []) if isinstance(item, (str, int))
    )[:MAX_EVIDENCE_REFS]

    recommendation_raw = raw.get("recommendation")
    recommendation = (
        str(recommendation_raw).strip()[:MAX_RECOMMENDATION_LENGTH] if recommendation_raw else None
    )

    return AuditObservation(
        category=category,
        type=observation_type,
        severity=severity,
        message=message[:MAX_MESSAGE_LENGTH],
        evidence_ref=evidence_ref,
        recommendation=recommendation or None,
    )


def _normalize_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", value).lower()
        if unicodedata.category(character) != "Mn"
    )


def _deterministic_summary(deterministic_status: str) -> str:
    status = _normalize_text(deterministic_status)
    return (
        f"Status determinístico {status}. Auditoria semântica consultiva disponível; "
        "findings e gates do motor permanecem autoritativos."
    )


def _coerce_outcome(raw: Any, *, model: str | None, deterministic_status: str) -> AuditOutcome:
    """Turn a raw sidecar response into an `AuditOutcome`.

    This is intentionally the only function in the codebase allowed to read
    fields off a `/v1/audit` HTTP response. It reads exactly five keys
    (`available`, `reason`, `summary`, `confidence`, `observations`) and
    nothing else -- a `status`, `score`, `trusted_for_projection`, or any
    other field the response might carry is never looked at here, by
    construction, so it cannot influence the return value.
    """

    if not isinstance(raw, Mapping):
        return _unavailable("malformed_response")

    if raw.get("available") is not True:
        reason = raw.get("reason")
        return _unavailable(reason if isinstance(reason, str) and reason else "unavailable")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return _unavailable("invalid_schema")

    confidence_raw = raw.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        return _unavailable("invalid_schema")
    confidence = max(0.0, min(1.0, float(confidence_raw)))

    raw_observations = raw.get("observations")
    if not isinstance(raw_observations, list):
        return _unavailable("invalid_schema")

    observations = tuple(
        item
        for item in (_coerce_observation(entry) for entry in raw_observations[:MAX_OBSERVATIONS])
        if item is not None
    )

    return AuditOutcome(
        available=True,
        reason=None,
        # Never expose provider prose as a verdict-like summary. This is a
        # structural boundary, independent of any finite phrase blacklist.
        summary=_deterministic_summary(deterministic_status),
        observations=observations,
        confidence=confidence,
        model=str(model) if isinstance(model, str) and model else None,
    )


def run_semantic_audit(
    *,
    audit_type: str,
    integrity_status: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]] = (),
    category_spending: Sequence[Mapping[str, Any]] = (),
    financial_metrics: Mapping[str, Any] | None = None,
    period: str | None = None,
    trace_id: str | None = None,
    client: CodexAdvisorClient | None = None,
) -> AuditOutcome:
    """Request one Codex Semantic Audit and return a fail-safe `AuditOutcome`.

    Never raises. Never returns anything shaped like a deterministic
    verdict. A disabled Advisor, an unreachable sidecar, a timeout, a
    provider error, or an invalid/out-of-contract response all collapse
    into ``AuditOutcome(available=False, reason=...)`` -- the caller's
    deterministic status/score/gates are computed independently of this
    call and are never read from, or written by, this function.
    """

    audit_client = client or CodexAdvisorClient()
    if not audit_client.configured:
        return _unavailable("codex_disabled")

    try:
        payload = build_audit_payload(
            audit_type=audit_type,
            integrity_status=integrity_status,
            findings=findings,
            category_spending=category_spending,
            financial_metrics=financial_metrics,
            period=period,
            trace_id=trace_id,
        )
    except AuditSanitizationError:
        return _unavailable("invalid_input")

    try:
        result = audit_client.audit(payload)
    except Exception:
        return _unavailable("provider_unreachable")
    if result.payload is None:
        return _unavailable("provider_unreachable")

    model = result.payload.get("model") if isinstance(result.payload, Mapping) else None
    return _coerce_outcome(
        result.payload,
        model=model,
        deterministic_status=str(integrity_status.get("status") or "unknown"),
    )
