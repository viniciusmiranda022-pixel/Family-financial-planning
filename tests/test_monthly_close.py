"""Service-level tests for the monthly close lifecycle (PR 7).

These tests manufacture `IntegrityRun`/`IntegrityFinding`/`FinancialSnapshot`
rows directly so the `trust` gate arithmetic can be exercised precisely,
without depending on which invariants the current schema can already prove
(see `build_baseline_checks`' own docstring). HTTP-level wiring, auth and a
*real* invariant-driven rejection are covered by `tests/test_api.py`'s
monthly-close scenario instead.
"""

import os
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-monthly-close-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-monthly-close-data-{uuid.uuid4().hex}")
os.environ.setdefault("SECRET_KEY", "monthly-close-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    FinancialSnapshot,
    Household,
    IntegrityFinding,
    IntegrityRun,
    User,
)
from app.services.monthly_close import (  # noqa: E402
    MonthlyCloseGateError,
    MonthlyCloseStateError,
    assert_close_runnable,
    reopen_monthly_close,
    trust_monthly_close,
    upsert_monthly_close_after_run,
)

PERIOD = "2026-08"


def _household_and_user(db: Session) -> tuple[Household, User]:
    household = Household(name="Família Fechamento")
    db.add(household)
    db.flush()
    user = User(
        household_id=household.id,
        name="Admin",
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_admin=True,
    )
    db.add(user)
    db.flush()
    return household, user


def _completed_period_run(db: Session, *, household_id: str, summary: dict) -> IntegrityRun:
    run = IntegrityRun(
        household_id=household_id,
        scope="period",
        period=PERIOD,
        status="completed",
        trigger="close",
        financial_rules_version="2026.09.1",
        calculation_version="test",
        completed_at=datetime.now(UTC),
        trace_id=str(uuid.uuid4()),
        summary=summary,
    )
    db.add(run)
    db.flush()
    return run


def _healthy_summary() -> dict:
    return {
        "status": "healthy",
        "score": 100.0,
        "trusted_for_projection": True,
        "trusted_for_reports": True,
        "result_counts": {"pass": 1},
        "severity_counts": {},
        "applicable_checks": 1,
        "raw_checks": 1,
    }


def _snapshot(db: Session, *, household_id: str, trusted: bool) -> FinancialSnapshot:
    snapshot = FinancialSnapshot(
        household_id=household_id,
        period=PERIOD,
        version=1,
        checksum=uuid.uuid4().hex,
        calculation_version="test",
        financial_rules_version="2026.09.1",
        integrity_status="healthy" if trusted else "unknown",
        trusted_for_reports=trusted,
        trusted_for_projection=trusted,
        trace_id=str(uuid.uuid4()),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _engine_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_run_never_promotes_directly_to_trusted() -> None:
    with _engine_session() as db:
        household, _user = _household_and_user(db)
        run = _completed_period_run(db, household_id=household.id, summary=_healthy_summary())
        snapshot = _snapshot(db, household_id=household.id, trusted=True)

        close = upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=snapshot.id,
            integrity_run_id=run.id,
        )
        assert close.status == "review_required"
        assert close.snapshot_id == snapshot.id
        assert close.integrity_run_id == run.id


def test_trust_requires_a_prior_run() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        with pytest.raises(MonthlyCloseStateError):
            trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)


def test_trust_blocked_by_material_finding() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        run = _completed_period_run(db, household_id=household.id, summary=_healthy_summary())
        snapshot = _snapshot(db, household_id=household.id, trusted=True)
        upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=snapshot.id,
            integrity_run_id=run.id,
        )
        # A still-open CRITICAL finding for the period must block trust even
        # though the run's own stored summary says "healthy" -- consolidated
        # status is overlaid by every still-open finding (see
        # `consolidated_integrity_status`).
        db.add(
            IntegrityFinding(
                household_id=household.id,
                run_id=run.id,
                invariant_id="INV-014",
                fingerprint=uuid.uuid4().hex,
                financial_rules_version="2026.09.1",
                trace_id=str(uuid.uuid4()),
                status="open",
                check_status="fail",
                severity="critical",
                scope="transaction",
                entity_type="transaction",
                entity_id="tx-1",
                period=PERIOD,
                title="Duplicidade",
                message="Possível duplicidade não resolvida",
            )
        )
        db.flush()

        with pytest.raises(MonthlyCloseGateError) as excinfo:
            trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        assert any("critical" in reason for reason in excinfo.value.reasons)


