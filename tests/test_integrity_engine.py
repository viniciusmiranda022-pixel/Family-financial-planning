import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-integrity-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-integrity-data-{uuid.uuid4().hex}")
os.environ.setdefault("SECRET_KEY", "integrity-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

import app.services.financial_integrity as financial_integrity_module  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Account, Household, IntegrityFinding, IntegrityRun, Transaction  # noqa: E402
from app.services.financial_integrity import (  # noqa: E402
    IntegrityRunScope,
    IntegrityRunTrigger,
    assess_integrity,
    build_baseline_checks,
    execute_integrity_run,
)
from app.services.financial_invariants import (  # noqa: E402
    FINANCIAL_RULES_VERSION,
    IntegritySeverity,
    InvariantResult,
    InvariantScope,
    InvariantStatus,
)
from app.services.invariant_registry import evaluate_invariant  # noqa: E402


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


def _seeded_duplicate_transactions(db: Session, *, classification_confidences: tuple[Decimal, ...]) -> Household:
    household = Household(name="Família Teste")
    db.add(household)
    db.flush()
    account = Account(
        household_id=household.id,
        name="Cartão",
        institution="Banco",
        account_type="credit_card",
        owner_label="Família",
    )
    db.add(account)
    db.flush()
    for index, classification_confidence in enumerate(classification_confidences, start=1):
        db.add(
            Transaction(
                household_id=household.id,
                account_id=account.id,
                booked_at=date(2026, 8, index),
                description=f"Duplicidade controlada {index}",
                normalized_description=f"DUPLICIDADE CONTROLADA {index}",
                amount=Decimal("-10.00"),
                transaction_type="expense",
                owner_label="Família",
                fingerprint=str(index) * 64,
                confidence=classification_confidence,
                possible_duplicate=True,
                excluded=False,
                reviewed=False,
            )
        )
    db.flush()
    return household


def test_duplicate_confidence_ignores_classification_confidence() -> None:
    """INV-014's duplicate_confidence must come from the exact fingerprint match,
    never from Transaction.confidence (the classifier's category confidence)."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_duplicate_transactions(
            db, classification_confidences=(Decimal("0.1000"), Decimal("0.9900"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        assert len(checks) == 2

        for check in checks:
            # The deterministic fingerprint match always yields full confidence,
            # regardless of how confident (or not) the classifier was.
            assert check.context.facts["duplicate_confidence"] == Decimal("1.00")
            result = evaluate_invariant(check.invariant_id, check.context)
            assert result.status is InvariantStatus.FAIL
            assert result.metadata["confidence_band"] == "strong"


def test_duplicate_confidence_is_unknown_without_deterministic_evidence() -> None:
    """When the schema cannot prove a duplicate (no fingerprint match), the
    engine must report `unknown` instead of guessing from another field."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_duplicate_transactions(db, classification_confidences=(Decimal("0.9900"),))
        transaction = db.scalar(select(Transaction).where(Transaction.household_id == household.id))
        assert financial_integrity_module._duplicate_confidence(transaction) == Decimal("1.00")

        transaction.possible_duplicate = False
        db.flush()
        assert financial_integrity_module._duplicate_confidence(transaction) is None


def test_execute_integrity_run_rolls_back_partial_findings_on_failure(monkeypatch) -> None:
    """A run that fails midway must not leave any finding it managed to persist
    before the failure; only the run's own `failed` status may survive."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_duplicate_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        assert len(checks) == 2

        original_persist_finding = financial_integrity_module._persist_finding
        calls = {"count": 0}

        def flaky_persist_finding(session, run, result):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated failure after the first finding was persisted")
            return original_persist_finding(session, run, result)

        monkeypatch.setattr(financial_integrity_module, "_persist_finding", flaky_persist_finding)

        with pytest.raises(RuntimeError):
            execute_integrity_run(
                db,
                household_id=household.id,
                scope=IntegrityRunScope.GLOBAL,
                trigger=IntegrityRunTrigger.MANUAL,
                checks=checks,
            )
        assert calls["count"] == 2

        run = db.scalar(select(IntegrityRun).where(IntegrityRun.household_id == household.id))
        assert run.status == "failed"
        db.commit()

        db.expire_all()
        persisted_run = db.get(IntegrityRun, run.id)
        assert persisted_run.status == "failed"
        findings = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert findings == []
