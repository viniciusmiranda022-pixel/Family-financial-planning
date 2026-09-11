import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-learning-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "classification-learning-test-secret")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base
from app.models import Category, ClassificationRule, Household, Transaction, User
from app.services.classification_learning import (
    accept_classification_rule,
    classify_with_local_rules,
    deactivate_classification_rule,
    edit_classification_rule,
    record_confirmed_correction,
)
from app.services.classifier import normalize_description


def _household_with_admin(db: Session, *, household_name: str = "Família") -> tuple[Household, User]:
    household = Household(name=household_name)
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


def test_local_rule_never_overrides_structural_invariant_classification() -> None:
    """A household rule may only ever refine the *category* of an ordinary
    income/expense classification. It must never recategorize a transfer
    (INV-001), a card-payment reconciliation (INV-002), a patrimonial
    application/redemption movement (INV-003/INV-004) or a refund
    (INV-016) -- even when a rule happens to exist for the exact normalized
    description of a structurally-protected transaction. See
    docs/WORK_ORDER_EDITABLE_MERCHANT_RULES.md, acceptance criterion 6."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, _admin = _household_with_admin(db)
        category = Category(household_id=household.id, name="Categoria manipulada")
        db.add(category)
        db.flush()

        # Fabricated directly (not through `record_confirmed_correction`,
        # which never itself captures a protected movement_type -- see its
        # docstring) to prove the *apply-time* guard holds even against
        # data that should never occur through the normal lifecycle.
        protected_rule = ClassificationRule(
            household_id=household.id,
            normalized_merchant=normalize_description("PAGAMENTO FATURA NUBANK"),
            category_id=category.id,
            movement_type="expense",
            confirmation_count=3,
            status="active",
            active=True,
        )
        db.add(protected_rule)
        db.flush()

        result = classify_with_local_rules(
            db, household_id=household.id, description="PAGAMENTO FATURA NUBANK", amount=500.0
        )
        assert result.source == "builtin_rule"
        assert result.transaction_type == "reconciliation"
        assert result.category == "Conciliação"
        assert result.excluded is True


def test_local_rule_with_stale_protected_movement_type_is_ignored() -> None:
    """Defense in depth: even if a rule's *own* `movement_type` were ever
    protected (stale data, a future bug), applying it is refused and the
    deterministic classifier decides instead."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, _admin = _household_with_admin(db)
        category = Category(household_id=household.id, name="Categoria manipulada")
        db.add(category)
        db.flush()

        stale_rule = ClassificationRule(
            household_id=household.id,
            normalized_merchant=normalize_description("Loja Genérica"),
            category_id=category.id,
            movement_type="transfer",
            confirmation_count=3,
            status="active",
            active=True,
        )
        db.add(stale_rule)
        db.flush()

        result = classify_with_local_rules(
            db, household_id=household.id, description="Loja Genérica", amount=-50.0
        )
        assert result.source == "builtin_rule"
        assert result.transaction_type == "expense"