def test_trust_blocked_by_unknown_status() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        run = _completed_period_run(
            db,
            household_id=household.id,
            summary={
                "status": "unknown",
                "score": None,
                "trusted_for_projection": False,
                "trusted_for_reports": False,
                "result_counts": {},
                "severity_counts": {},
                "applicable_checks": 0,
                "raw_checks": 0,
            },
        )
        snapshot = _snapshot(db, household_id=household.id, trusted=False)
        upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=snapshot.id,
            integrity_run_id=run.id,
        )
        with pytest.raises(MonthlyCloseGateError) as excinfo:
            trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        assert any("unknown" in reason for reason in excinfo.value.reasons)


def test_trust_blocked_by_stale_snapshot() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        run = _completed_period_run(db, household_id=household.id, summary=_healthy_summary())
        stale_snapshot = _snapshot(db, household_id=household.id, trusted=True)
        upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=stale_snapshot.id,
            integrity_run_id=run.id,
        )
        # A newer snapshot version supersedes the linked one without the
        # close being re-run.
        stale_snapshot.status = "superseded"
        newer_snapshot = FinancialSnapshot(
            household_id=household.id,
            period=PERIOD,
            version=2,
            checksum=uuid.uuid4().hex,
            calculation_version="test",
            financial_rules_version="2026.09.1",
            integrity_status="healthy",
            trusted_for_reports=True,
            trusted_for_projection=True,
            trace_id=str(uuid.uuid4()),
        )
        db.add(newer_snapshot)
        db.flush()

        with pytest.raises(MonthlyCloseGateError) as excinfo:
            trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        assert any("mudaram" in reason for reason in excinfo.value.reasons)


def test_trust_succeeds_when_healthy_and_snapshot_trusted() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        run = _completed_period_run(db, household_id=household.id, summary=_healthy_summary())
        snapshot = _snapshot(db, household_id=household.id, trusted=True)
        upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=snapshot.id,
            integrity_run_id=run.id,
        )

        close = trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        assert close.status == "trusted"
        assert close.closed_at is not None
        assert close.closed_by == user.id

        with pytest.raises(MonthlyCloseStateError):
            trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        with pytest.raises(MonthlyCloseStateError):
            assert_close_runnable(db, household_id=household.id, period=PERIOD)


def test_reopen_requires_trusted_and_preserves_close_history() -> None:
    with _engine_session() as db:
        household, user = _household_and_user(db)
        run = _completed_period_run(db, household_id=household.id, summary=_healthy_summary())
        snapshot = _snapshot(db, household_id=household.id, trusted=True)
        upsert_monthly_close_after_run(
            db,
            household_id=household.id,
            period=PERIOD,
            snapshot_id=snapshot.id,
            integrity_run_id=run.id,
        )
        with pytest.raises(MonthlyCloseStateError):
            reopen_monthly_close(
                db, household_id=household.id, period=PERIOD, user_id=user.id, reason="ainda não fechado"
            )

        close = trust_monthly_close(db, household_id=household.id, period=PERIOD, user_id=user.id)
        original_closed_at = close.closed_at
        original_closed_by = close.closed_by

        reopened = reopen_monthly_close(
            db,
            household_id=household.id,
            period=PERIOD,
            user_id=user.id,
            reason="Lançamento retroativo identificado",
        )
        assert reopened.status == "review_required"
        assert reopened.reopened_at is not None
        assert reopened.reopened_by == user.id
        assert reopened.reason == "Lançamento retroativo identificado"
        # History is preserved, not erased.
        assert reopened.closed_at == original_closed_at
        assert reopened.closed_by == original_closed_by
        assert reopened.snapshot_id == snapshot.id
        assert reopened.integrity_run_id == run.id
