"""Allowlist sanitizer for the Codex Semantic Audit sidecar (PR 6).

This module builds the *only* payload the isolated Advisor sidecar's
``POST /v1/audit`` endpoint is allowed to see. It is a strict allowlist: every
field that reaches the return value is copied out of a known, named source
field by this module, one at a time. Nothing here ever forwards an arbitrary
input dict wholesale, so a caller cannot smuggle an extra field through by
adding it upstream -- it would simply be ignored.

Explicitly never sent, by construction (there is no code path that could put
these into the returned payload):

- ``DATABASE_URL``, credentials, tokens, shared secrets;
- raw documents, OCR text, file paths, binary content;
- full transaction descriptions, account/card numbers, user names;
- anything the deterministic engine did not already compute (this module
  never invents or recomputes a financial figure).

This module has zero SQLAlchemy/FastAPI/HTTP imports and does not call the
Advisor. It only shapes data that some other layer already fetched.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

AUDIT_SCHEMA_VERSION = "1.0.0"

ALLOWED_AUDIT_TYPES = frozenset(
    {"period_review", "projection_review", "reconciliation_review", "report_review"}
)
ALLOWED_INTEGRITY_STATUS = frozenset(
    {"blocked", "critical", "review_required", "attention", "healthy", "unknown"}
)
ALLOWED_CHECK_STATUS = frozenset({"pass", "fail", "warning", "unknown"})
ALLOWED_SEVERITY = frozenset({"info", "warning", "review", "critical", "block"})

ALLOWED_FINANCIAL_METRIC_KEYS = (
    "operating_income",
    "operating_expenses",
    "operating_result",
    "liquidity_balance",
    "distance_to_floor",
    "budget_usage",
    "budget_remaining",
    "closing_uncovered_deficit",
)

MAX_FINDINGS = 30
MAX_CATEGORY_ITEMS = 12
MAX_PENDING_ITEMS = 20
MAX_MESSAGE_LENGTH = 300
MAX_CATEGORY_LENGTH = 80
MAX_TRACE_ID_LENGTH = 64

# Explicit severity rank, most severe first -- mirrors the ordering already
# encoded in app.services.financial_integrity.SEVERITY_WEIGHTS (not imported
# directly: that module pulls in SQLAlchemy, and this one is deliberately
# free of DB/HTTP imports -- see module docstring). Used only to decide
# which findings survive the MAX_FINDINGS cap below; never to change a
# finding's own severity value.
_SEVERITY_RANK = {"block": 0, "critical": 1, "review": 2, "warning": 3, "info": 4}

_INVARIANT_ID_PATTERN = re.compile(r"^INV-\d{3}$")
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Defense in depth only: every field that reaches the payload is already
# hand-picked by name below, so none of these markers should ever be able to
# appear as a *key* in the output. This scan exists to fail loudly if a
# future edit to this module ever widens a field to a raw passthrough.
_FORBIDDEN_KEY_MARKERS = (
    "secret",
    "password",
    "token",
    "database_url",
    "credential",
    "authorization",
    "cookie",
    "dsn",
    "path",
    "document",
    "raw_",
)


class AuditSanitizationError(ValueError):
    """Raised when the sanitizer itself is misused (a programming error),
    never for untrusted input content -- untrusted content is cleaned and
    truncated, never rejected outright, because the Codex audit is best
    effort and must not break the financial engine over a stray string."""


def _clean_text(value: Any, *, max_length: int) -> str:
    """Collapse to a single line, strip control characters, and truncate.

    This does not attempt to detect or remove "prompt injection" phrases --
    that is a losing, unbounded pattern-matching game. The actual defense
    against prompt injection lives in the Advisor's system prompt (every
    field here is framed as inert data, never as an instruction) and in the
    output contract (a fixed schema with additionalProperties: false, which
    structurally cannot carry an authority field). This function only
    guarantees the text is short, single-line and free of control
    characters/binary garbage before it leaves this process.
    """

    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    text = " ".join(text.split())
    return text[:max_length]


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _to_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def scan_for_forbidden_keys(payload: Any, *, path: str = "$") -> None:
    """Raise ``AuditSanitizationError`` if a secret-shaped key is present.

    Belt-and-suspenders check: this module never plucks a forbidden field by
    name, so this should never trigger. It exists so an accidental future
    change that widens a passthrough is caught by
    ``test_audit_sanitizer.py`` instead of shipping.
    """

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _FORBIDDEN_KEY_MARKERS):
                raise AuditSanitizationError(f"forbidden field name detected at {path}.{key}")
            scan_for_forbidden_keys(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            scan_for_forbidden_keys(item, path=f"{path}[{index}]")


def _sanitize_integrity_snapshot(integrity_status: Mapping[str, Any]) -> dict[str, Any]:
    status = str(integrity_status.get("status") or "unknown")
    if status not in ALLOWED_INTEGRITY_STATUS:
        status = "unknown"

    score = _to_number(integrity_status.get("score"))
    if score is not None:
        score = max(0.0, min(100.0, score))

    severity_counts_raw = integrity_status.get("open_findings_by_severity") or {}
    severity_counts: dict[str, int] = {}
    if isinstance(severity_counts_raw, Mapping):
        for key, value in severity_counts_raw.items():
            key_name = str(key).lower()
            if key_name in ALLOWED_SEVERITY:
                severity_counts[key_name] = _to_int(value)

    snapshot: dict[str, Any] = {
        "status": status,
        "score": score,
        "trusted_for_projection": bool(integrity_status.get("trusted_for_projection")),
        "trusted_for_reports": bool(integrity_status.get("trusted_for_reports")),
        "open_findings": _to_int(integrity_status.get("open_findings")),
    }
    if severity_counts:
        snapshot["open_findings_by_severity"] = severity_counts
    return snapshot


def _sanitize_finding(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    finding_id = finding.get("id")
    invariant_id = str(finding.get("invariant_id") or "")
    check_status = str(finding.get("check_status") or "").lower()
    severity = str(finding.get("severity") or "").lower()
    message = finding.get("message") or finding.get("title") or ""

    if not finding_id:
        return None
    if not _INVARIANT_ID_PATTERN.match(invariant_id):
        return None
    if check_status not in ALLOWED_CHECK_STATUS:
        return None
    if severity not in ALLOWED_SEVERITY:
        return None

    cleaned_message = _clean_text(message, max_length=MAX_MESSAGE_LENGTH)
    if not cleaned_message:
        return None

    sanitized: dict[str, Any] = {
        # A finding id is an opaque, randomly generated UUID: it carries no
        # information on its own and is sent only so a Codex observation can
        # correlate back to a specific already-computed finding.
        "opaque_id": str(finding_id),
        "invariant_id": invariant_id,
        "check_status": check_status,
        "severity": severity,
        "message": cleaned_message,
    }
    scope = finding.get("scope")
    if scope:
        sanitized["scope"] = _clean_text(scope, max_length=40)
    period = finding.get("period")
    if period and _PERIOD_PATTERN.match(str(period)):
        sanitized["period"] = str(period)
    return sanitized


def _rank_and_cap_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep the MAX_FINDINGS most severe findings, most severe first.

    Ordering by explicit rank (block > critical > review > warning > info)
    rather than the findings' arrival order or a plain string sort is
    required so a truncation to MAX_FINDINGS can never silently drop a
    BLOCK/CRITICAL finding in favor of a WARNING/REVIEW one -- a lexical
    sort on the severity string ("warning" > "review" > "info" > "critical"
    > "block" alphabetically) would do exactly that. `opaque_id` breaks ties
    between same-severity findings so the result is fully deterministic
    (stable across reruns and process restarts) rather than only
    order-preserving.

    This is a second, independent enforcement of the same ranking
    app/api.py's `_FINDING_SEVERITY_RANK` applies at the database query
    that is this module's only current caller: that call site already
    fetches at most MAX_FINDINGS rows in the right order, so this rarely
    changes anything in production today. It exists because
    `build_audit_payload` is documented and unit-tested as a
    caller-independent, pure transform (see its docstring) -- a future or
    different caller that passes more than MAX_FINDINGS findings, in any
    order, must not be able to reintroduce this defect just because it
    forgot to pre-sort/pre-cap them itself.
    """

    def sort_key(finding: Mapping[str, Any]) -> tuple[int, str]:
        rank = _SEVERITY_RANK.get(finding.get("severity"), len(_SEVERITY_RANK))
        return (rank, str(finding.get("opaque_id") or ""))

    return sorted(findings, key=sort_key)[:MAX_FINDINGS]


