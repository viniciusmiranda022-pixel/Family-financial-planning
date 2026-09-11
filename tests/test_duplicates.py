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
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

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


def test_probable_same_document_match_excludes_supporting_side_pending_resolution() -> None:
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
            username="admin-probable-same-document",
            password_hash="hash",
            is_admin=True,
        )
        document = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="c" * 64,
            encrypted_path="c.enc",
        )
        db.add_all([account, admin, document])
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
        # INV-014: confidence >= 0.60 pending resolution must keep totals
        # single-counted. Equal source priority (both `bank_statement`)
        # elects the first-registered row (`first`) canonical via the
        # existing-vs-new tie-break in `_match_and_persist_group`; `second`
        # -- the supporting side -- is excluded from totals until a human
        # resolves the group, without either row being deleted or its
        # `possible_duplicate` review flag skipped.
        assert first.excluded is False
        assert second.excluded is True
        assert first.canonical_status == "unassigned"
        assert second.canonical_status == "unassigned"
        assert first.possible_duplicate is True
        assert second.possible_duplicate is True

        resolve_duplicate_group(
            db,
            group_id=group.id,
            household_id=household.id,
            resolution="distinct",
            user_id=admin.id,
            reason="Compras distintas confirmadas manualmente",
        )
        # A human resolving the pair as genuinely distinct restores both to
        # totals -- the automatic exclusion never destroys evidence or
        # forecloses the "these are two real purchases" outcome.
        assert first.excluded is False
        assert second.excluded is False


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


