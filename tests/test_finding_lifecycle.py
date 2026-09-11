"""Service-level tests for the human finding lifecycle actions (PR 7).

HTTP-level wiring (auth, household isolation, reason validation, audit
events) is covered by `tests/test_api.py`'s monthly-close/finding scenario.
These tests exercise `financial_integrity.{acknowledge,resolve,ignore,
mark_finding_false_positive}_finding` directly against a manufactured
`IntegrityFinding` row, so every status transition and its refusal can be
checked precisely.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-finding-lifecycle-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-finding-lifecycle-data-{uuid.uuid4().hex}")
os.environ.setdefault("SECRET_KEY", "finding-lifecycle-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from app.db import Base  # noqa: E402
from app.models import Household, IntegrityFinding, IntegrityRun, Transaction, User  # noqa: E402
from app.services.financial_integrity import (  # noqa: E402
    FindingLifecycleError,
    acknowledge_finding,
    ignore_finding,
    mark_finding_false_positive,
    resolve_finding,
)


def _engine_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _finding(db: Session, *, household_id: str, run_id: str, entity_id: str | None = None) -> IntegrityFinding:
    finding = IntegrityFinding(
        household_id=household_id,
        run_id=run_id,
        invariant_id="INV-014",
        fingerprint=uuid.uuid4().hex,
        financial_rules_version="2026.09.1",
        trace_id=str(uuid.uuid4()),
        status="open",
        check_status="fail",
        severity="critical",
        scope="transaction",
        entity_type="transaction",
        entity_id=entity_id or "tx-1",
        period="2026-08",
        title="Duplicidade",
        message="Possível duplicidade não resolvida",
    )
    db.add(finding)
    db.flush()
    return finding


def _setup(db: Session) -> tuple[Household, User, IntegrityRun]:
    household = Household(name="Família Findings")
    db.add(household)
    db.flush()
    user = User(
        household_id=household.id,
        name="Admin",
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_admin=True,
    )
    run = IntegrityRun(
        household_id=household.id,
        scope="period",
        period="2026-08",
        status="completed",
        trigger="close",
        financial_rules_version="2026.09.1",
        calculation_version="test",
        trace_id=str(uuid.uuid4()),
    )
    db.add_all([user, run])
    db.flush()
    return household, user, run


def test_acknowledge_finding_stays_active_and_records_reason() -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        finding = _finding(db, household_id=household.id, run_id=run.id)

        acknowledge_finding(finding, user_id=user.id, reason="Estou ciente, investigando")

        assert finding.status == "acknowledged"
        assert finding.acknowledged_by == user.id
        assert finding.acknowledged_at is not None
        assert finding.acknowledgement_reason == "Estou ciente, investigando"


def test_acknowledge_twice_is_rejected() -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        finding = _finding(db, household_id=household.id, run_id=run.id)
        acknowledge_finding(finding, user_id=user.id, reason="Primeira vez")
        with pytest.raises(FindingLifecycleError):
            acknowledge_finding(finding, user_id=user.id, reason="Segunda vez")


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        (resolve_finding, "resolved"),
        (ignore_finding, "ignored"),
        (mark_finding_false_positive, "false_positive"),
    ],
)
def test_terminal_actions_from_open_set_shared_resolution_columns(action, expected_status) -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        finding = _finding(db, household_id=household.id, run_id=run.id)

        action(finding, user_id=user.id, reason="Motivo documentado")

        assert finding.status == expected_status
        # ignore/false-positive deliberately reuse the same resolved_at/by/
        # resolution_reason columns resolve() uses -- see
        # docs/FINANCIAL_RULES.md ("ignored e false_positive seguem a mesma
        # reserva").
        assert finding.resolved_at is not None
        assert finding.resolved_by == user.id
        assert finding.resolution_reason == "Motivo documentado"


@pytest.mark.parametrize("action", [resolve_finding, ignore_finding, mark_finding_false_positive])
def test_terminal_actions_from_acknowledged_are_allowed(action) -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        finding = _finding(db, household_id=household.id, run_id=run.id)
        acknowledge_finding(finding, user_id=user.id, reason="Ciente")

        action(finding, user_id=user.id, reason="Encerrando")
        assert finding.status != "acknowledged"


@pytest.mark.parametrize("action", [resolve_finding, ignore_finding, mark_finding_false_positive])
def test_terminal_actions_are_one_way(action) -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        finding = _finding(db, household_id=household.id, run_id=run.id)
        action(finding, user_id=user.id, reason="Primeira decisão")

        for other in (resolve_finding, ignore_finding, mark_finding_false_positive, acknowledge_finding):
            with pytest.raises(FindingLifecycleError):
                other(finding, user_id=user.id, reason="Segunda tentativa")


def test_lifecycle_actions_never_mutate_the_linked_transaction() -> None:
    with _engine_session() as db:
        household, user, run = _setup(db)
        account_id = None
        transaction = Transaction(
            household_id=household.id,
            account_id=account_id,
            booked_at=date(2026, 8, 15),
            description="Compra original",
            normalized_description="COMPRA ORIGINAL",
            amount=Decimal("-42.00"),
            transaction_type="expense",
            owner_label="Família",
            fingerprint="a" * 64,
            possible_duplicate=True,
            excluded=False,
            reviewed=False,
        )
        db.add(transaction)
        db.flush()
        finding = _finding(db, household_id=household.id, run_id=run.id, entity_id=transaction.id)

        resolve_finding(finding, user_id=user.id, reason="Confirmado como lançamento legítimo")
        db.flush()

        db.refresh(transaction)
        # Resolving the finding is a lifecycle decision about the *finding*
        # only -- it must never flip `possible_duplicate`, `excluded`,
        # `reviewed` or any other Transaction fact by itself (that stays the
        # job of the dedicated `PATCH /transactions/{id}` flow).
        assert transaction.possible_duplicate is True
        assert transaction.excluded is False
        assert transaction.reviewed is False
        assert transaction.amount == Decimal("-42.00")