def test_local_rule_never_applies_across_incompatible_movement_type() -> None:
    """A rule's evidence proves its category only for the movement type it
    was actually confirmed against (`record_confirmed_correction` keys
    evidence by `(normalized_merchant, category_id, movement_type)`). An
    active rule learned from expense corrections must never rewrite a
    later positive (structurally `income`) event for the same normalized
    merchant into an expense, and symmetrically for an income rule against
    a negative event -- either direction would apply the rule's category
    to a movement type it never proved. See
    docs/WORK_ORDER_EDITABLE_MERCHANT_RULES.md, acceptance criteria 1/6."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        expense_category = Category(household_id=household.id, name="Papelaria")
        income_category = Category(household_id=household.id, name="Reembolsos recebidos")
        db.add_all([expense_category, income_category])
        db.flush()

        expense_rule = None
        for index in range(3):
            expense_rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"expense-{index}",
                description="Papelaria Central",
                category_id=expense_category.id,
                movement_type="expense",
            )
        accept_classification_rule(
            db, household_id=household.id, rule_id=expense_rule.id, user_id=admin.id
        )

        # The active expense rule applies to a genuine expense for the
        # same merchant...
        expense_event = classify_with_local_rules(
            db, household_id=household.id, description="Papelaria Central", amount=-45.0
        )
        assert expense_event.source == "local_rule"
        assert expense_event.category == "Papelaria"
        assert expense_event.transaction_type == "expense"

        # ...but must NOT rewrite a positive event for the same normalized
        # description: `classify()` deterministically resolves an
        # unrecognized positive amount to ordinary `income`, and the
        # expense rule's evidence never proved anything about an income
        # event.
        income_event = classify_with_local_rules(
            db, household_id=household.id, description="Papelaria Central", amount=45.0
        )
        assert income_event.source == "builtin_rule"
        assert income_event.transaction_type == "income"
        assert income_event.category != "Papelaria"

        income_rule = None
        for index in range(3):
            income_rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"income-{index}",
                description="Cliente Consultoria",
                category_id=income_category.id,
                movement_type="income",
            )
        accept_classification_rule(
            db, household_id=household.id, rule_id=income_rule.id, user_id=admin.id
        )

        applied_income = classify_with_local_rules(
            db, household_id=household.id, description="Cliente Consultoria", amount=1200.0
        )
        assert applied_income.source == "local_rule"
        assert applied_income.category == "Reembolsos recebidos"

        # Symmetric direction: a negative event for the same normalized
        # description must fall back to the deterministic expense
        # classification, never inherit the income rule's category.
        negative_event = classify_with_local_rules(
            db, household_id=household.id, description="Cliente Consultoria", amount=-1200.0
        )
        assert negative_event.source == "builtin_rule"
        assert negative_event.transaction_type == "expense"
        assert negative_event.category != "Reembolsos recebidos"


def test_classification_rule_lifecycle_deactivate_and_edit() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        original_category = Category(household_id=household.id, name="Transporte")
        corrected_category = Category(household_id=household.id, name="Trabalho")
        db.add_all([original_category, corrected_category])
        db.flush()

        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-{index}",
                description="Posto Avenida",
                category_id=original_category.id,
                movement_type="expense",
            )
        assert rule.status == "pending_acceptance"

        # Deactivating before acceptance is rejected: only an active rule
        # can be turned off.
        with pytest.raises(ValueError):
            deactivate_classification_rule(db, household_id=household.id, rule_id=rule.id)

        accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)
        applied = classify_with_local_rules(
            db, household_id=household.id, description="Posto Avenida", amount=-80.0
        )
        assert applied.category == "Transporte"
        assert applied.rule_id == rule.id

        deactivated = deactivate_classification_rule(db, household_id=household.id, rule_id=rule.id)
        assert deactivated.status == "inactive"
        assert deactivated.active is False
        # Evidence is preserved, not deleted, by deactivation.
        assert deactivated.confirmation_count == 3

        after_deactivation = classify_with_local_rules(
            db, household_id=household.id, description="Posto Avenida", amount=-80.0
        )
        assert after_deactivation.source == "builtin_rule"

        # An admin cannot simply flip a deactivated rule back on without
        # new evidence: it is not eligible for activation while `inactive`.
        with pytest.raises(ValueError):
            accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)

        # An inactive rule is still eligible for editing. Changing the
        # category must NOT let the three confirmations recorded for
        # "Transporte" stand in as proof for "Trabalho": that would let an
        # admin activate a category with zero confirmed corrections ever
        # supporting it. See docs/FINANCIAL_RULES.md and the Work Order's
        # lifecycle/evidence-model requirement.
        edited = edit_classification_rule(
            db, household_id=household.id, rule_id=rule.id, category_id=corrected_category.id
        )
        assert edited.category_id == corrected_category.id
        # movement_type is untouched by edit; only the category changes.
        assert edited.movement_type == "expense"
        assert edited.confirmation_count == 0
        assert edited.status == "observed"
        assert edited.active is False
        assert edited.accepted_at is None
        assert edited.accepted_by is None
        # The superseded evidence for "Transporte" is preserved, not
        # deleted, so the original three confirmations remain auditable.
        history = edited.evidence["history"]
        assert len(history) == 1
        assert history[0]["category_id"] == original_category.id
        assert history[0]["confirmation_count"] == 3
        assert len(history[0]["transaction_ids"]) == 3
        # Each history entry carries only its own bounded facts, never the
        # raw evidence blob it was cut from -- see
        # test_classification_rule_edit_history_does_not_nest_recursively.
        assert "evidence" not in history[0]
        assert "history" not in history[0]

        # The regression this guards against: three confirmations for
        # category A must never activate category B. With zero
        # confirmations recorded for "Trabalho", acceptance is rejected.
        with pytest.raises(ValueError):
            accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)
        still_builtin = classify_with_local_rules(
            db, household_id=household.id, description="Posto Avenida", amount=-80.0
        )
        assert still_builtin.source == "builtin_rule"

        # Only three genuinely new, distinct confirmed corrections for the
        # edited category re-promote it to `pending_acceptance`.
        for index in (1, 2):
            record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-trabalho-{index}",
                description="Posto Avenida",
                category_id=corrected_category.id,
                movement_type="expense",
            )
        with pytest.raises(ValueError):
            accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)
        reconfirmed = record_confirmed_correction(
            db,
            household_id=household.id,
            transaction_id="tx-trabalho-3",
            description="Posto Avenida",
            category_id=corrected_category.id,
            movement_type="expense",
        )
        assert reconfirmed.status == "pending_acceptance"
        assert reconfirmed.confirmation_count == 3
        accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)
        assert rule.status == "active"
        applied_after_edit = classify_with_local_rules(
            db, household_id=household.id, description="Posto Avenida", amount=-80.0
        )
        assert applied_after_edit.category == "Trabalho"


def test_classification_rule_edit_history_does_not_nest_recursively() -> None:
    """Repeated admin edits must keep serialized evidence growth linear.

    `edit_classification_rule` preserves each superseded version under
    `evidence["history"]`. Before the fix, the history entry captured the
    *entire* prior `evidence` blob -- which, from the second edit onward,
    already contained its own `"history"` key. Each subsequent edit then
    nested another full copy of all earlier history inside the new entry
    while also keeping it at the top level, so the JSON payload grew
    roughly multiplicatively per edit. This proves three successive edits
    keep `history` at exactly one entry per edit, with no entry embedding
    a `"history"` or raw `"evidence"` sub-tree.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        categories = [
            Category(household_id=household.id, name=name)
            for name in ("Transporte", "Trabalho", "Lazer", "Alimentação")
        ]
        db.add_all(categories)
        db.flush()

        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-0-{index}",
                description="Loja Multiuso",
                category_id=categories[0].id,
                movement_type="expense",
            )
        accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)

        for step, next_category in enumerate(categories[1:], start=1):
            edited = edit_classification_rule(
                db, household_id=household.id, rule_id=rule.id, category_id=next_category.id
            )
            history = edited.evidence["history"]
            assert len(history) == step
            for entry in history:
                assert "history" not in entry
                assert "evidence" not in entry
            # Earn three fresh confirmations so the next edit starts from
            # an eligible (non-`observed`) status, matching real usage.
            for index in (1, 2, 3):
                record_confirmed_correction(
                    db,
                    household_id=household.id,
                    transaction_id=f"tx-{step}-{index}",
                    description="Loja Multiuso",
                    category_id=next_category.id,
                    movement_type="expense",
                )

        assert len(rule.evidence["history"]) == 3
        assert [entry["category_id"] for entry in rule.evidence["history"]] == [
            categories[0].id,
            categories[1].id,
            categories[2].id,
        ]