def _sanitize_category_breakdown(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for index, item in enumerate(items[:MAX_CATEGORY_ITEMS]):
        category = _clean_text(item.get("category"), max_length=MAX_CATEGORY_LENGTH)
        amount = _to_number(item.get("amount"))
        if not category or amount is None:
            continue
        sanitized.append({"opaque_id": f"cat-{index}", "category": category, "amount": amount})
    return sanitized


def _sanitize_pending_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for index, item in enumerate(items[:MAX_PENDING_ITEMS]):
        category = _clean_text(item.get("category"), max_length=60)
        if not category:
            continue
        sanitized.append({"opaque_id": item.get("opaque_id") or f"pending-{index}", "category": category})
    return sanitized


def _sanitize_financial_metrics(metrics: Mapping[str, Any] | None) -> dict[str, float]:
    if not metrics:
        return {}
    sanitized = {}
    for key in ALLOWED_FINANCIAL_METRIC_KEYS:
        if key in metrics:
            value = _to_number(metrics[key])
            if value is not None:
                sanitized[key] = value
    return sanitized


def _sanitize_trace_id(trace_id: str | None) -> str:
    candidate = trace_id or str(uuid.uuid4())
    cleaned = _clean_text(candidate, max_length=MAX_TRACE_ID_LENGTH)
    return cleaned or str(uuid.uuid4())


def build_audit_payload(
    *,
    audit_type: str,
    integrity_status: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]] = (),
    category_spending: Sequence[Mapping[str, Any]] = (),
    pending_items: Sequence[Mapping[str, Any]] = (),
    financial_metrics: Mapping[str, Any] | None = None,
    period: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the exact, allowlisted, schema-conformant `/v1/audit` request body.

    Every argument is data the deterministic engine already computed
    (``integrity_status`` from
    :func:`app.services.financial_integrity.consolidated_integrity_status`,
    ``findings`` from :func:`app.services.financial_integrity.serialize_finding`,
    ``category_spending``/``financial_metrics`` from a
    :class:`~app.services.financial_snapshots.FinancialSnapshot` payload). This
    function never queries the database and never calls the Advisor -- it is
    a pure, synchronous, unit-testable transform.
    """

    if audit_type not in ALLOWED_AUDIT_TYPES:
        raise AuditSanitizationError(f"unknown audit_type: {audit_type!r}")
    if period is not None and not _PERIOD_PATTERN.match(period):
        raise AuditSanitizationError(f"invalid period: {period!r}")

    # Sanitize every finding first (not just the first MAX_FINDINGS in
    # arrival order), then rank by severity and cap -- otherwise a valid,
    # severe finding could be discarded by an early raw-order slice before
    # it ever reached ranking, and a less severe one kept in its place.
    all_sanitized = [item for item in (_sanitize_finding(finding) for finding in findings) if item]
    sanitized_findings = _rank_and_cap_findings(all_sanitized)

    payload: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_type": audit_type,
        "period": period,
        "trace_id": _sanitize_trace_id(trace_id),
        "integrity_snapshot": _sanitize_integrity_snapshot(integrity_status),
        "findings": sanitized_findings,
    }

    category_breakdown = _sanitize_category_breakdown(list(category_spending))
    if category_breakdown:
        payload["category_breakdown"] = category_breakdown

    pending = _sanitize_pending_items(list(pending_items))
    if pending:
        payload["pending_items"] = pending

    financial_metrics_sanitized = _sanitize_financial_metrics(financial_metrics)
    if financial_metrics_sanitized:
        payload["financial_metrics"] = financial_metrics_sanitized

    # Defense in depth: prove nothing forbidden-shaped is present before
    # this payload ever leaves the process. See `scan_for_forbidden_keys`.
    scan_for_forbidden_keys(payload)
    return payload
