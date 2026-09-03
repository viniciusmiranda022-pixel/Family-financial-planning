"""Persistent, non-destructive duplicate detection and canonical precedence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.classifier import normalize_description

DUPLICATE_RULE_VERSION = "2026.09.1"
PROBABLE_THRESHOLD = Decimal("0.6000")
STRONG_THRESHOLD = Decimal("0.8500")


@dataclass(frozen=True, slots=True)
class DuplicateAssessment:
    confidence: Decimal
    band: str
    signals: dict[str, Any]


def source_priority(document_type: str | None) -> int:
    return {
        "financial_plan_workbook": 100,
        "manual": 90,
        "bank_statement": 70,
        "credit_card": 70,
        "payroll": 70,
        "capture": 60,
    }.get(document_type or "", 50)


def assess_duplicate(first: Any, second: Any) -> DuplicateAssessment:
    exact_fingerprint = bool(first.fingerprint and first.fingerprint == second.fingerprint)
    amount_equal = abs(Decimal(first.amount) - Decimal(second.amount)) <= Decimal("0.01")
    days_apart = abs((first.booked_at - second.booked_at).days)
    first_description = normalize_description(first.description)
    second_description = normalize_description(second.description)
    description_equal = first_description == second_description
    description_similarity = _token_similarity(first_description, second_description)
    account_equal = bool(first.account_id and first.account_id == second.account_id)
    installment_equal = (
        first.installment_current == second.installment_current
        and first.installment_total == second.installment_total
        and any(
            value is not None
            for value in (first.installment_current, first.installment_total)
        )
    )
    competence_equal = _competence(first) == _competence(second)

    if exact_fingerprint:
        confidence = Decimal("1.0000")
    else:
        confidence = Decimal("0")
        confidence += Decimal("0.25") if amount_equal else Decimal("0")
        confidence += Decimal("0.20") if days_apart == 0 else Decimal("0.10") if days_apart <= 3 else Decimal("0")
        confidence += (
            Decimal("0.25")
            if description_equal
            else Decimal("0.20") * description_similarity
        )
        confidence += Decimal("0.10") if account_equal else Decimal("0")
        confidence += Decimal("0.10") if installment_equal else Decimal("0")
        confidence += Decimal("0.05") if competence_equal else Decimal("0")
        confidence += Decimal("0.05") if first.document_id != second.document_id else Decimal("0")
        confidence = min(Decimal("1"), confidence)
    confidence = confidence.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    different_documents = first.document_id != second.document_id
    strong_evidence = exact_fingerprint or different_documents
    return DuplicateAssessment(
        confidence=confidence,
        band=(
            "strong"
            if confidence >= STRONG_THRESHOLD and strong_evidence
            else "probable"
            if confidence >= PROBABLE_THRESHOLD
            else "low"
        ),
        signals={
            "exact_fingerprint": exact_fingerprint,
            "amount_equal": amount_equal,
            "days_apart": days_apart,
            "description_equal": description_equal,
            "description_similarity": str(description_similarity),
            "account_equal": account_equal,
            "installment_equal": installment_equal,
            "competence_equal": competence_equal,
            "different_documents": different_documents,
            "strong_evidence": strong_evidence,
        },
    )


def register_transaction_duplicates(
    db: Session,
    *,
    transaction: Any,
    household_id: str,
) -> tuple[Any | None, DuplicateAssessment | None]:
    """Find the strongest bounded candidate, persist both sides of the
    derived group, and apply the resulting classification to the matched
    `Transaction` row(s) themselves.

    This is the live import/API path: `transaction` is either a row that
    just arrived through the pipeline or one a human is actively acting on,
    so immediately writing `canonical_status`/`possible_duplicate`/
    `excluded`/`duplicate_group_id` is the documented, intended effect, not
    a silent correction of settled history. Historical reprocessing must
    use `discover_transaction_duplicates` instead -- see its docstring.
    """

    return _match_and_persist_group(
        db, transaction=transaction, household_id=household_id, apply_to_transactions=True
    )


def discover_transaction_duplicates(
    db: Session,
    *,
    transaction: Any,
    household_id: str,
) -> tuple[Any | None, DuplicateAssessment | None]:
    """Non-mutating duplicate discovery for historical reprocessing.

    Runs the exact same candidate search and `assess_duplicate` scoring as
    `register_transaction_duplicates` -- the deterministic rule is never
    forked -- and persists the same *derived* evidence rows (`DuplicateGroup`,
    `DuplicateGroupMember`), so a discovered pair is immediately visible
    through `GET /duplicate-groups` like any other group.

    Unlike `register_transaction_duplicates`, it never writes to
    `transaction`'s own `canonical_status`, `possible_duplicate`, `excluded`
    or `duplicate_group_id` -- nor to those same columns on any matched
    transaction. Those columns are the system's applied classification of
    *existing*, already-published financial history; changing them changes
    which source is treated as canonical and whether a transaction is
    counted. Only a genuinely new transaction arriving through the live
    pipeline, or an explicit human decision
    (`resolve_duplicate_group`), may change them -- never an automatic
    historical backfill side effect (docs/INTEGRITY_IMPLEMENTATION_PLAN.md
    §0.2: "O backfill planejado deverá produzir findings sobre a base real
    sem corrigi-la silenciosamente").

    A group a human has already resolved is left untouched only while every
    transaction being examined is already a persisted member of it:
    rediscovering an already-resolved pair during a passive backfill pass is
    not new evidence and must not reopen that decision. A transaction that is
    *not* yet a member of that resolved group -- a genuinely new, unexamined
    row matching a pair a human already decided on -- is different: per
    docs/FINANCIAL_RULES.md ("Uma nova ocorrência compatível reabre o grupo
    para revisão sem apagar a resolução anterior, preservada nos sinais do
    grupo"), it reopens the group for review through the same
    reopen-on-new-matching-transaction lifecycle `register_transaction_duplicates`
    uses, so the new evidence is not silently lost. The full prior decision --
    `resolution`, `resolved_at`, `resolved_by` and `resolution_reason`, not
    just the resolution type and timestamp -- is preserved in `signals`
    (`previous_resolution`/`previous_resolved_at`/`previous_resolved_by`/
    `previous_resolution_reason`), and every past decision the group has ever
    had is additionally kept, oldest first, in an append-only
    `signals["resolution_history"]` list, so a group resolved and reopened
    more than once never loses an earlier human decision to a later one.
    Even then, discovery mode never mutates the source `Transaction` rows --
    only the derived `DuplicateGroup`/`DuplicateGroupMember` evidence
    changes.
    """

    return _match_and_persist_group(
        db, transaction=transaction, household_id=household_id, apply_to_transactions=False
    )


def _match_and_persist_group(
    db: Session,
    *,
    transaction: Any,
    household_id: str,
    apply_to_transactions: bool,
) -> tuple[Any | None, DuplicateAssessment | None]:
    """Shared candidate search, scoring and derived-evidence persistence for
    `register_transaction_duplicates` (`apply_to_transactions=True`) and
    `discover_transaction_duplicates` (`apply_to_transactions=False`). See
    those two docstrings for the behavioral contract each one promises.
    """

    from app.models import Document, DuplicateGroup, DuplicateGroupMember, Transaction

    start = transaction.booked_at - timedelta(days=3)
    end = transaction.booked_at + timedelta(days=3)
    candidates = tuple(
        db.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.id != transaction.id,
                Transaction.booked_at >= start,
                Transaction.booked_at <= end,
                Transaction.amount.between(
                    Decimal(transaction.amount) - Decimal("0.01"),
                    Decimal(transaction.amount) + Decimal("0.01"),
                ),
            )
        ).all()
    )
    assessments = sorted(
        ((candidate, assess_duplicate(candidate, transaction)) for candidate in candidates),
        key=lambda item: item[1].confidence,
        reverse=True,
    )
    if not assessments or assessments[0][1].confidence < PROBABLE_THRESHOLD:
        if apply_to_transactions:
            transaction.canonical_status = "unassigned"
        return None, assessments[0][1] if assessments else None

    existing, assessment = assessments[0]
    existing_document_type = db.scalar(
        select(Document.document_type).where(Document.id == existing.document_id)
    )
    candidate_document_type = db.scalar(
        select(Document.document_type).where(Document.id == transaction.document_id)
    )
    existing_priority = existing.source_priority or source_priority(existing_document_type)
    candidate_priority = transaction.source_priority or source_priority(candidate_document_type)
    canonical, supporting = (
        (transaction, existing)
        if candidate_priority > existing_priority
        else (existing, transaction)
    )

    group = None
    if existing.duplicate_group_id:
        group = db.get(DuplicateGroup, existing.duplicate_group_id)
    if group is None:
        group_key = _group_key(existing, transaction, assessment)
        group = db.scalar(
            select(DuplicateGroup).where(
                DuplicateGroup.household_id == household_id,
                DuplicateGroup.group_key == group_key,
            )
        )
        if group is None:
            group = DuplicateGroup(
                household_id=household_id,
                group_key=group_key,
                status="open",
                confidence=assessment.confidence,
                canonical_transaction_id=canonical.id,
                signals=assessment.signals,
                rule_version=DUPLICATE_RULE_VERSION,
            )
            db.add(group)
            db.flush()

    if not apply_to_transactions and group.status == "resolved":
        already_member = (
            db.scalar(
                select(DuplicateGroupMember.id).where(
                    DuplicateGroupMember.group_id == group.id,
                    DuplicateGroupMember.transaction_id == transaction.id,
                )
            )
            is not None
        )
        if already_member:
            # Rediscovering a pair a human already resolved is not new
            # evidence: passive rediscovery of an *existing* member of a
            # resolved group must not reopen that decision or touch the
            # transactions it applies to.
            return group, assessment
        # `transaction` is not yet a persisted member of this resolved
        # group -- i.e. a genuinely new (to this group), unexamined
        # transaction matches a pair a human already decided on.
        # docs/FINANCIAL_RULES.md, "Reconciliação, duplicidades e
        # anomalias": "Uma nova ocorrência compatível reabre o grupo para
        # revisão sem apagar a resolução anterior, preservada nos sinais do
        # grupo." That rule is not conditioned on the occurrence arriving
        # through the live pipeline vs. a backfill scan, so fall through
        # into the same reopen-on-new-matching-transaction lifecycle the
        # live path already uses below (`reopened_reason`, resolution
        # preserved in `signals`). `apply_to_transactions` still gates
        # every mutation of the source `Transaction` rows -- discovery mode
        # only ever persists derived `DuplicateGroup`/`DuplicateGroupMember`
        # evidence, never the transaction's own classification columns.

    # A candidate can point at any member of an existing group. Canonical
    # precedence must always compare the new row with the group's persisted
    # canonical row, never with whichever supporting member happened to win
    # the candidate query ordering.
    if group.canonical_transaction_id and group.canonical_transaction_id != transaction.id:
        persisted_canonical = db.get(Transaction, group.canonical_transaction_id)
        if persisted_canonical is not None:
            existing = persisted_canonical
            assessment = assess_duplicate(existing, transaction)
            existing_document_type = db.scalar(
                select(Document.document_type).where(Document.id == existing.document_id)
            )
            existing_priority = existing.source_priority or source_priority(existing_document_type)
            canonical, supporting = (
                (transaction, existing)
                if candidate_priority > existing_priority
                else (existing, transaction)
            )

    if group.status == "resolved":
        # 2026-09-03 review round 3, P1: `resolved_by`/`resolution_reason`
        # are as much a part of the human decision being reopened as
        # `resolution`/`resolved_at` -- docs/FINANCIAL_RULES.md requires the
        # *resolution* to be preserved, not just its type and timestamp.
        # Losing who decided and why is a silent loss of audit trail.
        # Because the same group can be resolved and reopened more than
        # once, a flat `previous_*` set of keys would itself be silently
        # overwritten by a second reopen. Every prior decision is instead
        # appended to an append-only `resolution_history` list, so no human
        # decision -- however many reopens later -- is ever dropped.
        previous_decision = {
            "resolution": group.resolution,
            "resolved_at": (
                group.resolved_at.isoformat() if group.resolved_at else None
            ),
            "resolved_by": group.resolved_by,
            "resolution_reason": group.resolution_reason,
            "reopened_reason": "new_matching_transaction",
        }
        existing_signals = dict(group.signals or {})
        resolution_history = [
            *list(existing_signals.get("resolution_history") or []),
            previous_decision,
        ]
        group.signals = {
            **existing_signals,
            "previous_resolution": previous_decision["resolution"],
            "previous_resolved_at": previous_decision["resolved_at"],
            "previous_resolved_by": previous_decision["resolved_by"],
            "previous_resolution_reason": previous_decision["resolution_reason"],
            "reopened_reason": "new_matching_transaction",
            "resolution_history": resolution_history,
        }
        group.status = "open"
        group.resolution = None
        group.resolved_at = None
        group.resolved_by = None
        group.resolution_reason = None

    group.confidence = max(Decimal(group.confidence), assessment.confidence)
    group.canonical_transaction_id = canonical.id
    group.signals = {**dict(group.signals or {}), **assessment.signals}
    strong = Decimal(group.confidence) >= STRONG_THRESHOLD and bool(
        (group.signals or {}).get("strong_evidence")
    )
    member_policies = (
        (
            canonical,
            "canonical" if strong else "candidate",
            max(existing_priority, candidate_priority),
            False,
        ),
        (
            supporting,
            "supporting" if strong else "candidate",
            min(existing_priority, candidate_priority),
            strong,
        ),
    )
    for member, role, priority, excluded_by_policy in member_policies:
        if apply_to_transactions:
            member.duplicate_group_id = group.id
            member.canonical_status = role if strong else "unassigned"
            member.possible_duplicate = role != "canonical"
            if excluded_by_policy:
                member.excluded = True
        stored_member = db.scalar(
            select(DuplicateGroupMember).where(
                DuplicateGroupMember.group_id == group.id,
                DuplicateGroupMember.transaction_id == member.id,
            )
        )
        if stored_member is None:
            db.add(
                DuplicateGroupMember(
                    household_id=household_id,
                    group_id=group.id,
                    transaction_id=member.id,
                    role=role,
                    confidence=assessment.confidence,
                    signals=assessment.signals,
                    source_priority=priority,
                    excluded_by_policy=excluded_by_policy,
                )
            )
        else:
            stored_member.role = role
            stored_member.confidence = assessment.confidence
            stored_member.signals = assessment.signals
            stored_member.source_priority = priority
            stored_member.excluded_by_policy = excluded_by_policy
    db.flush()
    return group, assessment


def resolve_duplicate_group(
    db: Session,
    *,
    group_id: str,
    household_id: str,
    resolution: str,
    user_id: str,
    reason: str,
    canonical_transaction_id: str | None = None,
) -> Any:
    from datetime import UTC, datetime

    from app.models import DuplicateGroup, DuplicateGroupMember, Transaction

    group = db.scalar(
        select(DuplicateGroup).where(
            DuplicateGroup.id == group_id,
            DuplicateGroup.household_id == household_id,
        )
    )
    if group is None:
        raise LookupError("duplicate group not found")
    members = tuple(
        db.scalars(
            select(DuplicateGroupMember).where(DuplicateGroupMember.group_id == group.id)
        ).all()
    )
    member_ids = {member.transaction_id for member in members}
    if resolution == "duplicate":
        canonical_id = canonical_transaction_id or group.canonical_transaction_id
        if canonical_id not in member_ids:
            raise ValueError("canonical transaction must belong to the group")
        group.canonical_transaction_id = canonical_id
        for member in members:
            transaction = db.get(Transaction, member.transaction_id)
            is_canonical = member.transaction_id == canonical_id
            was_excluded_by_policy = member.excluded_by_policy
            member.role = "canonical" if is_canonical else "supporting"
            member.excluded_by_policy = not is_canonical
            transaction.canonical_status = member.role
            transaction.possible_duplicate = False
            transaction.reviewed = True
            if is_canonical and was_excluded_by_policy:
                transaction.excluded = False
            elif not is_canonical:
                transaction.excluded = True
    elif resolution == "distinct":
        for member in members:
            transaction = db.get(Transaction, member.transaction_id)
            was_excluded_by_policy = member.excluded_by_policy
            member.role = "distinct"
            member.excluded_by_policy = False
            transaction.canonical_status = "distinct"
            transaction.possible_duplicate = False
            transaction.reviewed = True
            if was_excluded_by_policy:
                transaction.excluded = False
        group.canonical_transaction_id = None
    else:
        raise ValueError("unsupported duplicate resolution")
    group.status = "resolved"
    group.resolution = resolution
    group.resolved_at = datetime.now(UTC)
    group.resolved_by = user_id
    group.resolution_reason = reason
    return group


def serialize_duplicate_group(group: Any, members: list[Any]) -> dict[str, Any]:
    return {
        "id": group.id,
        "status": group.status,
        "confidence": str(group.confidence),
        "resolution": group.resolution,
        "canonical_transaction_id": group.canonical_transaction_id,
        "signals": group.signals,
        "rule_version": group.rule_version,
        "resolved_at": group.resolved_at.isoformat() if group.resolved_at else None,
        "resolved_by": group.resolved_by,
        "resolution_reason": group.resolution_reason,
        "members": [
            {
                "transaction_id": member.transaction_id,
                "role": member.role,
                "confidence": str(member.confidence),
                "signals": member.signals,
                "source_priority": member.source_priority,
                "excluded_by_policy": member.excluded_by_policy,
            }
            for member in members
        ],
    }


def _group_key(first: Any, second: Any, assessment: DuplicateAssessment) -> str:
    if assessment.signals["exact_fingerprint"]:
        return str(first.fingerprint)
    payload = json.dumps(
        {
            "amount": str(Decimal(first.amount).quantize(Decimal("0.01"))),
            "descriptions": sorted(
                {
                    normalize_description(first.description),
                    normalize_description(second.description),
                }
            ),
            "period": min(first.booked_at, second.booked_at).strftime("%Y-%m"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _competence(transaction: Any) -> str:
    return transaction.competence or transaction.booked_at.strftime("%Y-%m")


def _token_similarity(first: str, second: str) -> Decimal:
    first_tokens = set(first.split())
    second_tokens = set(second.split())
    union = first_tokens | second_tokens
    if not union:
        return Decimal("0")
    return (Decimal(len(first_tokens & second_tokens)) / Decimal(len(union))).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