def test_classification_rule_edit_rejects_unknown_category() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, _admin = _household_with_admin(db)
        category = Category(household_id=household.id, name="Transporte")
        db.add(category)
        db.flush()
        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-{index}",
                description="Posto Avenida",
                category_id=category.id,
                movement_type="expense",
            )
        with pytest.raises(LookupError):
            edit_classification_rule(
                db, household_id=household.id, rule_id=rule.id, category_id="does-not-exist"
            )


def test_classification_rule_actions_are_household_isolated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household_a, _admin_a = _household_with_admin(db, household_name="Família A")
        household_b, admin_b = _household_with_admin(db, household_name="Família B")
        category_a = Category(household_id=household_a.id, name="Categoria A")
        db.add(category_a)
        db.flush()

        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household_a.id,
                transaction_id=f"tx-{index}",
                description="Loja X",
                category_id=category_a.id,
                movement_type="expense",
            )
        assert rule.status == "pending_acceptance"

        with pytest.raises(LookupError):
            accept_classification_rule(
                db, household_id=household_b.id, rule_id=rule.id, user_id=admin_b.id
            )
        with pytest.raises(LookupError):
            deactivate_classification_rule(db, household_id=household_b.id, rule_id=rule.id)
        with pytest.raises(LookupError):
            edit_classification_rule(
                db, household_id=household_b.id, rule_id=rule.id, category_id=category_a.id
            )

        accept_classification_rule(db, household_id=household_a.id, rule_id=rule.id, user_id=admin_b.id)

        # A different household's active rule must never classify a
        # transaction for this household, even with an identical merchant
        # description.
        result = classify_with_local_rules(
            db, household_id=household_b.id, description="Loja X", amount=-40.0
        )
        assert result.source == "builtin_rule"


