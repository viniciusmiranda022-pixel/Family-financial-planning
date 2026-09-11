import os
import uuid
from datetime import UTC, date, datetime
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
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import app.services.financial_integrity as financial_integrity_module  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Document,
    Household,
    IntegrityFinding,
    IntegrityRun,
    Transaction,
    User,
)
from app.services.duplicates import (  # noqa: E402
    discover_transaction_duplicates,
    register_transaction_duplicates,
    resolve_duplicate_group,
)
from app.services.financial_integrity import (  # noqa: E402
    ACTIVE_FINDING_STATUSES,
    IntegrityRunScope,
    IntegrityRunTrigger,
    assess_integrity,
    build_baseline_checks,
    consolidated_integrity_status,
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


def test_projection_gate_stays_false_when_a_required_invariant_is_never_evaluated() -> None:
    """A required invariant that is never run must never be inferred as PASS.

    PR 34 was blocked on merge because the forecast endpoint only ran INV-018
    and inferred the rest of the projection gate from unrelated ad hoc facts.
    Omitting INV-022 entirely here (not failing it -- never evaluating it at
    all) must still leave the gate false, exactly like the missing-coverage
    case already covered for a single check in
    `test_non_pass_block_finding_blocks_the_affected_trust_gate`.
    """

    assessment = assess_integrity(
        [
            _result(
                invariant_id,
                InvariantStatus.PASS,
                IntegritySeverity.BLOCK if invariant_id == "INV-018" else IntegritySeverity.CRITICAL,
                scope=InvariantScope.PROJECTION,
            )
            for invariant_id in ("INV-005", "INV-006", "INV-018")
        ]
    )
    assert assessment.trusted_for_projection is False


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


def test_reopened_group_from_new_discovered_member_is_seen_by_inv014() -> None:
    """2026-09-03 review round 2, P1: a resolved `DuplicateGroup` matched by
    a genuinely new, not-yet-examined transaction must reopen -- otherwise
    `build_baseline_checks`'s open-`DuplicateGroup`-membership branch (see
    its docstring: exactly the mechanism a `discover_transaction_duplicates`
    find relies on, since discovery mode never sets
    `Transaction.possible_duplicate`) never includes that transaction and
    INV-014 silently never evaluates it. This reproduces the review's
    required proof end-to-end: resolve a group, discover a new member
    against it, and assert INV-014 actually sees that member."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família Teste")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Conta", owner_label="Família")
        admin = User(
            household_id=household.id,
            name="Admin",
            username="admin-inv014-reopen",
            password_hash="hash",
            is_admin=True,
        )
        statement = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="5" * 64,
            encrypted_path="k.enc",
        )
        workbook = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="plan.xlsx",
            document_type="financial_plan_workbook",
            sha256="6" * 64,
            encrypted_path="l.enc",
        )
        capture = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="capture.json",
            document_type="capture",
            sha256="7" * 64,
            encrypted_path="m.enc",
        )
        db.add_all([account, admin, statement, workbook, capture])
        db.flush()

        def _txn(document: Document, priority: int, fingerprint_seed: str) -> Transaction:
            return Transaction(
                household_id=household.id,
                account_id=account.id,
                document_id=document.id,
                booked_at=date(2026, 8, 10),
                description="Mercado Central",
                normalized_description="MERCADO CENTRAL",
                amount=Decimal("-100.00"),
                transaction_type="expense",
                owner_label="Família",
                fingerprint=fingerprint_seed.rjust(64, "0"),
                source_priority=priority,
                confidence=Decimal("1"),
            )

        imported = _txn(statement, 70, "a")
        authoritative = _txn(workbook, 100, "b")
        db.add_all([imported, authoritative])
        db.flush()

        group, _ = register_transaction_duplicates(
            db, transaction=authoritative, household_id=household.id
        )
        resolve_duplicate_group(
            db,
            group_id=group.id,
            household_id=household.id,
            resolution="distinct",
            user_id=admin.id,
            reason="Movimentos distintos confirmados",
        )
        assert group.status == "resolved"

        # A legacy row a backfill pass is scanning for the first time.
        late_evidence = _txn(capture, 60, "c")
        db.add(late_evidence)
        db.flush()

        discover_transaction_duplicates(db, transaction=late_evidence, household_id=household.id)
        assert group.status == "open"
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        inv014_entity_ids = {
            check.context.entity_id for check in checks if check.invariant_id == "INV-014"
        }
        assert late_evidence.id in inv014_entity_ids
        # Discovery mode never touches the source `Transaction` rows.
        assert late_evidence.possible_duplicate is False
        assert late_evidence.canonical_status == "unassigned"


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


def test_execute_integrity_run_supersedes_stale_finding_when_check_passes() -> None:
    """A FAIL creates an open finding. Once the household resolves the
    duplicate through the supported review workflow (`Transaction.reviewed`,
    not direct DB surgery) and INV-014 re-evaluates to PASS, the prior
    finding must stop being active: `consolidated_integrity_status` must not
    stay `critical` forever on stale evidence. The finding is never marked
    `resolved` automatically -- docs/FINANCIAL_RULES.md reserves that for the
    human-driven resolve action -- so history (first_seen_at, occurrence
    count, resolved_at/resolved_by staying empty) must survive untouched."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        _, results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=checks,
        )
        db.commit()
        assert all(result.status is InvariantStatus.FAIL for result in results)

        opened = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(opened) == 2
        assert all(finding.status == "open" for finding in opened)
        first_seen_by_id = {finding.id: finding.first_seen_at for finding in opened}
        occurrence_by_id = {finding.id: finding.occurrence_count for finding in opened}

        status_before = consolidated_integrity_status(db, household_id=household.id)
        assert status_before["status"] == "critical"
        assert status_before["open_findings"] == 2

        transactions = db.scalars(
            select(Transaction).where(Transaction.household_id == household.id)
        ).all()
        for transaction in transactions:
            transaction.reviewed = True
        db.flush()

        rerun_checks = build_baseline_checks(
            db, household_id=household.id, scope=IntegrityRunScope.GLOBAL
        )
        _, rerun_results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=rerun_checks,
        )
        db.commit()
        assert all(result.status is InvariantStatus.PASS for result in rerun_results)

        db.expire_all()
        superseded = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(superseded) == 2
        for finding in superseded:
            assert finding.status == "superseded"
            assert finding.status not in ACTIVE_FINDING_STATUSES
            # Never claims a human resolution happened.
            assert finding.resolved_at is None
            assert finding.resolved_by is None
            assert finding.resolution_reason is None
            # History is preserved, not rewritten.
            assert finding.first_seen_at == first_seen_by_id[finding.id]
            assert finding.occurrence_count == occurrence_by_id[finding.id]
            assert finding.check_status == "fail"
            assert finding.metadata_json.get("superseded_by_run_id")

        status_after = consolidated_integrity_status(db, household_id=household.id)
        assert status_after["status"] == "healthy"
        assert status_after["open_findings"] == 0


