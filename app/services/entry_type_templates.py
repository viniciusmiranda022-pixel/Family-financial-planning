"""Learned "Outra entrada"/"Outra saída" types for the Assistente
Financeiro (P0 #87, October Go-Live Slice 4 -- rebaseline §8.2/§9,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.6).

Copies `app.services.classification_learning`'s exact evidence/lifecycle
model (`observed -> suggested -> pending_acceptance -> active`, admin-gated
activation) instead of a second learning engine: a template only ever
influences future presentation of a reusable label/category, never a
financial invariant -- creating or reusing one is never, by itself, a
mutation of any `Transaction`/`Obligation`/balance.

Deduplication key: `(household_id, normalized_label, movement_type)` --
`normalize_description` (the same normalizer `classification_learning`
already uses for merchants) means "Aluguel", "Recebi aluguel" and "Aluguel
recebido" only collapse together when they actually normalize the same way;
a genuinely different label is simply a different row, never silently
merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.classifier import normalize_description

_ALLOWED_MOVEMENT_TYPES = frozenset({"income", "expense"})


class EntryTypeTemplateError(ValueError):
    """A request that fails an entry-type-template precondition (unknown
    template, not yet eligible for activation, invalid movement_type)."""


@dataclass(frozen=True, slots=True)
class RecordedTemplateEvidence:
    template_id: str
    status: str
    confirmation_count: int
    active: bool


def record_observed_entry(
    db: Session,
    *,
    household_id: str,
    movement_type: str,
    label: str,
    category_id: str | None,
    transaction_id: str,
) -> RecordedTemplateEvidence:
    """Record one confirmed use of a custom "Outra entrada"/"Outra saída"
    label, creating the template on first use or accumulating evidence on a
    repeat. Never activates a template by itself -- see
    `accept_entry_type_template`, the same admin-gated barrier
    `classification_rules` already uses.
    """

    from app.models import EntryTypeTemplate

    if movement_type not in _ALLOWED_MOVEMENT_TYPES:
        raise EntryTypeTemplateError("movement_type deve ser income ou expense")
    clean_label = " ".join(label.split())[:100]
    if not clean_label:
        raise EntryTypeTemplateError("O rótulo do tipo não pode ser vazio")
    normalized = normalize_description(clean_label)
    if not normalized:
        raise EntryTypeTemplateError("O rótulo do tipo não pode ser vazio")

    template = db.scalar(
        select(EntryTypeTemplate).where(
            EntryTypeTemplate.household_id == household_id,
            EntryTypeTemplate.normalized_label == normalized,
            EntryTypeTemplate.movement_type == movement_type,
        )
    )
    if template is None:
        template = EntryTypeTemplate(
            household_id=household_id,
            movement_type=movement_type,
            label=clean_label,
            normalized_label=normalized,
            category_id=category_id,
            confirmation_count=1,
            status="observed",
            active=False,
            evidence={"transaction_ids": [transaction_id]},
        )
        db.add(template)
        db.flush()
        return RecordedTemplateEvidence(template.id, template.status, template.confirmation_count, template.active)

    evidence = dict(template.evidence or {})
    transaction_ids = list(evidence.get("transaction_ids", []))
    if transaction_id not in transaction_ids:
        transaction_ids.append(transaction_id)
        template.confirmation_count += 1
    template.evidence = {**evidence, "transaction_ids": transaction_ids[-20:]}
    if template.confirmation_count >= 3 and not template.active:
        template.status = "pending_acceptance"
    elif template.confirmation_count >= 2 and not template.active:
        template.status = "suggested"
    return RecordedTemplateEvidence(template.id, template.status, template.confirmation_count, template.active)


def accept_entry_type_template(db: Session, *, household_id: str, template_id: str, user_id: str) -> Any:
    from app.models import EntryTypeTemplate

    template = db.scalar(
        select(EntryTypeTemplate).where(
            EntryTypeTemplate.id == template_id, EntryTypeTemplate.household_id == household_id
        )
    )
    if template is None:
        raise LookupError("entry type template not found")
    if template.confirmation_count < 3 or template.status != "pending_acceptance":
        raise EntryTypeTemplateError("O tipo ainda não possui três confirmações consistentes")
    template.status = "active"
    template.active = True
    template.accepted_at = datetime.now(UTC)
    template.accepted_by = user_id
    return template


def deactivate_entry_type_template(db: Session, *, household_id: str, template_id: str) -> Any:
    from app.models import EntryTypeTemplate

    template = db.scalar(
        select(EntryTypeTemplate).where(
            EntryTypeTemplate.id == template_id, EntryTypeTemplate.household_id == household_id
        )
    )
    if template is None:
        raise LookupError("entry type template not found")
    if template.status != "active":
        raise EntryTypeTemplateError("Somente um tipo ativo pode ser desativado")
    template.status = "inactive"
    template.active = False
    return template


def list_entry_type_templates(
    db: Session, *, household_id: str, movement_type: str | None = None, active_only: bool = False
) -> list[Any]:
    from app.models import EntryTypeTemplate

    query = select(EntryTypeTemplate).where(EntryTypeTemplate.household_id == household_id)
    if movement_type is not None:
        query = query.where(EntryTypeTemplate.movement_type == movement_type)
    if active_only:
        query = query.where(EntryTypeTemplate.active.is_(True))
    query = query.order_by(EntryTypeTemplate.active.desc(), EntryTypeTemplate.confirmation_count.desc())
    return list(db.scalars(query))


def serialize_entry_type_template(template: Any) -> dict[str, Any]:
    return {
        "id": template.id,
        "movement_type": template.movement_type,
        "label": template.label,
        "category_id": template.category_id,
        "confirmation_count": template.confirmation_count,
        "status": template.status,
        "active": template.active,
        "accepted_at": template.accepted_at.isoformat() if template.accepted_at else None,
        "accepted_by": template.accepted_by,
    }
