"""Unit tests for `app.services.recurring_income.reconcile_recurring_income`
-- October Go-Live Slice 3 (P0 #87), rebaseline §8.4.

"Quando o crédito real chegar: PREVISTO -> REALIZADO. A conciliação não pode
criar uma segunda receita." These tests exercise the pure matching function
directly against an isolated in-memory SQLite session -- no HTTP, no
FastAPI app -- the same style `tests/test_card_invoice_lifecycle.py`
Section 2 uses for service-level tests.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-recurring-income-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "recurring-income-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base  # noqa: E402
from app.models import Account, Household, Transaction  # noqa: E402
from app.services.financial_state import PREVISTO, REALIZADO  # noqa: E402
from app.services.recurring_income import reconcile_recurring_income  # noqa: E402

_NON_SALARY_DESCRIPTION = "Reembolso viagem"
_NON_SALARY_NORMALIZED = "REEMBOLSO VIAGEM"


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return session_factory


def _household(session_factory, name="Família Renda Recorrente") -> str:
    with session_factory() as db:
        household = Household(name=name)
        db.add(household)
        db.commit()
        return household.id


def _account(session_factory, *, household_id, name="Conta Kelly") -> str:
    with session_factory() as db:
        account = Account(household_id=household_id, name=name, account_type="checking")
        db.add(account)
        db.commit()
        return account.id


def _income(
    session_factory,
    *,
    household_id,
    account_id,
    amount: str,
    competence: str,
    owner_label: str = "Família",
    excluded: bool = False,
    possible_duplicate: bool = False,
    transaction_type: str = "income",
    booked_at: date | None = None,
    description: str = "Salário",
    normalized_description: str = "SALARIO",
) -> str:
    booked_at = booked_at or date(int(competence[:4]), int(competence[5:7]), 5)
    with session_factory() as db:
        txn = Transaction(
            household_id=household_id,
            account_id=account_id,
            booked_at=booked_at,
            occurred_at=booked_at,
            competence=competence,
            description=description,
            normalized_description=normalized_description,
            amount=Decimal(amount),
            transaction_type=transaction_type,
            owner_label=owner_label,
            fingerprint=f"salary{uuid.uuid4().hex}".ljust(64, "0")[:64],
            source_priority=50,
            confidence=Decimal("1"),
            excluded=excluded,
            possible_duplicate=possible_duplicate,
            reviewed=True,
        )
        db.add(txn)
        db.commit()
        return txn.id


def test_no_matching_transaction_stays_previsto() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.matched_transaction_id is None
    assert result.matched_amount is None
    assert result.candidate_count == 0


def test_exact_amount_match_in_competence_is_realizado() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    txn_id = _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == REALIZADO
    assert result.matched_transaction_id == txn_id
    assert result.matched_amount == Decimal("5000.00")


def test_amount_outside_tolerance_never_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5010.00",
        competence="2026-11",
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 1


def test_wrong_competence_never_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-10",
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 0


def test_excluded_transaction_never_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        excluded=True,
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 0


def test_possible_duplicate_transaction_never_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        possible_duplicate=True,
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 0


def test_expense_transaction_of_same_amount_never_matches() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="-5000.00",
        competence="2026-11",
        transaction_type="expense",
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 0


def test_multiple_identified_candidates_stays_previsto_and_ambiguous() -> None:
    # October Go-Live Slice 3, Round 2 of PR #91's review: "não escolher
    # `best` por tie-break e tratar como fato" -- two transactions that both
    # look like the salary (amount + competence + identity evidence) must
    # never be silently resolved to one of them. The period stays PREVISTO
    # and the ambiguity is surfaced instead.
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    far_id = _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        booked_at=date(2026, 11, 2),
    )
    close_id = _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.01",
        competence="2026-11",
        booked_at=date(2026, 11, 5),
    )
    assert far_id != close_id

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.matched_transaction_id is None
    assert result.ambiguous is True
    assert result.candidate_count == 2


def test_description_without_salary_marker_never_promotes_to_realizado() -> None:
    # October Go-Live Slice 3, Round 2: amount + competence alone is not
    # identity evidence -- a coincidentally same-value reimbursement must
    # never be silently promoted to "the salary".
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        description=_NON_SALARY_DESCRIPTION,
        normalized_description=_NON_SALARY_NORMALIZED,
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == PREVISTO
    assert result.matched_transaction_id is None
    assert result.ambiguous is False
    assert result.candidate_count == 1


def test_single_salary_marked_candidate_among_non_salary_ones_is_realizado() -> None:
    # A genuine payroll credit is still found even when an unrelated
    # same-amount, same-competence income also exists -- because only the
    # marked one carries identity evidence, there is exactly one identified
    # candidate, not an ambiguity.
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    salary_id = _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
    )
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        description=_NON_SALARY_DESCRIPTION,
        normalized_description=_NON_SALARY_NORMALIZED,
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("5000.00")
        )
    assert result.financial_state == REALIZADO
    assert result.matched_transaction_id == salary_id
    assert result.ambiguous is False
    assert result.candidate_count == 2


def test_zero_or_negative_expected_amount_stays_previsto_without_querying() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    with session_factory() as db:
        zero = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("0")
        )
        negative = reconcile_recurring_income(
            db, household_id=household_id, period="2026-11", expected_amount=Decimal("-100")
        )
    assert zero.financial_state == PREVISTO
    assert zero.candidate_count == 0
    assert negative.financial_state == PREVISTO
    assert negative.candidate_count == 0


def test_owner_label_filter_restricts_candidates() -> None:
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        owner_label="Vinicius",
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db,
            household_id=household_id,
            period="2026-11",
            expected_amount=Decimal("5000.00"),
            owner_label="Kelly",
        )
    assert result.financial_state == PREVISTO
    assert result.candidate_count == 0


def test_owner_label_is_sufficient_identity_evidence_without_salary_marker() -> None:
    # October Go-Live Slice 3, Round 2: an explicit `owner_label` is itself
    # real, already-persisted identity evidence -- the query narrows to that
    # one person's transactions, so a description marker is not additionally
    # required to promote the match to REALIZADO.
    session_factory = _session_factory()
    household_id = _household(session_factory)
    account_id = _account(session_factory, household_id=household_id)
    txn_id = _income(
        session_factory,
        household_id=household_id,
        account_id=account_id,
        amount="5000.00",
        competence="2026-11",
        owner_label="Kelly",
        description=_NON_SALARY_DESCRIPTION,
        normalized_description=_NON_SALARY_NORMALIZED,
    )

    with session_factory() as db:
        result = reconcile_recurring_income(
            db,
            household_id=household_id,
            period="2026-11",
            expected_amount=Decimal("5000.00"),
            owner_label="Kelly",
        )
    assert result.financial_state == REALIZADO
    assert result.matched_transaction_id == txn_id
    assert result.ambiguous is False