def test_persist_finding_recovers_from_concurrent_insert_race(monkeypatch) -> None:
    """`SELECT ... FOR UPDATE` cannot lock a fingerprint no run has persisted
    yet, so two runs racing to be the first to persist the same new
    fingerprint can both miss and both attempt to `INSERT`. The loser must
    recover via the unique constraint (savepoint + retry + reselect) and fall
    back to the reincidence update, instead of leaving the run's session in a
    broken transaction or losing the winner's finding."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        assert len(checks) == 2
        result = evaluate_invariant(checks[0].invariant_id, checks[0].context)
        assert result.status is InvariantStatus.FAIL

        run = IntegrityRun(
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL.value,
            status="running",
            trigger=IntegrityRunTrigger.MANUAL.value,
            financial_rules_version=FINANCIAL_RULES_VERSION,
            calculation_version=FINANCIAL_RULES_VERSION,
            started_at=datetime.now(UTC),
            trace_id="trace-race",
        )
        db.add(run)
        db.flush()

        fingerprint = financial_integrity_module._finding_fingerprint(result)

        # Simulate the concurrent winner: another run's row for this exact
        # fingerprint already committed before our own SELECT ... FOR UPDATE
        # became visible to it -- the scenario `SELECT ... FOR UPDATE` cannot
        # protect against because no row existed for it to lock at read time.
        now = datetime.now(UTC)
        winner = IntegrityFinding(
            household_id=household.id,
            run_id=run.id,
            invariant_id=result.invariant_id,
            fingerprint=fingerprint,
            source="deterministic",
            status="open",
            financial_rules_version=result.financial_rules_version,
            trace_id="trace-winner",
            check_status=result.status.value,
            severity=result.severity.value,
            scope=str(result.scope),
            entity_type=result.entity_type,
            entity_id=str(result.entity_id),
            period=result.period,
            title="Duplicidade",
            message="ocorrência concorrente original",
            metadata_json={},
            first_seen_at=now,
            last_seen_at=now,
            occurrence_count=1,
        )
        db.add(winner)
        db.flush()

        calls = {"count": 0}
        original_select = financial_integrity_module._select_finding_for_update

        def miss_once(session, household_id, fingerprint_value):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return original_select(session, household_id, fingerprint_value)

        monkeypatch.setattr(financial_integrity_module, "_select_finding_for_update", miss_once)

        finding_id = financial_integrity_module._persist_finding(db, run, result)
        db.flush()

        assert calls["count"] == 2
        assert finding_id == winner.id

        rows = db.scalars(
            select(IntegrityFinding).where(
                IntegrityFinding.household_id == household.id,
                IntegrityFinding.fingerprint == fingerprint,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].occurrence_count == 2
        assert rows[0].status == "open"


def test_supported_ui_resolution_flow_supersedes_finding_even_after_flag_flip() -> None:
    """Reproduces the exact payload the UI sends for "considerar lançamento
    legítimo": `PATCH /transactions/{id}` with `possible_duplicate=False,
    reviewed=True, excluded=False`. `build_baseline_checks` used to select
    only `possible_duplicate=True` rows, so this payload removed the
    transaction from the next run's checks entirely -- `_supersede_finding_if_active`
    was then never called, and the finding stayed `open` (and
    `consolidated_integrity_status` stayed `critical`) forever, even though a
    human had legitimately resolved it. `build_baseline_checks` must keep
    evaluating any transaction with an active INV-014 finding regardless of
    its current `possible_duplicate` value, so the resolution is honored."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        _, results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=checks,
        )
        db.commit()
        assert all(result.status is InvariantStatus.FAIL for result in results)

        opened = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(opened) == 2
        assert all(finding.status == "open" for finding in opened)

        status_before = consolidated_integrity_status(db, household_id=household.id)
        assert status_before["status"] == "critical"

        # Mirrors the real UI payload for both duplicate-pair members: the
        # flag that made them selectable in the first place is cleared.
        transactions = db.scalars(
            select(Transaction).where(Transaction.household_id == household.id)
        ).all()
        for transaction in transactions:
            transaction.excluded = False
            transaction.possible_duplicate = False
            transaction.reviewed = True
        db.flush()

        # Without tracking active findings, this would now be empty.
        rerun_checks = build_baseline_checks(
            db, household_id=household.id, scope=IntegrityRunScope.GLOBAL
        )
        assert len(rerun_checks) == 2

        _, rerun_results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=rerun_checks,
        )
        db.commit()
        assert all(result.status is InvariantStatus.PASS for result in rerun_results)

        db.expire_all()
        superseded = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(superseded) == 2
        for finding in superseded:
            assert finding.status == "superseded"
            assert finding.status not in ACTIVE_FINDING_STATUSES

        status_after = consolidated_integrity_status(db, household_id=household.id)
        assert status_after["status"] == "healthy"
        assert status_after["open_findings"] == 0


def test_superseded_finding_reopens_on_reproduced_failure() -> None:
    """FAIL creates an `open` finding; a later PASS supersedes it; a further
    re-evaluation that reproduces the original failure must reopen it to
    `open` -- not leave it `superseded`, which `ACTIVE_FINDING_STATUSES`
    excludes from `consolidated_integrity_status`, manufacturing false
    confidence about a condition that is failing right now. History
    (`first_seen_at`) survives; `occurrence_count` keeps incrementing and the
    reopen transition is recorded in `metadata`."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = _seeded_colliding_transactions(
            db, classification_confidences=(Decimal("0.9000"), Decimal("0.9000"))
        )
        db.commit()

        checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
        _, results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=checks,
        )
        db.commit()
        assert all(result.status is InvariantStatus.FAIL for result in results)

        opened = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(opened) == 2
        first_seen_by_id = {finding.id: finding.first_seen_at for finding in opened}
        assert all(finding.occurrence_count == 1 for finding in opened)

        transactions = db.scalars(
            select(Transaction).where(Transaction.household_id == household.id)
        ).all()

        # Human review resolves the duplicate: PASS supersedes both findings.
        for transaction in transactions:
            transaction.reviewed = True
        db.flush()
        _, superseding_results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL),
        )
        db.commit()
        assert all(result.status is InvariantStatus.PASS for result in superseding_results)
        db.expire_all()
        superseded = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert all(finding.status == "superseded" for finding in superseded)
        assert consolidated_integrity_status(db, household_id=household.id)["status"] == "healthy"

        # The review is undone (or the condition is genuinely reproduced
        # again): the same fingerprint fails once more.
        for transaction in transactions:
            transaction.reviewed = False
        db.flush()
        _, reopening_results = execute_integrity_run(
            db,
            household_id=household.id,
            scope=IntegrityRunScope.GLOBAL,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL),
        )
        db.commit()
        assert all(result.status is InvariantStatus.FAIL for result in reopening_results)

        db.expire_all()
        reopened = db.scalars(
            select(IntegrityFinding).where(IntegrityFinding.household_id == household.id)
        ).all()
        assert len(reopened) == 2
        for finding in reopened:
            assert finding.status == "open"
            assert finding.status in ACTIVE_FINDING_STATUSES
            assert finding.occurrence_count == 2
            assert finding.check_status == "fail"
            assert finding.first_seen_at == first_seen_by_id[finding.id]
            assert finding.metadata_json.get("reopened_from_status") == "superseded"
            assert finding.metadata_json.get("reopened_at")

        status_final = consolidated_integrity_status(db, household_id=household.id)
        assert status_final["status"] == "critical"
        assert status_final["open_findings"] == 2