def test_rule_activation_and_edit_never_mutate_existing_transactions() -> None:
    """Activating, editing or deactivating a rule must never touch
    `Transaction` rows -- classification only changes for future events
    (acceptance criterion 3)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        original_category = Category(household_id=household.id, name="Transporte")
        corrected_category = Category(household_id=household.id, name="Trabalho")
        db.add_all([original_category, corrected_category])
        db.flush()

        historical = Transaction(
            household_id=household.id,
            account_id=None,
            category_id=original_category.id,
            booked_at=date(2026, 8, 1),
            description="Posto Avenida",
            normalized_description=normalize_description("Posto Avenida"),
            amount=-80,
            transaction_type="expense",
            owner_label="Família",
            fingerprint="a" * 64,
        )
        db.add(historical)
        db.flush()
        before = {
            "category_id": historical.category_id,
            "transaction_type": historical.transaction_type,
            "amount": str(historical.amount),
            "description": historical.description,
        }

        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-{index}",
                description="Posto Avenida",
                category_id=original_category.id,
                movement_type="expense",
            )
        accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)
        # Deactivate before editing: editing resets confirmation evidence
        # for the new category (see test_classification_rule_lifecycle_
        # deactivate_and_edit), so it no longer leaves the rule `active`
        # for `deactivate_classification_rule` to act on afterward.
        deactivate_classification_rule(db, household_id=household.id, rule_id=rule.id)
        edit_classification_rule(
            db, household_id=household.id, rule_id=rule.id, category_id=corrected_category.id
        )

        after = {
            "category_id": historical.category_id,
            "transaction_type": historical.transaction_type,
            "amount": str(historical.amount),
            "description": historical.description,
        }
        assert after == before


def test_smart_capture_document_row_routes_through_active_household_rule() -> None:
    """Smart capture's structured-document row classifier must resolve to
    the same active household rule the importer uses when given the same
    database session and household -- one canonical classification path,
    not a second policy (acceptance criterion 4). HTTP-level proof through
    the real `/api/captures/preview` endpoint lives in
    `tests/test_classification_rules_api.py`; this is the fast, direct
    unit of that same wiring."""
    from app.services.importer import ParsedTransaction
    from app.services.smart_capture import _parsed_transaction_item

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        household, admin = _household_with_admin(db)
        category = Category(household_id=household.id, name="Papelaria")
        db.add(category)
        db.flush()

        rule = None
        for index in (1, 2, 3):
            rule = record_confirmed_correction(
                db,
                household_id=household.id,
                transaction_id=f"tx-{index}",
                description="Papelaria Central",
                category_id=category.id,
                movement_type="expense",
            )
        accept_classification_rule(db, household_id=household.id, rule_id=rule.id, user_id=admin.id)

        item = ParsedTransaction(date(2026, 8, 1), "Papelaria Central", Decimal("-45"), 1)
        with_rule = _parsed_transaction_item(item, db=db, household_id=household.id)
        assert with_rule["category_name"] == "Papelaria"

        without_context = _parsed_transaction_item(item)
        assert without_context["category_name"] != "Papelaria"
