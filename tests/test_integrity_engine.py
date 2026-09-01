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
    """Seed N transactions flagged `possible_duplicate=True` but with distinct
    dates and descriptions (no real fingerprint collision). Only useful for
    exercising `build_baseline_checks` selection and persistence plumbing --
    never for asserting a `duplicate_confidence` value, since none of these
    rows actually collide."""

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


def _seeded_colliding_transactions(
    db: Session, *, classification_confidences: tuple[Decimal, ...]
) -> Household:
    """Seed N transactions that share every field `transaction_fingerprint()`
    hashes (account, booked date, amount, normalized description, owner label,
    installment position) -- a real, database-provable duplicate collision.

    Each row is given a *different* stored `fingerprint` column value on
    purpose, so a test built on this fixture can only pass if the derivation
    compares live fields and never trusts the persisted (and, in production,
    potentially stale) `fingerprint` column.
    """

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
                booked_at=date(2026, 8, 15),
                description="Loja Exemplo",
                normalized_description="LOJA EXEMPLO",
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


def _seeded_unpaired_transaction(db: Session, *, possible_duplicate: bool) -> Transaction:
    """Seed a single transaction with no other row sharing its fingerprint
    fields, so it can never be a real duplicate collision."""

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
    transaction = Transaction(
        household_id=household.id,
        account_id=account.id,
        booked_at=date(2026, 8, 20),
        description="Compra única",
        normalized_description="COMPRA UNICA",
        amount=Decimal("-42.00"),
        transaction_type="expense",
        owner_label="Família",
        fingerprint="a" * 64,
        confidence=Decimal("0.9900"),
        possible_duplicate=possible_duplicate,
        excluded=False,
        reviewed=False,
    )
    db.add(transaction)
    db.flush()
    return transaction


def test_duplicate_confidence_ignores_classification_confidence() -> None:
    """INV-014's duplicate_confidence must come from a real fingerprint
    collision between two distinct rows, never from Transaction.confidence
    (the classifier's category confidence)."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
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


def test_duplicate_confidence_is_unknown_without_real_pair() -> None:
    """`possible_duplicate` is a workflow/review flag that `PATCH
    /transactions/{id}` lets any user set on any row, with or without a real
    match. Manually flagging an unpaired transaction must not fabricate
    confidence: the engine must report `unknown`, never `1.00`/"strong"."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        transaction = _seeded_unpaired_transaction(db, possible_duplicate=True)
        db.commit()

        assert financial_integrity_module._duplicate_confidence(db, transaction) is None

        checks = build_baseline_checks(
            db, household_id=transaction.household_id, scope=IntegrityRunScope.GLOBAL
        )
        assert len(checks) == 1
        assert checks[0].context.facts["duplicate_confidence"] is None
        result = evaluate_invariant(checks[0].invariant_id, checks[0].context)
        assert result.status is InvariantStatus.UNKNOWN


def test_duplicate_confidence_follows_live_fields_not_stale_stored_fingerprint() -> None:
    """`Transaction.fingerprint` is computed once at import time and never
    recomputed. `owner_label` -- one of its inputs -- can later be edited via
    `PATCH /transactions/{id}` without the stored fingerprint changing, so the
    derivation must follow the live `owner_label` value, not the frozen
    column, once the pair no longer actually matches."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        first, second = db.scalars(
            select(Transaction).where(Transaction.household_id == household.id).order_by(Transaction.id)
        ).all()
        assert financial_integrity_module._duplicate_confidence(db, first) == Decimal("1.00")

        # Mirrors PATCH /transactions/{id}: owner_label changes, the stored
        # `fingerprint` column is intentionally left untouched.
        second.owner_label = "Outro Responsável"
        db.flush()

        assert financial_integrity_module._duplicate_confidence(db, first) is None


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
