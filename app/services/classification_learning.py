"""Deterministic local classification rules learned only from confirmed corrections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.classifier import Classification, classify, normalize_description

CLASSIFICATION_RULES_VERSION = "2026.09.1"

# `classify()` (app/services/classifier.py) assigns these transaction types
# only through structural, invariant-bound detection: PAYMENT_PATTERN (card
# bill payment -> INV-002 reconciliation), the PRIVILEGE/RESGATE pattern and
# the internal-alias PIX/TED/TRANSF match (INV-001 transfer, INV-003/004
# patrimonial movement), and REFUND_PATTERN plus the positive-amount
# expense-to-refund flip (INV-016 refund). A household local merchant rule
# (docs/WORK_ORDER_EDITABLE_MERCHANT_RULES.md) may only ever refine the
# *category* of an ordinary income/expense classification; it must never be
# allowed to override one of these structural results, or an admin-accepted
# rule could silently defeat a financial invariant for every future
# transaction that happens to share its normalized description.
_INVARIANT_PROTECTED_TRANSACTION_TYPES = frozenset({"transfer", "reconciliation", "refund"})

# Statuses in which an administrator may act on a rule at all: the rule has
# already cleared the three-distinct-confirmation evidence bar (or was
# active and is now being reconsidered). `observed`/`suggested` are still
# organically accumulating evidence and are not yet a governance surface.
_ADMIN_ELIGIBLE_STATUSES = frozenset({"pending_acceptance", "active", "inactive"})


@dataclass(frozen=True, slots=True)
class AppliedClassification:
    category: str
    transaction_type: str
    excluded: bool
    confidence: float
    review_reason: str | None
    source: str
    version: str
    rule_id: str | None = None


def classify_with_local_rules(
    db: Session,
    *,
    household_id: str,
    description: str,
    amount: float,
    internal_aliases: tuple[str, ...] = (),
) -> AppliedClassification:
    from app.models import Category, ClassificationRule

    fallback = classify(description, amount, internal_aliases)
    if fallback.transaction_type in _INVARIANT_PROTECTED_TRANSACTION_TYPES:
        # Deterministic structural classification wins unconditionally: a
        # household rule can never recategorize a transfer, a card-payment
        # reconciliation or a refund (see module docstring/constant above).
        return _from_fallback(fallback)

    normalized = normalize_description(description)
    row = db.execute(
        select(ClassificationRule, Category.name)
        .join(Category, Category.id == ClassificationRule.category_id)
        .where(
            ClassificationRule.household_id == household_id,
            ClassificationRule.normalized_merchant == normalized,
            ClassificationRule.active.is_(True),
            ClassificationRule.status == "active",
        )
        .order_by(ClassificationRule.priority.desc(), ClassificationRule.updated_at.desc())
        .limit(1)
    ).first()
    if row and row[0].movement_type not in _INVARIANT_PROTECTED_TRANSACTION_TYPES:
        rule, category_name = row
        return AppliedClassification(
            category=category_name,
            transaction_type=rule.movement_type,
            excluded=rule.movement_type in {"transfer", "reconciliation"},
            confidence=0.99,
            review_reason=None,
            source="local_rule",
            version=CLASSIFICATION_RULES_VERSION,
            rule_id=rule.id,
        )
    return _from_fallback(fallback)


def record_confirmed_correction(
    db: Session,
    *,
    household_id: str,
    transaction_id: str,
    description: str,
    category_id: str,
    movement_type: str,
) -> Any:
    from app.models import ClassificationRule

    normalized = normalize_description(description)
    if not normalized:
        raise ValueError("merchant description cannot be empty")
    rule = db.scalar(
        select(ClassificationRule).where(
            ClassificationRule.household_id == household_id,
            ClassificationRule.normalized_merchant == normalized,
            ClassificationRule.category_id == category_id,
            ClassificationRule.movement_type == movement_type,
        )
    )
    if rule is None:
        rule = ClassificationRule(
            household_id=household_id,
            normalized_merchant=normalized,
            category_id=category_id,
            movement_type=movement_type,
            confirmation_count=1,
            status="observed",
            active=False,
            evidence={"transaction_ids": [transaction_id]},
        )
        db.add(rule)
        db.flush()
        return rule

    evidence = dict(rule.evidence or {})
    transaction_ids = list(evidence.get("transaction_ids", []))
    if transaction_id not in transaction_ids:
        transaction_ids.append(transaction_id)
        rule.confirmation_count += 1
    rule.evidence = {**evidence, "transaction_ids": transaction_ids[-20:]}
    rule.last_confirmed_at = datetime.now(UTC)
    if rule.confirmation_count >= 3 and not rule.active:
        rule.status = "pending_acceptance"
    elif rule.confirmation_count >= 2 and not rule.active:
        rule.status = "suggested"
    return rule


def accept_classification_rule(
    db: Session,
    *,
    household_id: str,
    rule_id: str,
    user_id: str,
) -> Any:
    from app.models import ClassificationRule

    rule = db.scalar(
        select(ClassificationRule).where(
            ClassificationRule.id == rule_id,
            ClassificationRule.household_id == household_id,
        )
    )
    if rule is None:
        raise LookupError("classification rule not found")
    if rule.confirmation_count < 3 or rule.status != "pending_acceptance":
        raise ValueError("classification rule does not have three consistent confirmations")
    rule.status = "active"
    rule.active = True
    rule.accepted_at = datetime.now(UTC)
    rule.accepted_by = user_id
    return rule


def deactivate_classification_rule(
    db: Session,
    *,
    household_id: str,
    rule_id: str,
) -> Any:
    """Turn off an active local rule for future classification only.

    This never touches `Transaction` rows: turning a rule off changes only
    `classification_rules.status`/`active`, so nothing that was already
    classified is recategorized. Deactivation is deliberately one-way from
    the admin's perspective -- it does not reset `confirmation_count` or
    `evidence`, so the historical evidence stays intact and auditable, but
    an admin cannot simply flip it back on without new evidence: a rule can
    only return to `pending_acceptance` (and from there be activated again
    through `accept_classification_rule`) when `record_confirmed_correction`
    observes a genuinely new confirmed correction (see the `not rule.active`
    branch above), never as a side effect of this function.
    """
    from app.models import ClassificationRule

    rule = db.scalar(
        select(ClassificationRule).where(
            ClassificationRule.id == rule_id,
            ClassificationRule.household_id == household_id,
        )
    )
    if rule is None:
        raise LookupError("classification rule not found")
    if rule.status != "active":
        raise ValueError("only an active classification rule can be deactivated")
    rule.status = "inactive"
    rule.active = False
    return rule


def edit_classification_rule(
    db: Session,
    *,
    household_id: str,
    rule_id: str,
    category_id: str,
) -> Any:
    """Let an administrator correct the category an eligible rule applies.

    `movement_type` is intentionally not editable here: it is inherited,
    at correction time, from the deterministic `transaction_type` the
    built-in classifier (or an earlier local rule) already assigned to the
    confirming transaction (see `record_confirmed_correction` and the
    `TransactionUpdate` schema, which has no `transaction_type` field). If
    an administrator could freely set `movement_type` through this edit
    surface, a rule could be turned into a transfer/reconciliation/refund
    rule that `classify_with_local_rules` would then have to reject anyway
    (its invariant-protection guard), or worse, into a category that
    contradicts the rule's own evidence. Only the category is genuinely
    editable without touching financial semantics.

    Changing the category resets the evidence threshold. `evidence` and
    `confirmation_count` are keyed, via `record_confirmed_correction`, to
    the exact `(normalized_merchant, category_id, movement_type)` outcome
    the confirmations were recorded against -- they prove that outcome and
    nothing else. If an edit that changes `category_id` kept the old
    `confirmation_count`/`status`/`active`, an admin could turn three
    confirmed corrections for category A into an immediately-active rule
    for category B with zero confirmed corrections ever supporting B,
    which violates the normative three-distinct-confirmation threshold
    (`docs/FINANCIAL_RULES.md`) and defeats the acceptance evidence model.
    So a category change here always resets `confirmation_count` to 0,
    `status` to `observed`, `active` to `False`, and clears
    `accepted_at`/`accepted_by`: the edited rule must earn its own three
    distinct confirmed corrections and a fresh administrator acceptance
    before it can apply again, exactly like a new rule. The prior
    evidence is never deleted or overwritten -- it is preserved under
    `evidence["history"]` for audit purposes. A no-op edit (the requested
    category is already the current one) changes nothing.
    """
    from app.models import Category, ClassificationRule

    rule = db.scalar(
        select(ClassificationRule).where(
            ClassificationRule.id == rule_id,
            ClassificationRule.household_id == household_id,
        )
    )
    if rule is None:
        raise LookupError("classification rule not found")
    if rule.status not in _ADMIN_ELIGIBLE_STATUSES:
        raise ValueError("classification rule is not yet eligible for administrator review")
    category = db.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.household_id == household_id,
        )
    )
    if category is None:
        raise LookupError("category not found")
    if category.id == rule.category_id:
        return rule

    history_entry = {
        "category_id": rule.category_id,
        "confirmation_count": rule.confirmation_count,
        "status": rule.status,
        "active": rule.active,
        "evidence": rule.evidence,
        "accepted_at": rule.accepted_at.isoformat() if rule.accepted_at else None,
        "accepted_by": rule.accepted_by,
        "superseded_at": datetime.now(UTC).isoformat(),
    }
    history = list((rule.evidence or {}).get("history", []))
    history.append(history_entry)

    rule.category_id = category.id
    rule.confirmation_count = 0
    rule.status = "observed"
    rule.active = False
    rule.accepted_at = None
    rule.accepted_by = None
    rule.last_confirmed_at = datetime.now(UTC)
    rule.evidence = {"transaction_ids": [], "history": history}
    return rule


def serialize_classification_rule(rule: Any, category_name: str) -> dict[str, Any]:
    return {
        "id": rule.id,
        "normalized_merchant": rule.normalized_merchant,
        "category_id": rule.category_id,
        "category": category_name,
        "movement_type": rule.movement_type,
        "confirmation_count": rule.confirmation_count,
        "status": rule.status,
        "priority": rule.priority,
        "active": rule.active,
        "created_from_correction": rule.created_from_correction,
        "last_confirmed_at": rule.last_confirmed_at.isoformat(),
        "accepted_at": rule.accepted_at.isoformat() if rule.accepted_at else None,
        "accepted_by": rule.accepted_by,
    }


def _from_fallback(result: Classification) -> AppliedClassification:
    return AppliedClassification(
        category=result.category,
        transaction_type=result.transaction_type,
        excluded=result.excluded,
        confidence=result.confidence,
        review_reason=result.review_reason,
        source="builtin_rule",
        version=CLASSIFICATION_RULES_VERSION,
    )
