"""Persistent orchestration for deterministic financial integrity checks.

The module owns execution, scoring, trust gates and finding persistence. It never
changes source financial records and it does not call the Advisor/Codex sidecar.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.services.financial_invariants import (
    FINANCIAL_RULES_VERSION,
    IntegritySeverity,
    InvariantContext,
    InvariantResult,
    InvariantScope,
    InvariantStatus,
)
from app.services.invariant_registry import evaluate_invariant, get_invariant

if TYPE_CHECKING:
    from app.models import IntegrityFinding, IntegrityRun, Transaction


class IntegrityRunScope(StrEnum):
    ENTITY = "entity"
    PERIOD = "period"
    GLOBAL = "global"
    PROJECTION = "projection"
    REPORT = "report"
    DOCUMENT = "document"


class IntegrityRunTrigger(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    TRANSACTION = "transaction"
    CLOSE = "close"
    BACKFILL = "backfill"
    SYSTEM = "system"


class ConsolidatedIntegrityStatus(StrEnum):
    BLOCKED = "blocked"
    CRITICAL = "critical"
    REVIEW_REQUIRED = "review_required"
    ATTENTION = "attention"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"


SEVERITY_WEIGHTS = {
    IntegritySeverity.INFO: Decimal("1"),
    IntegritySeverity.WARNING: Decimal("2"),
    IntegritySeverity.REVIEW: Decimal("4"),
    IntegritySeverity.CRITICAL: Decimal("8"),
    IntegritySeverity.BLOCK: Decimal("16"),
}

RESULT_FACTORS = {
    InvariantStatus.PASS: Decimal("1.00"),
    InvariantStatus.WARNING: Decimal("0.75"),
    InvariantStatus.UNKNOWN: Decimal("0.50"),
    InvariantStatus.FAIL: Decimal("0.00"),
}

ACTIVE_FINDING_STATUSES = frozenset({"open", "acknowledged"})

PROJECTION_INVARIANTS = frozenset(
    {
        "INV-005",
        "INV-006",
        "INV-007",
        "INV-008",
        "INV-009",
        "INV-010",
        "INV-011",
        "INV-012",
        "INV-018",
        "INV-022",
    }
)
REPORT_INVARIANTS = frozenset(
    {
        "INV-001",
        "INV-002",
        "INV-003",
        "INV-004",
        "INV-014",
        "INV-015",
        "INV-016",
        "INV-017",
        "INV-019",
        "INV-020",
        "INV-021",
        "INV-022",
    }
)
PROJECTION_REQUIRED_INVARIANTS = frozenset(
    {"INV-005", "INV-006", "INV-018", "INV-022"}
)
REPORT_REQUIRED_INVARIANTS = frozenset({"INV-019", "INV-020", "INV-022"})


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    invariant_id: str
    context: InvariantContext


@dataclass(frozen=True, slots=True)
class IntegrityAssessment:
    status: ConsolidatedIntegrityStatus
    score: Decimal | None
    trusted_for_projection: bool
    trusted_for_reports: bool
    result_counts: dict[str, int]
    severity_counts: dict[str, int]
    applicable_checks: int
    raw_checks: int
    weighted_points: Decimal
    weighted_total: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "score": float(self.score) if self.score is not None else None,
            "trusted_for_projection": self.trusted_for_projection,
            "trusted_for_reports": self.trusted_for_reports,
            "result_counts": self.result_counts,
            "severity_counts": self.severity_counts,
            "applicable_checks": self.applicable_checks,
            "raw_checks": self.raw_checks,
            "score_explanation": {
                "weighted_points": str(self.weighted_points),
                "weighted_total": str(self.weighted_total),
                "formula": "100 * weighted_points / weighted_total",
                "aggregation_key": ["invariant_id", "scope", "period"],
            },
        }


def assess_integrity(results: Sequence[InvariantResult]) -> IntegrityAssessment:
    """Calculate the documented non-decorative score and explicit trust gates."""

    aggregated = _aggregate_results(results)
    if not aggregated:
        return IntegrityAssessment(
            status=ConsolidatedIntegrityStatus.UNKNOWN,
            score=None,
            trusted_for_projection=False,
            trusted_for_reports=False,
            result_counts={},
            severity_counts={},
            applicable_checks=0,
            raw_checks=len(results),
            weighted_points=Decimal("0"),
            weighted_total=Decimal("0"),
        )

    weighted_points = sum(
        (SEVERITY_WEIGHTS[result.severity] * RESULT_FACTORS[result.status] for result in aggregated),
        start=Decimal("0"),
    )
    weighted_total = sum(
        (SEVERITY_WEIGHTS[result.severity] for result in aggregated),
        start=Decimal("0"),
    )
    score = (Decimal("100") * weighted_points / weighted_total).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return IntegrityAssessment(
        status=_status_for_results(aggregated),
        score=score,
        trusted_for_projection=_trust_gate(aggregated, "projection"),
        trusted_for_reports=_trust_gate(aggregated, "reports"),
        result_counts=dict(Counter(result.status.value for result in aggregated)),
        severity_counts=dict(Counter(result.severity.value for result in aggregated)),
        applicable_checks=len(aggregated),
        raw_checks=len(results),
        weighted_points=weighted_points,
        weighted_total=weighted_total,
    )


def execute_integrity_run(
    db: Session,
    *,
    household_id: str,
    scope: IntegrityRunScope | str,
    trigger: IntegrityRunTrigger | str,
    checks: Sequence[IntegrityCheck],
    created_by: str | None = None,
    period: str | None = None,
    scope_entity_type: str | None = None,
    scope_entity_id: str | None = None,
    calculation_version: str = FINANCIAL_RULES_VERSION,
) -> tuple[IntegrityRun, tuple[InvariantResult, ...]]:
    """Evaluate a bounded set of checks and persist only non-pass findings."""

    from app.models import IntegrityRun

    run_scope = IntegrityRunScope(str(scope))
    run_trigger = IntegrityRunTrigger(str(trigger))
    trace_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    timer_started = time.perf_counter()
    run = IntegrityRun(
        household_id=household_id,
        scope=run_scope.value,
        scope_entity_type=scope_entity_type,
        scope_entity_id=scope_entity_id,
        period=period,
        status="running",
        trigger=run_trigger.value,
        financial_rules_version=FINANCIAL_RULES_VERSION,
        calculation_version=calculation_version,
        started_at=started_at,
        trace_id=trace_id,
        created_by=created_by,
    )
    db.add(run)
    db.flush()

    try:
        results = tuple(
            evaluate_invariant(check.invariant_id, _run_context(check.context, run, trace_id))
            for check in checks
        )
        assessment = assess_integrity(results)
        # Derived finding persistence is isolated in its own SAVEPOINT. If any
        # write in this loop fails partway through, SQLAlchemy rolls back to the
        # savepoint automatically, so no partial finding mutation from a failed
        # run can survive the `except` branch below, which only records the
        # run itself as `failed`.
        with db.begin_nested():
            finding_ids = [
                _persist_finding(db, run, result)
                for result in results
                if result.status is not InvariantStatus.PASS
            ]
        completed_at = datetime.now(UTC)
        run.status = "completed"
        run.completed_at = completed_at
        run.duration_ms = max(0, round((time.perf_counter() - timer_started) * 1000))
        run.summary = {
            **assessment.to_dict(),
            "checks": [result.to_dict() for result in results],
            "finding_ids": finding_ids,
            "financial_rules_version": FINANCIAL_RULES_VERSION,
            "trace_id": trace_id,
        }
        db.flush()
        return run, results
    except Exception as exc:
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
        run.duration_ms = max(0, round((time.perf_counter() - timer_started) * 1000))
        run.error_code = type(exc).__name__[:80]
        run.summary = {
            "status": "unknown",
            "score": None,
            "error": "integrity_run_failed",
            "trace_id": trace_id,
        }
        db.flush()
        raise


DUPLICATE_FINGERPRINT_CONFIDENCE = Decimal("1.00")


def _duplicate_confidence(db: Session, transaction: Transaction) -> Decimal | None:
    """Derive INV-014's `duplicate_confidence` from a proven fingerprint collision.

    `Transaction.possible_duplicate` is a workflow/review flag, not proof of
    duplication: `TransactionUpdate` lets any authenticated user flip it via
    `PATCH /transactions/{id}` on any row, with or without a matching
    transaction, so it must never be trusted directly as the confidence fact.

    The stored `Transaction.fingerprint` column is not trusted either. It is
    computed once at import time from `transaction_fingerprint()` (account,
    booked date, amount, normalized description, owner label and installment
    position), but `owner_label` -- one of those inputs -- can be edited later
    through the same PATCH endpoint without the stored fingerprint being
    recomputed. A stale stored fingerprint could therefore either miss a real
    collision or, in principle, still equal another row's stale fingerprint
    without the live fields actually matching.

    The only fact this function trusts is a live query: does another, distinct
    transaction row in the same household currently match every field
    `transaction_fingerprint()` hashes? `normalized_description` (and the
    `description` it derives from) is safe to compare directly because
    `TransactionUpdate` never allows either to change after import, so it
    cannot have drifted from the value used at creation time. When such a row
    exists, the duplication is deterministic and proven, so full confidence
    (1.00) is returned -- the documented "strong" band. Absent that evidence --
    including when `possible_duplicate` was set manually without a real
    match -- the fact cannot be proven from current data and `None` is
    returned so the invariant reports `unknown` instead of trusting the flag.

    `Transaction.confidence` (the importer's category-classification
    confidence) is a distinct fact and is never read here.
    """

    from app.models import Transaction as TransactionModel

    match = db.scalar(
        select(TransactionModel.id)
        .where(
            TransactionModel.household_id == transaction.household_id,
            TransactionModel.id != transaction.id,
            TransactionModel.account_id == transaction.account_id,
            TransactionModel.booked_at == transaction.booked_at,
            TransactionModel.amount == transaction.amount,
            TransactionModel.normalized_description == transaction.normalized_description,
            TransactionModel.owner_label == transaction.owner_label,
            TransactionModel.installment_current == transaction.installment_current,
            TransactionModel.installment_total == transaction.installment_total,
        )
        .limit(1)
    )
    if match is not None:
        return DUPLICATE_FINGERPRINT_CONFIDENCE
    return None


def build_baseline_checks(
    db: Session,
    *,
    household_id: str,
    scope: IntegrityRunScope | str,
    period: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> tuple[IntegrityCheck, ...]:
    """Build checks only from facts the current schema can prove without guessing.

    PR 2 can prove the open probable-duplicate rule. Other checks become applicable
    as canonical reconciliations and snapshots arrive in PRs 3-5.
    """

    from app.models import Transaction

    run_scope = IntegrityRunScope(str(scope))
    statement = select(Transaction).where(
        Transaction.household_id == household_id,
        Transaction.possible_duplicate.is_(True),
    )

    if run_scope is IntegrityRunScope.ENTITY:
        if entity_type != "transaction" or not entity_id:
            raise ValueError("entity scope requires a transaction entity_id")
        statement = statement.where(Transaction.id == entity_id)
    elif run_scope is IntegrityRunScope.PERIOD:
        start, end = _period_bounds(period)
        statement = statement.where(
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
        )

    rows = tuple(db.scalars(statement.order_by(Transaction.booked_at, Transaction.id)).all())
    if run_scope is IntegrityRunScope.ENTITY and not rows:
        entity_exists = db.scalar(
            select(Transaction.id).where(
                Transaction.household_id == household_id,
                Transaction.id == entity_id,
            )
        )
        if entity_exists is None:
            raise LookupError("transaction not found")

    return tuple(
        IntegrityCheck(
            invariant_id="INV-014",
            context=InvariantContext(
                facts={
                    "duplicate_confidence": _duplicate_confidence(db, transaction),
                    "resolution_status": "resolved" if transaction.reviewed else "open",
                    "included_in_totals": not transaction.excluded,
                },
                scope=InvariantScope.TRANSACTION,
                entity_type="transaction",
                entity_id=transaction.id,
                period=transaction.booked_at.strftime("%Y-%m"),
            ),
        )
        for transaction in rows
    )


def consolidated_integrity_status(
    db: Session,
    *,
    household_id: str,
    period: str | None = None,
) -> dict[str, Any]:
    """Return the latest score, overlaid by every still-open persisted finding."""

    from app.models import IntegrityFinding, IntegrityRun

    run_statement = select(IntegrityRun).where(
        IntegrityRun.household_id == household_id,
        IntegrityRun.status == "completed",
    )
    finding_statement = select(IntegrityFinding).where(
        IntegrityFinding.household_id == household_id,
        IntegrityFinding.status.in_(ACTIVE_FINDING_STATUSES),
    )
    if period:
        _period_bounds(period)
        run_statement = run_statement.where(
            IntegrityRun.scope == IntegrityRunScope.PERIOD.value,
            IntegrityRun.period == period,
        )
        finding_statement = finding_statement.where(
            or_(IntegrityFinding.period == period, IntegrityFinding.period.is_(None))
        )
    else:
        run_statement = run_statement.where(
            IntegrityRun.scope == IntegrityRunScope.GLOBAL.value
        )

    latest_run = db.scalar(
        run_statement.order_by(IntegrityRun.completed_at.desc(), IntegrityRun.id.desc()).limit(1)
    )
    findings = tuple(db.scalars(finding_statement).all())
    base = dict(latest_run.summary or {}) if latest_run else _unknown_summary()
    stored_status = _status_for_findings(findings)
    if _status_rank(stored_status) > _status_rank(str(base.get("status", "unknown"))):
        base["status"] = stored_status

    projection_blocked = any(_finding_affects_gate(item, "projection") for item in findings)
    reports_blocked = any(_finding_affects_gate(item, "reports") for item in findings)
    base["trusted_for_projection"] = bool(base.get("trusted_for_projection")) and not projection_blocked
    base["trusted_for_reports"] = bool(base.get("trusted_for_reports")) and not reports_blocked
    base["open_findings"] = len(findings)
    base["open_findings_by_severity"] = dict(Counter(item.severity for item in findings))
    base["last_run"] = serialize_run(latest_run) if latest_run else None
    return base


def serialize_run(run: IntegrityRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "scope": run.scope,
        "scope_entity_type": run.scope_entity_type,
        "scope_entity_id": run.scope_entity_id,
        "period": run.period,
        "status": run.status,
        "trigger": run.trigger,
        "financial_rules_version": run.financial_rules_version,
        "calculation_version": run.calculation_version,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "duration_ms": run.duration_ms,
        "summary": run.summary,
        "error_code": run.error_code,
        "trace_id": run.trace_id,
        "created_by": run.created_by,
    }


def serialize_finding(finding: IntegrityFinding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "run_id": finding.run_id,
        "invariant_id": finding.invariant_id,
        "fingerprint": finding.fingerprint,
        "financial_rules_version": finding.financial_rules_version,
        "trace_id": finding.trace_id,
        "source": finding.source,
        "status": finding.status,
        "check_status": finding.check_status,
        "severity": finding.severity,
        "scope": finding.scope,
        "entity_type": finding.entity_type,
        "entity_id": finding.entity_id,
        "period": finding.period,
        "title": finding.title,
        "message": finding.message,
        "expected_amount": _decimal_string(finding.expected_amount),
        "actual_amount": _decimal_string(finding.actual_amount),
        "difference_amount": _decimal_string(finding.difference_amount),
        "expected": finding.expected,
        "actual": finding.actual,
        "metadata": finding.metadata_json,
        "recommended_action": finding.recommended_action,
        "first_seen_at": _iso(finding.first_seen_at),
        "last_seen_at": _iso(finding.last_seen_at),
        "occurrence_count": finding.occurrence_count,
        "acknowledged_at": _iso(finding.acknowledged_at),
        "acknowledged_by": finding.acknowledged_by,
        "resolved_at": _iso(finding.resolved_at),
        "resolved_by": finding.resolved_by,
        "resolution_reason": finding.resolution_reason,
        "created_at": _iso(finding.created_at),
        "updated_at": _iso(finding.updated_at),
    }


def _aggregate_results(results: Sequence[InvariantResult]) -> tuple[InvariantResult, ...]:
    selected: dict[tuple[str, str, str | None], InvariantResult] = {}
    for result in results:
        key = (result.invariant_id, str(result.scope), result.period)
        current = selected.get(key)
        if current is None or _result_risk(result) > _result_risk(current):
            selected[key] = result
    return tuple(selected.values())


def _result_risk(result: InvariantResult) -> tuple[Decimal, Decimal]:
    return (Decimal("1") - RESULT_FACTORS[result.status], SEVERITY_WEIGHTS[result.severity])


def _status_for_results(results: Sequence[InvariantResult]) -> ConsolidatedIntegrityStatus:
    non_pass = tuple(result for result in results if result.status is not InvariantStatus.PASS)
    if not non_pass:
        return ConsolidatedIntegrityStatus.HEALTHY
    if any(result.severity is IntegritySeverity.BLOCK for result in non_pass):
        return ConsolidatedIntegrityStatus.BLOCKED
    if any(result.severity is IntegritySeverity.CRITICAL for result in non_pass):
        return ConsolidatedIntegrityStatus.CRITICAL
    if any(
        result.severity is IntegritySeverity.REVIEW or result.status is InvariantStatus.UNKNOWN
        for result in non_pass
    ):
        return ConsolidatedIntegrityStatus.REVIEW_REQUIRED
    return ConsolidatedIntegrityStatus.ATTENTION


def _trust_gate(results: Sequence[InvariantResult], gate: str) -> bool:
    relevant_ids = PROJECTION_INVARIANTS if gate == "projection" else REPORT_INVARIANTS
    required_ids = (
        PROJECTION_REQUIRED_INVARIANTS
        if gate == "projection"
        else REPORT_REQUIRED_INVARIANTS
    )
    relevant_scopes = (
        {InvariantScope.PROJECTION.value}
        if gate == "projection"
        else {InvariantScope.REPORT.value}
    )
    relevant = tuple(
        result
        for result in results
        if result.invariant_id in relevant_ids or str(result.scope) in relevant_scopes
    )
    passing_ids = {
        result.invariant_id
        for result in relevant
        if result.status is InvariantStatus.PASS
    }
    return required_ids.issubset(passing_ids) and all(
        result.status is InvariantStatus.PASS for result in relevant
    )


def _run_context(context: InvariantContext, run: IntegrityRun, trace_id: str) -> InvariantContext:
    return replace(
        context,
        entity_type=context.entity_type or run.scope_entity_type or "system",
        entity_id=context.entity_id if context.entity_id is not None else run.scope_entity_id,
        period=context.period or run.period,
        trace_id=context.trace_id or trace_id,
    )


def _persist_finding(db: Session, run: IntegrityRun, result: InvariantResult) -> str:
    from app.models import IntegrityFinding

    fingerprint = _finding_fingerprint(result)
    finding = db.scalar(
        select(IntegrityFinding)
        .where(
            IntegrityFinding.household_id == run.household_id,
            IntegrityFinding.fingerprint == fingerprint,
        )
        .with_for_update()
    )
    payload = result.to_dict()
    now = datetime.now(UTC)
    definition = get_invariant(result.invariant_id)
    values = {
        "financial_rules_version": result.financial_rules_version,
        "trace_id": result.trace_id or run.trace_id,
        "check_status": result.status.value,
        "severity": result.severity.value,
        "scope": str(result.scope),
        "entity_type": result.entity_type,
        "entity_id": str(result.entity_id) if result.entity_id is not None else None,
        "period": result.period,
        "title": definition.title,
        "message": result.message,
        "expected_amount": _monetary_scalar(result.expected),
        "actual_amount": _monetary_scalar(result.actual),
        "difference_amount": result.difference,
        "expected": payload["expected"],
        "actual": payload["actual"],
        "metadata_json": {
            **dict(payload["metadata"] or {}),
            "required_facts": list(definition.required_facts),
        },
        "recommended_action": _recommended_action(result.severity),
        "last_seen_at": now,
    }
    if finding:
        for key, value in values.items():
            setattr(finding, key, value)
        finding.occurrence_count += 1
    else:
        finding = IntegrityFinding(
            household_id=run.household_id,
            run_id=run.id,
            invariant_id=result.invariant_id,
            fingerprint=fingerprint,
            source="deterministic",
            status="open",
            first_seen_at=now,
            occurrence_count=1,
            **values,
        )
        db.add(finding)
    db.flush()
    return finding.id


def _finding_fingerprint(result: InvariantResult) -> str:
    canonical = json.dumps(
        {
            "invariant_id": result.invariant_id,
            "scope": str(result.scope),
            "entity_type": result.entity_type,
            "entity_id": str(result.entity_id) if result.entity_id is not None else None,
            "period": result.period,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _period_bounds(period: str | None) -> tuple[date, date]:
    if period is None:
        raise ValueError("period scope requires period in YYYY-MM format")
    try:
        start = date.fromisoformat(f"{period}-01")
    except ValueError as exc:
        raise ValueError("period must use YYYY-MM format") from exc
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start, end


def _monetary_scalar(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _recommended_action(severity: IntegritySeverity) -> str:
    if severity is IntegritySeverity.BLOCK:
        return "Revisar a evidência determinística antes de confiar na saída afetada."
    if severity in {IntegritySeverity.CRITICAL, IntegritySeverity.REVIEW}:
        return "Revisar a origem e corrigir o registro pelo fluxo próprio; o finding não altera dados."
    return "Verificar a evidência e registrar uma decisão explícita quando necessário."


def _unknown_summary() -> dict[str, Any]:
    return {
        "status": ConsolidatedIntegrityStatus.UNKNOWN.value,
        "score": None,
        "trusted_for_projection": False,
        "trusted_for_reports": False,
        "result_counts": {},
        "severity_counts": {},
        "applicable_checks": 0,
        "raw_checks": 0,
    }


def _status_for_findings(findings: Sequence[IntegrityFinding]) -> str:
    severities = {finding.severity for finding in findings}
    if "block" in severities:
        return ConsolidatedIntegrityStatus.BLOCKED.value
    if "critical" in severities:
        return ConsolidatedIntegrityStatus.CRITICAL.value
    if "review" in severities or any(finding.check_status == "unknown" for finding in findings):
        return ConsolidatedIntegrityStatus.REVIEW_REQUIRED.value
    if findings:
        return ConsolidatedIntegrityStatus.ATTENTION.value
    return ConsolidatedIntegrityStatus.UNKNOWN.value


def _status_rank(status: str) -> int:
    return {
        "unknown": 0,
        "healthy": 1,
        "attention": 2,
        "review_required": 3,
        "critical": 4,
        "blocked": 5,
    }.get(status, 0)


def _finding_affects_gate(finding: IntegrityFinding, gate: str) -> bool:
    relevant_ids = PROJECTION_INVARIANTS if gate == "projection" else REPORT_INVARIANTS
    relevant_scope = "projection" if gate == "projection" else "report"
    return finding.invariant_id in relevant_ids or finding.scope == relevant_scope


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _decimal_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