def test_discover_transaction_duplicates_reopens_a_resolved_group_for_a_new_member() -> None:
    """2026-09-03 review round 2, P1: a resolved group must stay untouched
    only when the transaction being examined is *already* one of its
    persisted members (covered above). A different, not-yet-examined
    transaction that matches the same resolved pair is genuinely new
    evidence -- docs/FINANCIAL_RULES.md requires it to reopen the group for
    review, preserving the prior resolution in `signals`, exactly like the
    live path's reopen-on-new-matching-transaction lifecycle. The passive
    early return must not silently drop that membership, or INV-014 would
    never see it."""

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
            username="admin-reopen",
            password_hash="hash",
            is_admin=True,
        )
        statement = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="statement.csv",
            document_type="bank_statement",
            sha256="2" * 64,
            encrypted_path="h.enc",
        )
        workbook = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="plan.xlsx",
            document_type="financial_plan_workbook",
            sha256="3" * 64,
            encrypted_path="i.enc",
        )
        capture = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="capture.json",
            document_type="capture",
            sha256="4" * 64,
            encrypted_path="j.enc",
        )
        db.add_all([account, admin, statement, workbook, capture])
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
        imported_state = (imported.canonical_status, imported.possible_duplicate, imported.excluded)
        authoritative_state = (
            authoritative.canonical_status,
            authoritative.possible_duplicate,
            authoritative.excluded,
        )

        # A third, unexamined transaction -- e.g. a legacy row a backfill
        # pass is scanning for the first time -- matches the same pair.
        late_evidence = _transaction(household.id, account.id, capture.id, 60)
        db.add(late_evidence)
        db.flush()

        reopened_group, assessment = discover_transaction_duplicates(
            db, transaction=late_evidence, household_id=household.id
        )

        assert reopened_group.id == group.id
        assert assessment is not None
        # The group is reopened for review; the prior human resolution is
        # preserved as audit history, never erased.
        assert reopened_group.status == "open"
        assert reopened_group.resolution is None
        assert reopened_group.signals["previous_resolution"] == "distinct"
        assert reopened_group.signals["reopened_reason"] == "new_matching_transaction"
        # 2026-09-03 review round 3, P1: who resolved it and why are part of
        # the human decision being reopened, not just the resolution type
        # and timestamp -- both must survive the reopen.
        assert reopened_group.signals["previous_resolved_by"] == admin.id
        assert (
            reopened_group.signals["previous_resolution_reason"]
            == "Movimentos distintos confirmados"
        )
        assert reopened_group.signals["resolution_history"] == [
            {
                "resolution": "distinct",
                "resolved_at": reopened_group.signals["previous_resolved_at"],
                "resolved_by": admin.id,
                "resolution_reason": "Movimentos distintos confirmados",
                "reopened_reason": "new_matching_transaction",
            }
        ]

        # The new evidence is deterministically surfaced as derived
        # membership -- this is exactly what INV-014's open-group-member
        # check relies on to flag it.
        members = db.scalars(
            select(DuplicateGroupMember).where(DuplicateGroupMember.group_id == group.id)
        ).all()
        assert late_evidence.id in {member.transaction_id for member in members}

        # No source `Transaction` row -- old members or the new one -- is
        # ever mutated by discovery mode.
        assert (
            imported.canonical_status,
            imported.possible_duplicate,
            imported.excluded,
        ) == imported_state
        assert (
            authoritative.canonical_status,
            authoritative.possible_duplicate,
            authoritative.excluded,
        ) == authoritative_state
        assert late_evidence.canonical_status == "unassigned"
        assert late_evidence.possible_duplicate is False
        assert late_evidence.excluded is False
        assert late_evidence.duplicate_group_id is None

        # A second human resolves the reopened group -- a different admin,
        # a different resolution, a different reason -- and a fourth,
        # still-unexamined transaction later surfaces matching evidence.
        # Both prior human decisions must remain auditable, not just the
        # immediately preceding one.
        second_admin = User(
            household_id=household.id,
            name="Segunda Administradora",
            username="admin-reopen-2",
            password_hash="hash",
            is_admin=True,
        )
        capture_2 = Document(
            household_id=household.id,
            account_id=account.id,
            original_name="capture-2.json",
            document_type="capture",
            sha256="5" * 64,
            encrypted_path="k.enc",
        )
        db.add_all([second_admin, capture_2])
        db.flush()

        resolve_duplicate_group(
            db,
            group_id=reopened_group.id,
            household_id=household.id,
            resolution="duplicate",
            user_id=second_admin.id,
            reason="Confirmado como lançamento duplicado na revisão",
            canonical_transaction_id=authoritative.id,
        )
        assert reopened_group.status == "resolved"
        assert reopened_group.resolution == "duplicate"
        first_resolved_at = reopened_group.signals["previous_resolved_at"]
        second_resolved_at = reopened_group.resolved_at.isoformat()

        even_later_evidence = _transaction(household.id, account.id, capture_2.id, 60)
        db.add(even_later_evidence)
        db.flush()

        twice_reopened_group, second_assessment = discover_transaction_duplicates(
            db, transaction=even_later_evidence, household_id=household.id
        )

        assert twice_reopened_group.id == group.id
        assert second_assessment is not None
        assert twice_reopened_group.status == "open"
        assert twice_reopened_group.resolution is None
        # The most recent decision is surfaced as `previous_*`...
        assert twice_reopened_group.signals["previous_resolution"] == "duplicate"
        assert twice_reopened_group.signals["previous_resolved_by"] == second_admin.id
        assert (
            twice_reopened_group.signals["previous_resolution_reason"]
            == "Confirmado como lançamento duplicado na revisão"
        )
        assert twice_reopened_group.signals["previous_resolved_at"] == second_resolved_at
        # ...but neither human decision is lost: both remain fully
        # inspectable, oldest first, in the append-only history.
        assert twice_reopened_group.signals["resolution_history"] == [
            {
                "resolution": "distinct",
                "resolved_at": first_resolved_at,
                "resolved_by": admin.id,
                "resolution_reason": "Movimentos distintos confirmados",
                "reopened_reason": "new_matching_transaction",
            },
            {
                "resolution": "duplicate",
                "resolved_at": second_resolved_at,
                "resolved_by": second_admin.id,
                "resolution_reason": "Confirmado como lançamento duplicado na revisão",
                "reopened_reason": "new_matching_transaction",
            },
        ]

        # Discovery mode still never mutates the source `Transaction` rows,
        # even across repeated resolve/reopen cycles -- only the explicit
        # human `resolve_duplicate_group` call above (not discovery) may
        # touch `imported`/`authoritative`'s classification columns.
        assert even_later_evidence.canonical_status == "unassigned"
        assert even_later_evidence.possible_duplicate is False
        assert even_later_evidence.excluded is False
        assert even_later_evidence.duplicate_group_id is None
