import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-learning-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "classification-learning-test-secret")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base
from app.models import Category, Household, User
from app.services.classification_learning import (
    accept_classification_rule,
    classify_with_local_rules,
    record_confirmed_correction,
)


def test_rule_requires_three_distinct_confirmations_and_explicit_acceptance() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household = Household(name="Família")
        db.add(household)
        db.flush()
        category = Category(household_id=household.id, name="Transporte")
        admin = User(
            household_id=household.id,
            name="Admin",
            username="admin-learning",
            password_hash="hash",
            is_admin=True,
        )
        db.add_all([category, admin])
        db.flush()

        for index, expected in ((1, "observed"), (2, "suggested"), (3, "pending_acceptance")):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"transaction-{index}",
                description="Posto Avenida",
                category_id=category.id,
                movement_type="expense",
            )
            assert rule.confirmation_count == index
            assert rule.status == expected
            assert rule.active is False

        before_acceptance = classify_with_local_rules(
            db,
            household_id=household.id,
            description="Posto Avenida",
            amount=-100,
        )
        assert before_acceptance.source == "builtin_rule"

        accept_classification_rule(
            db,
            household_id=household.id,
            rule_id=rule.id,
            user_id=admin.id,
        )
        applied = classify_with_local_rules(
            db,
            household_id=household.id,
            description="Posto Avenida",
            amount=-100,
        )
        assert applied.source == "local_rule"
        assert applied.category == "Transporte"
        assert applied.rule_id == rule.id

        # Reusing the same evidence never manufactures another confirmation.
        repeated = record_confirmed_correction(
            db,
            household_id=household.id,
            transaction_id="transaction-3",
            description="Posto Avenida",
            category_id=category.id,
            movement_type="expense",
        )
        assert repeated.confirmation_count == 3
