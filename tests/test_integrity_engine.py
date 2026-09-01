from decimal import Decimal
from pathlib import Path

from app.services.financial_integrity import assess_integrity
from app.services.financial_invariants import (
    FINANCIAL_RULES_VERSION,
    IntegritySeverity,
    InvariantResult,
    InvariantScope,
    InvariantStatus,
)


def _result(
    invariant_id: str,
    status: InvariantStatus,
    severity: IntegritySeverity,
    *,
    scope: InvariantScope = InvariantScope.TRANSACTION,
    entity_id: str = "entity-1",
    period: str = "2026-08",
) -> InvariantResult:
    return InvariantResult(
        invariant_id=invariant_id,
        financial_rules_version=FINANCIAL_RULES_VERSION,
        status=status,
        severity=severity,
        scope=scope,
        entity_type="transaction",
        entity_id=entity_id,
        period=period,
        trace_id="trace-test",
        message="resultado de teste",
    )


def test_no_applicable_checks_is_unknown_without_artificial_score() -> None:
    assessment = assess_integrity([])
    assert assessment.status.value == "unknown"
    assert assessment.score is None
    assert assessment.trusted_for_projection is False
    assert assessment.trusted_for_reports is False


def test_score_uses_documented_weights_and_worst_result_per_aggregation_key() -> None:
    assessment = assess_integrity(
        [
            _result("INV-001", InvariantStatus.PASS, IntegritySeverity.CRITICAL),
            _result(
                "INV-001",
                InvariantStatus.FAIL,
                IntegritySeverity.CRITICAL,
                entity_id="entity-2",
            ),
            _result("INV-013", InvariantStatus.WARNING, IntegritySeverity.REVIEW),
            _result(
                "INV-018",
                InvariantStatus.PASS,
                IntegritySeverity.BLOCK,
                scope=InvariantScope.PROJECTION,
            ),
        ]
    )
    assert assessment.raw_checks == 4
    assert assessment.applicable_checks == 3
    assert assessment.weighted_points == Decimal("19.00")
    assert assessment.weighted_total == Decimal("28")
    assert assessment.score == Decimal("67.86")
    assert assessment.status.value == "critical"
    assert assessment.trusted_for_projection is False
    assert assessment.trusted_for_reports is False


def test_non_pass_block_finding_blocks_the_affected_trust_gate() -> None:
    assessment = assess_integrity(
        [
            _result(
                "INV-018",
                InvariantStatus.UNKNOWN,
                IntegritySeverity.BLOCK,
                scope=InvariantScope.PROJECTION,
            )
        ]
    )
    assert assessment.status.value == "blocked"
    assert assessment.score == Decimal("50.00")
    assert assessment.trusted_for_projection is False


def test_projection_gate_requires_complete_deterministic_coverage() -> None:
    assessment = assess_integrity(
        [
            _result(
                invariant_id,
                InvariantStatus.PASS,
                IntegritySeverity.BLOCK if invariant_id == "INV-018" else IntegritySeverity.CRITICAL,
                scope=InvariantScope.PROJECTION,
            )
            for invariant_id in ("INV-005", "INV-006", "INV-018", "INV-022")
        ]
    )
    assert assessment.trusted_for_projection is True


def test_integrity_orchestrator_has_no_advisor_or_codex_dependency() -> None:
    source = Path("app/services/financial_integrity.py").read_text(encoding="utf-8").lower()
    assert "codex_client" not in source
    assert "advisor_client" not in source
