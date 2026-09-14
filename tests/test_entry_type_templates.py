"""Tests for the Assistente Financeiro's "Outra entrada"/"Outra saída"
learning mechanism (P0 #87, October Go-Live Slice 4 -- rebaseline §8.2/§9,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.6). Mirrors
`tests/test_classification_learning.py`'s structure -- this module copies
that same evidence/lifecycle model.
"""

import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-entry-templates-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "entry-type-template-test-secret-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base  # noqa: E402
from app.models import Category, Household, Transaction, User  # noqa: E402
from app.services.entry_type_templates import (  # noqa: E402
    EntryTypeTemplateError,
    accept_entry_type_template,
    deactivate_entry_type_template,
    list_entry_type_templates,
    record_observed_entry,
)


def _household_with_admin(db: Session, *, name: str = "Família") -> tuple[Household, User]:
    household = Household(name=name)
    db.add(household)
    db.flush()
    admin = User(
        household_id=household.id,
        name="Admin",
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash="hash",
        is_admin=True,
    )
    db.add(admin)
    db.flush()
    return household, admin


def _transaction_id(db: Session, *, household_id: str) -> str:
    from datetime import date
    from decimal import Decimal

    from sqlalchemy import select

    category = db.scalar(
        select(Category).where(Category.household_id == household_id, Category.name == "Receitas")
    )
    if category is None:
        category = Category(household_id=household_id, name="Receitas")
        db.add(category)
        db.flush()
    transaction = Transaction(
        household_id=household_id,
        category_id=category.id,
        booked_at=date(2026, 9, 1),
        description="Aluguel recebido",
        normalized_description="ALUGUEL RECEBIDO",
        amount=Decimal("1500.00"),
        transaction_type="income",
        fingerprint=f"entrytype{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=50,
        confidence=Decimal("1"),
        reviewed=True,
    )
    db.add(transaction)
    db.flush()
    return transaction.id


def test_template_requires_three_confirmations_and_explicit_admin_acceptance() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)

        transaction_ids = [_transaction_id(db, household_id=household.id) for _ in range(3)]
        for index, transaction_id in enumerate(transaction_ids):
            evidence = record_observed_entry(
                db,
                household_id=household.id,
                movement_type="income",
                label="Aluguel recebido",
                category_id=None,
                transaction_id=transaction_id,
            )
            if index < 2:
                assert evidence.active is False
        assert evidence.status == "pending_acceptance"
        assert evidence.confirmation_count == 3
        assert evidence.active is False

        template = accept_entry_type_template(
            db, household_id=household.id, template_id=evidence.template_id, user_id=admin.id
        )
        assert template.status == "active"
        assert template.active is True
        assert template.accepted_by == admin.id


def test_synonymous_labels_that_normalize_the_same_collapse_into_one_template() -> None:
    """Rebaseline §8.2 -- "Aluguel", "Recebi aluguel" and "Aluguel recebido"
    must not explode into distinct templates when they normalize the same
    way."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, _ = _household_with_admin(db)
        transaction_id_1 = _transaction_id(db, household_id=household.id)
        transaction_id_2 = _transaction_id(db, household_id=household.id)

        first = record_observed_entry(
            db,
            household_id=household.id,
            movement_type="income",
            label="aluguel recebido",
            category_id=None,
            transaction_id=transaction_id_1,
        )
        second = record_observed_entry(
            db,
            household_id=household.id,
            movement_type="income",
            label="ALUGUEL RECEBIDO",
            category_id=None,
            transaction_id=transaction_id_2,
        )
        assert first.template_id == second.template_id
        assert second.confirmation_count == 2


def test_distinct_label_is_a_distinct_template_never_silently_merged() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, _ = _household_with_admin(db)
        transaction_id_1 = _transaction_id(db, household_id=household.id)
        transaction_id_2 = _transaction_id(db, household_id=household.id)

        rent = record_observed_entry(
            db,
            household_id=household.id,
            movement_type="income",
            label="Aluguel recebido",
            category_id=None,
            transaction_id=transaction_id_1,
        )
        commission = record_observed_entry(
            db,
            household_id=household.id,
            movement_type="income",
            label="Reembolso viagem",
            category_id=None,
            transaction_id=transaction_id_2,
        )
        assert rent.template_id != commission.template_id


def test_template_never_activates_before_three_confirmations() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        transaction_id = _transaction_id(db, household_id=household.id)
        evidence = record_observed_entry(
            db,
            household_id=household.id,
            movement_type="income",
            label="Aluguel recebido",
            category_id=None,
            transaction_id=transaction_id,
        )
        try:
            accept_entry_type_template(
                db, household_id=household.id, template_id=evidence.template_id, user_id=admin.id
            )
            raised = False
        except EntryTypeTemplateError:
            raised = True
        assert raised


def test_deactivate_preserves_evidence_and_is_reversible_only_via_new_confirmations() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        for _ in range(3):
            transaction_id = _transaction_id(db, household_id=household.id)
            evidence = record_observed_entry(
                db,
                household_id=household.id,
                movement_type="income",
                label="Aluguel recebido",
                category_id=None,
                transaction_id=transaction_id,
            )
        accept_entry_type_template(
            db, household_id=household.id, template_id=evidence.template_id, user_id=admin.id
        )

        template = deactivate_entry_type_template(
            db, household_id=household.id, template_id=evidence.template_id
        )
        assert template.status == "inactive"
        assert template.active is False
        assert template.confirmation_count == 3  # evidence preserved, not reset


def test_templates_are_household_isolated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household_a, _ = _household_with_admin(db, name="Família A")
        household_b, _ = _household_with_admin(db, name="Família B")
        transaction_id = _transaction_id(db, household_id=household_a.id)
        record_observed_entry(
            db,
            household_id=household_a.id,
            movement_type="income",
            label="Aluguel recebido",
            category_id=None,
            transaction_id=transaction_id,
        )
        assert list_entry_type_templates(db, household_id=household_a.id)
        assert list_entry_type_templates(db, household_id=household_b.id) == []
