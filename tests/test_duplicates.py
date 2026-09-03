import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-duplicates-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "duplicates-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base
from app.models import Account, Document, DuplicateGroupMember, Household, Transaction, User
from app.services.duplicates import (
    discover_transaction_duplicates,
    register_transaction_duplicates,
    resolve_duplicate_group,
)


def _transaction(household_id: str, account_id: str, document_id: str, priority: int) -> Transaction:
    return Transaction(
        household_id=household_id,
        account_id=account_id,
        document_id=document_id,
        booked_at=date(2026, 8, 10),
        occurred_at=date(2026, 8, 10),
        competence="2026-08",
        description="Mercado Central",
        normalized_description="MERCADO CENTRAL",
        amount=Decimal("-100.00"),
        transaction_type="expense",
        owner_label="Família",
        fingerprint=f"{document_id:0<64}"[:64],
        source_priority=priority,
        confidence=Decimal("1"),
    )


def test_duplicate_group_preserves_rows_and_uses_source_precedence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Conta", owner_label="Família")
        admin = User(
            household_id=household.id,
            name="Admin",
            username="admin-duplicates",
            password_hash="hash",
            is_admin=True,
        )
        statement = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="a" * 64,
            encrypted_path="a.enc",
        )
        workbook = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="plan.xlsx",
            document_type="financial_plan_workbook",
            sha256="b" * 64,
            encrypted_path="b.enc",
        )
        db.add_all([account, admin, statement, workbook])
        db.flush()
        imported = _transaction(household.id, account.id, statement.id, 70)
        authoritative = _transaction(household.id, account.id, workbook.id, 100)
        db.add(imported)
        db.flush()
        assert register_transaction_duplicates(
            db, transaction=imported, household_id=household.id
        )[0] is None
        db.add(authoritative)
        db.flush()

        group, assessment = register_transaction_duplicates(
            db, transaction=authoritative, household_id=household.id
        )

        assert group is not None
        assert assessment.band == "strong"
        assert group.canonical_transaction_id == authoritative.id
        assert db.get(Transaction, imported.id) is not None
        assert imported.canonical_status == "supporting"
        assert imported.excluded is True
        assert authoritative.canonical_status == "canonical"
        assert len(db.scalars(select(DuplicateGroupMember)).all()) == 2

        resolve_duplicate_group(
            db,
            group_id=group.id,
            household_id=household.id,
            resolution="distinct",
            user_id=admin.id,
            reason="Movimentos distintos confirmados",
        )
        assert group.status == "resolved"
        assert all(
            member.role == "distinct"
            for member in db.scalars(select(DuplicateGroupMember)).all()
        )
        assert imported.excluded is False
        assert authoritative.excluded is False


def test_probable_same_document_match_does_not_auto_exclude() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Conta", owner_label="Família")
        document = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="c" * 64,
            encrypted_path="c.enc",
        )
        db.add_all([account, document])
        db.flush()
        first = _transaction(household.id, account.id, document.id, 70)
        second = _transaction(household.id, account.id, document.id, 70)
        second.fingerprint = "z" * 64
        db.add_all([first, second])
        db.flush()

        group, assessment = register_transaction_duplicates(
            db, transaction=second, household_id=household.id
        )

        assert group is not None
        assert assessment.band == "probable"
        assert first.excluded is False
        assert second.excluded is False
        assert first.canonical_status == "unassigned"
        assert second.canonical_status == "unassigned"


def test_discover_transaction_duplicates_persists_evidence_without_mutating_transactions() -> None:
    """`app.cli.backfill`'s non-mutating path: historical reprocessing must
    surface the same derived evidence `register_transaction_duplicates`
    would, without ever writing to either transaction's own classification
    columns (2026-09-03 engineering review of PR 8)."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Conta", owner_label="Família")
        statement = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="d" * 64,
            encrypted_path="d.enc",
        )
        workbook = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="plan.xlsx",
            document_type="financial_plan_workbook",
            sha256="e" * 64,
            encrypted_path="e.enc",
        )
        db.add_all([account, statement, workbook])
        db.flush()
        imported = _transaction(household.id, account.id, statement.id, 70)
        authoritative = _transaction(household.id, account.id, workbook.id, 100)
        db.add_all([imported, authoritative])
        db.flush()

        group, assessment = discover_transaction_duplicates(
            db, transaction=authoritative, household_id=household.id
        )

        assert group is not None
        assert assessment.band == "strong"
        assert group.status == "open"
        assert group.canonical_transaction_id == authoritative.id

        # Derived evidence is persisted and immediately visible for review...
        members = db.scalars(select(DuplicateGroupMember)).all()
        assert len(members) == 2
        assert {member.transaction_id for member in members} == {imported.id, authoritative.id}

        # ...but neither transaction's own columns were touched.
        for row in (imported, authoritative):
            assert row.canonical_status == "unassigned"
            assert row.possible_duplicate is False
            assert row.excluded is False
            assert row.duplicate_group_id is None


def test_discover_transaction_duplicates_does_not_reopen_a_resolved_group() -> None:
    """A human's `resolve_duplicate_group` decision is authoritative: a
    passive rediscovery pass (backfill) rescanning the same pair later must
    not reopen it or touch the transactions it applies to."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família")
        db.add(household)
        db.flush()
        account = Account(household_id=household.id, name="Conta", owner_label="Família")
        admin = User(
            household_id=household.id,
            name="Admin",
            username="admin-discover",
            password_hash="hash",
            is_admin=True,
        )
        statement = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="f" * 64,
            encrypted_path="f.enc",
        )
        workbook = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="plan.xlsx",
            document_type="financial_plan_workbook",
            sha256="1" * 64,
            encrypted_path="g.enc",
        )
        db.add_all([account, admin, statement, workbook])
        db.flush()
        imported = _transaction(household.id, account.id, statement.id, 70)
        authoritative = _transaction(household.id, account.id, workbook.id, 100)
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
        imported_state = (imported.canonical_status, imported.excluded)
        authoritative_state = (authoritative.canonical_status, authoritative.excluded)

        rediscovered_group, _ = discover_transaction_duplicates(
            db, transaction=authoritative, household_id=household.id
        )

        assert rediscovered_group.id == group.id
        assert rediscovered_group.status == "resolved"
        assert rediscovered_group.resolution == "distinct"
        assert (imported.canonical_status, imported.excluded) == imported_state
        assert (authoritative.canonical_status, authoritative.excluded) == authoritative_state
