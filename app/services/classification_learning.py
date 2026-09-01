"""Deterministic local classification rules learned only from confirmed corrections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.classifier import Classification, classify, normalize_description

CLASSIFICATION_RULES_VERSION = "2026.09.1"


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
    if row:
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
    fallback = classify(description, amount, internal_aliases)
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
