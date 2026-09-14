"""Assistente Financeiro typed-action dispatcher (P0 #87, October Go-Live
Slice 4 -- `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_4.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.5/§4).

This module is the only place that turns a Codex NLU interpretation
(`app.services.assistant_interpreter.StructuredInterpretation`) into a
concrete, executable typed-action proposal, and the only place that
dispatches a confirmed typed action to a real write.

Architecture (rebaseline §11.2 -- "o backend permanece autoridade de
escrita/cálculo determinístico"):

- `build_typed_action_proposal` is read-only: it resolves Codex's free-text
  hints (`account_hint`, `category_hint`, ...) against real household data
  using plain, deterministic, household-scoped queries -- never a guess, and
  never a write. When a hint does not resolve to exactly one candidate, or a
  materially required field is still missing, it returns a clarifying
  question instead of a proposal. A category is the one intentional
  exception: it is passed through as free text (`category_name`) for
  `create_manual_transaction` to resolve at execute time, exactly the same
  way a human typing a new category name in the manual-entry form already
  works -- see its own docstring below.
- `execute_typed_action` never re-implements financial logic: every typed
  action is a direct, in-process call into the exact same endpoint function
  (`app.api.create_manual_transaction`, `.create_manual_transfer`,
  `.pay_obligation`, `.pay_card_invoice_lifecycle`,
  `.link_refund_transaction`) that a human's manual-entry form already
  calls over HTTP -- imported locally to avoid a circular import (`app.api`
  will import this module to expose `/assistant/*`). Household isolation,
  `_require_admin`, large-amount confirmation, duplicate detection and the
  underlying `AuditEvent` are therefore all enforced exactly once, by the
  same code a human path already exercises -- this module adds nothing to
  that authorization surface, it only narrates the call in
  `AssistantActionEvent`.
- `undo_assistant_action` never deletes audit history: it calls the
  existing deterministic reversal (`unpay_obligation`, `unlink_refund`, or
  the transaction-delete guard already used by manual entries) and records
  a brand new `AuditEvent` for the reversal, marking the original
  `AssistantActionEvent` as undone -- it never rewrites or removes the
  original row.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.assistant_interpreter import StructuredInterpretation

# Typed actions the Assistant may propose/execute this slice. Deliberately a
# closed list matching `app.services.assistant_sanitizer.KNOWN_INTENTS`
# minus the two non-mutating intents (`query`/`unknown`, which never reach
# `execute_typed_action`) -- see the Work Order's mandatory scope (§ escopo
# obrigatório item 5). Asset/investment typed actions
# (`UPDATE_ASSET_VALUE`/`REGISTER_ASSET_CONTRIBUTION`) are deliberately not
# included: `Investment`/asset entities do not exist yet (October Go-Live
# Slice 6, not yet implemented) -- see the PR description's scope note.
TYPED_ACTIONS = (
    "create_expense",
    "create_income",
    "create_internal_transfer",
    "pay_obligation",
    "pay_card_invoice",
    "register_refund",
)

_INTENT_TO_TYPED_ACTION = {
    "create_expense": "create_expense",
    "create_income": "create_income",
    "create_internal_transfer": "create_internal_transfer",
    "pay_obligation": "pay_obligation",
    "pay_card_invoice": "pay_card_invoice",
    "register_refund": "register_refund",
}

MAX_CANDIDATES = 5


def _json_safe(value: Any) -> Any:
    """Round-trip `value` through `json.dumps(default=str)`/`json.loads` so
    it is safe to store in a `JSON_DOCUMENT` column. Mirrors
    `app.api._audit_state` exactly: the underlying endpoint's response dict
    can carry `Decimal`/`date` values (a plain Python return value, not a
    FastAPI-serialized HTTP body), which the database driver's own JSON
    encoder cannot serialize on its own."""

    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class AssistantActionError(ValueError):
    """A typed-action request that fails a Slice 4 precondition (unknown
    typed_action, undo of a non-reversible or already-undone action, action
    not found/not owned by this household). Callers translate this to an
    HTTP 404/409/422 at the API layer, mirroring every other service-level
    `ValueError` subclass in this project (`TransferError`,
    `CardInvoiceError`)."""


# ---------------------------------------------------------------------------
# Deterministic text parsing helpers (no Codex involved from here on -- this
# is the same kind of small, stable, testable parsing already used
# elsewhere in this project, e.g. `app.services.card_competence`).
# ---------------------------------------------------------------------------


def parse_amount_text(text: str | None) -> Decimal | None:
    """Parse a free-text amount hint (`"300"`, `"R$ 300,50"`, `"8.500"`)
    into a positive `Decimal`, or `None` if it cannot be parsed
    unambiguously. Never guesses a value from context -- an unparsable
    amount is always treated as a missing field, never defaulted to zero or
    to a previous value."""

    if not text:
        return None
    cleaned = re.sub(r"[^0-9,.\-]", "", text).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # Brazilian format: "." thousands, "," decimal -- e.g. "8.500,00".
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        # Only a comma: treat it as the decimal separator ("300,50").
        cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


def parse_date_text(text: str | None, *, today: date | None = None) -> date | None:
    """Parse a small, closed vocabulary of relative/absolute date hints.

    Deliberately narrow (`"hoje"`, `"ontem"`, `"anteontem"`, ISO
    `YYYY-MM-DD`, `DD/MM/YYYY`) -- anything else is left unresolved rather
    than guessed, and the caller falls back to today's date only where the
    Work Order explicitly allows it (`create_expense`/`create_income`
    default to "data atual" when the message gives no date, rebaseline §11
    example)."""

    if not text:
        return None
    normalized = _strip_accents(text).strip().lower()
    reference = today or date.today()
    if normalized in {"hoje"}:
        return reference
    if normalized in {"ontem"}:
        return reference - timedelta(days=1)
    if normalized in {"anteontem"}:
        return reference - timedelta(days=2)
    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text.strip())
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            return None
    br_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text.strip())
    if br_match:
        try:
            return date(int(br_match.group(3)), int(br_match.group(2)), int(br_match.group(1)))
        except ValueError:
            return None
    return None


def _strip_accents(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", value)
        if unicodedata.category(character) != "Mn"
    )


# ---------------------------------------------------------------------------
# Deterministic, household-scoped entity resolution. Every function here is
# a plain read: no row is created, no column is written, no `db.flush()`/
# `db.commit()` is ever called from this section.
# ---------------------------------------------------------------------------


def _candidate_accounts(
    db: Session, *, household_id: str, hint: str, account_types: tuple[str, ...] | None = None
) -> list[Any]:
    from app.models import Account

    if not hint:
        return []
    normalized_hint = _strip_accents(hint).strip().lower()
    if not normalized_hint:
        return []
    query = select(Account).where(Account.household_id == household_id, Account.active.is_(True))
    if account_types:
        query = query.where(Account.account_type.in_(account_types))
    accounts = list(db.scalars(query))
    matches = [
        account
        for account in accounts
        if normalized_hint in _strip_accents(account.name).lower()
        or (account.last_four and account.last_four in hint)
    ]
    return matches


def _candidate_obligations(db: Session, *, household_id: str, hint: str) -> list[Any]:
    from app.models import Obligation

    if not hint:
        return []
    normalized_hint = _strip_accents(hint).strip().lower()
    if not normalized_hint:
        return []
    obligations = list(
        db.scalars(
            select(Obligation).where(
                Obligation.household_id == household_id,
                Obligation.active.is_(True),
                Obligation.status == "pending",
            )
        )
    )
    return [
        obligation
        for obligation in obligations
        if normalized_hint in _strip_accents(obligation.name).lower()
    ][:MAX_CANDIDATES]


def _candidate_open_invoices(db: Session, *, household_id: str, hint: str) -> list[Any]:
    """Existing, already-synced `CardInvoice` rows for a card matching
    `hint`, in `open`/`closed`/`partially_paid` status (payable states).
    Never syncs/creates an invoice here -- resolution is read-only; a card
    whose current cycle was never synced yet (`POST /card-invoices/sync`,
    or the first "Contas a pagar" visit that triggers it) simply yields no
    candidate, and the Assistant asks the user to open Contas a pagar
    first, rather than materializing a new row from an "interpret" call."""

    from app.models import CardInvoice

    accounts = _candidate_accounts(
        db, household_id=household_id, hint=hint, account_types=("credit_card",)
    )
    if len(accounts) != 1:
        return []
    invoices = list(
        db.scalars(
            select(CardInvoice)
            .where(
                CardInvoice.household_id == household_id,
                CardInvoice.account_id == accounts[0].id,
                CardInvoice.status.in_(("open", "closed", "partially_paid")),
            )
            .order_by(CardInvoice.competence.desc())
        )
    )
    return invoices[:MAX_CANDIDATES]


def _candidate_refund_targets(
    db: Session, *, household_id: str, hint: str, amount: Decimal | None
) -> list[Any]:
    """Candidate original purchases a refund/estorno might neutralize:
    non-excluded `expense` transactions, most recent first, optionally
    narrowed by amount. Never picks a single candidate by itself when more
    than one plausible match exists -- ambiguity always returns to the
    caller as multiple candidates, per rebaseline §6.6/Work Order test
    "ambiguidade de alvo de estorno -> pergunta, nunca escolha silenciosa"."""

    from app.models import Transaction

    query = select(Transaction).where(
        Transaction.household_id == household_id,
        Transaction.transaction_type == "expense",
        Transaction.excluded.is_(False),
    )
    if amount is not None:
        query = query.where(Transaction.amount.between(-(amount + Decimal("0.01")), -(amount - Decimal("0.01"))))
    candidates = list(db.scalars(query.order_by(Transaction.booked_at.desc()).limit(50)))
    normalized_hint = _strip_accents(hint).strip().lower() if hint else ""
    if normalized_hint:
        candidates = [
            transaction
            for transaction in candidates
            if normalized_hint in _strip_accents(transaction.description).lower()
        ]
    return candidates[:MAX_CANDIDATES]


def _candidate_refund_transactions(db: Session, *, household_id: str, hint: str) -> list[Any]:
    """Candidate already-existing `refund` transactions not yet linked to
    an original purchase -- the counterpart search to
    `_candidate_refund_targets` when the user refers to a refund that
    already exists (e.g. imported from a bank/card statement) instead of
    one the Assistant should create."""

    from app.models import Transaction

    candidates = list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.household_id == household_id,
                Transaction.transaction_type == "refund",
                Transaction.refund_of_transaction_id.is_(None),
            )
            .order_by(Transaction.booked_at.desc())
            .limit(50)
        )
    )
    normalized_hint = _strip_accents(hint).strip().lower() if hint else ""
    if normalized_hint:
        candidates = [
            transaction
            for transaction in candidates
            if normalized_hint in _strip_accents(transaction.description).lower()
        ]
    return candidates[:MAX_CANDIDATES]


def _serialize_candidate(entity: Any, kind: str) -> dict[str, Any]:
    if kind == "account":
        return {"id": entity.id, "label": f"{entity.name} ({entity.account_type})"}
    if kind == "obligation":
        return {"id": entity.id, "label": f"{entity.name} - vencimento {entity.due_date.isoformat()}"}
    if kind == "card_invoice":
        return {"id": entity.id, "label": f"Fatura {entity.competence} ({entity.status})"}
    if kind == "transaction":
        return {
            "id": entity.id,
            "label": f"{entity.description} - {entity.booked_at.isoformat()} - "
            f"R$ {abs(entity.amount):,.2f}".replace(",", "_").replace(".", ",").replace("_", "."),
        }
    return {"id": entity.id, "label": str(entity.id)}


# ---------------------------------------------------------------------------
# Typed-action proposal (the output of `POST /assistant/interpret`).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypedActionProposal:
    can_execute: bool
    typed_action: str | None = None
    payload: dict[str, Any] | None = None
    path_params: dict[str, str] | None = None
    clarifying_question: str | None = None
    missing_fields: tuple[str, ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()
    candidate_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_execute": self.can_execute,
            "typed_action": self.typed_action,
            "payload": self.payload,
            "path_params": self.path_params,
            "clarifying_question": self.clarifying_question,
            "missing_fields": list(self.missing_fields),
            "candidates": list(self.candidates),
            "candidate_kind": self.candidate_kind,
        }


def _needs_disambiguation(question: str, *, missing_fields: tuple[str, ...] = ()) -> TypedActionProposal:
    return TypedActionProposal(can_execute=False, clarifying_question=question, missing_fields=missing_fields)


def build_typed_action_proposal(
    db: Session,
    *,
    household_id: str,
    interpretation: StructuredInterpretation,
) -> TypedActionProposal:
    """Deterministically resolve a Codex interpretation into either a
    ready-to-confirm typed-action proposal or a clarifying question.

    Read-only: never creates, updates or deletes a row. The returned
    `payload`/`path_params`, when `can_execute` is true, are exactly the
    fields the matching `app.schemas` request model requires -- but
    `execute_typed_action` re-validates and re-authorizes all of it from
    scratch; nothing here is trusted at execute time just because it was
    proposed here.
    """

    if not interpretation.available:
        return _needs_disambiguation(
            "Não consegui interpretar sua mensagem agora. Pode reescrever de outro jeito, "
            "com valor, conta/cartão e o que foi?"
        )
    if interpretation.intent in (None, "query", "unknown"):
        return _needs_disambiguation(
            "Não identifiquei uma ação para executar nessa mensagem. Você quer registrar uma "
            "entrada, uma saída, uma transferência, pagar uma obrigação/fatura ou registrar um estorno?"
        )

    typed_action = _INTENT_TO_TYPED_ACTION.get(interpretation.intent)
    if typed_action is None:
        return _needs_disambiguation("Não sei executar esse tipo de ação ainda pelo Assistente.")

    fields = interpretation.extracted_fields
    if typed_action in ("create_expense", "create_income"):
        return _propose_create_transaction(db, household_id=household_id, typed_action=typed_action, fields=fields)
    if typed_action == "create_internal_transfer":
        return _propose_internal_transfer(db, household_id=household_id, fields=fields)
    if typed_action == "pay_obligation":
        return _propose_pay_obligation(db, household_id=household_id, fields=fields)
    if typed_action == "pay_card_invoice":
        return _propose_pay_card_invoice(db, household_id=household_id, fields=fields)
    if typed_action == "register_refund":
        return _propose_register_refund(db, household_id=household_id, fields=fields)
    return _needs_disambiguation("Não sei executar esse tipo de ação ainda pelo Assistente.")


def _propose_create_transaction(
    db: Session, *, household_id: str, typed_action: str, fields: dict[str, str]
) -> TypedActionProposal:
    amount = parse_amount_text(fields.get("amount_text"))
    missing = []
    if amount is None:
        missing.append("amount")
    description = fields.get("description") or fields.get("counterparty_hint")
    if not description:
        missing.append("description")

    account_hint = fields.get("account_hint")
    accounts = _candidate_accounts(db, household_id=household_id, hint=account_hint or "")
    if not account_hint:
        missing.append("account")
    elif len(accounts) == 0:
        return _needs_disambiguation(
            f'Não encontrei nenhuma conta/cartão chamado "{account_hint}". Qual conta ou cartão foi?'
        )
    elif len(accounts) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de uma conta com esse nome. Qual delas?",
            candidates=tuple(_serialize_candidate(item, "account") for item in accounts),
            candidate_kind="account",
        )

    # `create_expense`'s funding origin question (rebaseline §4.4, §11.1):
    # only material for an expense paid from cash/checking with no explicit
    # source -- a card purchase never asks this (the card itself is the
    # origin), and `create_income` never asks it (income has no funding
    # source, INV-003/004 boundary).
    funding_source = "account"
    if typed_action == "create_expense" and accounts:
        account = accounts[0]
        hint_value = fields.get("funding_source_hint")
        if account.account_type in ("checking", "cash"):
            if hint_value == "privilege":
                funding_source = "privilege"
            elif hint_value == "account":
                funding_source = "account"
            else:
                return _needs_disambiguation(
                    "O valor já estava na Conta Corrente ou você resgatou do Privilège?"
                )

    if missing:
        return _needs_disambiguation(
            "Preciso de mais informação: valor, conta/cartão e o que foi.", missing_fields=tuple(missing)
        )

    booked_at = parse_date_text(fields.get("date_text")) or date.today()
    payload: dict[str, Any] = {
        "booked_at": booked_at.isoformat(),
        "description": description[:500],
        "amount": str(amount),
        "movement_type": "expense" if typed_action == "create_expense" else "income",
        "account_id": accounts[0].id,
    }
    if typed_action == "create_expense":
        category_hint = fields.get("category_hint")
        if category_hint:
            payload["category_name"] = category_hint[:100]
        payload["funding_source"] = funding_source
    return TypedActionProposal(can_execute=True, typed_action=typed_action, payload=payload, path_params={})


def _propose_internal_transfer(db: Session, *, household_id: str, fields: dict[str, str]) -> TypedActionProposal:
    amount = parse_amount_text(fields.get("amount_text"))
    if amount is None:
        return _needs_disambiguation("Qual o valor da transferência?", missing_fields=("amount",))

    account_hint = fields.get("account_hint")
    target_hint = fields.get("target_hint") or fields.get("counterparty_hint")
    if not account_hint or not target_hint:
        return _needs_disambiguation(
            "De qual conta para qual conta foi essa transferência?",
            missing_fields=tuple(name for name, value in (("from_account", account_hint), ("to_account", target_hint)) if not value),
        )
    from_accounts = _candidate_accounts(db, household_id=household_id, hint=account_hint)
    to_accounts = _candidate_accounts(db, household_id=household_id, hint=target_hint)
    if len(from_accounts) != 1 or len(to_accounts) != 1:
        return _needs_disambiguation("Não consegui identificar as duas contas dessa transferência com certeza. Quais são elas?")
    if from_accounts[0].id == to_accounts[0].id:
        return _needs_disambiguation("A conta de origem e destino não podem ser a mesma. Quais são as contas certas?")

    booked_at = parse_date_text(fields.get("date_text")) or date.today()
    description = fields.get("description") or "Transferência entre contas"
    payload = {
        "booked_at": booked_at.isoformat(),
        "description": description[:500],
        "amount": str(amount),
        "from_account_id": from_accounts[0].id,
        "to_account_id": to_accounts[0].id,
    }
    return TypedActionProposal(
        can_execute=True, typed_action="create_internal_transfer", payload=payload, path_params={}
    )


def _propose_pay_obligation(db: Session, *, household_id: str, fields: dict[str, str]) -> TypedActionProposal:
    target_hint = fields.get("target_hint") or fields.get("counterparty_hint") or fields.get("description")
    if not target_hint:
        return _needs_disambiguation("Qual obrigação você quer pagar?", missing_fields=("target",))
    obligations = _candidate_obligations(db, household_id=household_id, hint=target_hint)
    if len(obligations) == 0:
        return _needs_disambiguation(f'Não encontrei nenhuma obrigação em aberto parecida com "{target_hint}".')
    if len(obligations) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de uma obrigação parecida. Qual delas?",
            candidates=tuple(_serialize_candidate(item, "obligation") for item in obligations),
            candidate_kind="obligation",
        )
    obligation = obligations[0]

    funding_source_hint = fields.get("funding_source_hint")
    if funding_source_hint not in ("account", "privilege"):
        return _needs_disambiguation("O pagamento saiu da Conta Corrente ou você resgatou do Privilège?")

    # `ObligationPaymentRequest` always needs a real checking/cash
    # `account_id` where the payment lands, regardless of
    # `funding_source` -- "privilege" only marks that account as funded by
    # an internal Privilège redemption (`_fund_obligation_from_privilege`);
    # it is never a substitute for a bank account.
    account_hint = fields.get("account_hint")
    accounts = _candidate_accounts(
        db, household_id=household_id, hint=account_hint or "", account_types=("checking", "cash")
    )
    if len(accounts) != 1:
        return _needs_disambiguation("De qual conta corrente ou caixa saiu esse pagamento?")

    paid_at = parse_date_text(fields.get("date_text")) or date.today()
    payload: dict[str, Any] = {
        "account_id": accounts[0].id,
        "paid_at": paid_at.isoformat(),
        "funding_source": funding_source_hint,
        "description": fields.get("description") or None,
    }
    return TypedActionProposal(
        can_execute=True,
        typed_action="pay_obligation",
        payload=payload,
        path_params={"obligation_id": obligation.id},
    )


def _propose_pay_card_invoice(db: Session, *, household_id: str, fields: dict[str, str]) -> TypedActionProposal:
    account_hint = fields.get("account_hint") or fields.get("target_hint")
    if not account_hint:
        return _needs_disambiguation("Qual cartão você quer pagar?", missing_fields=("account",))
    invoices = _candidate_open_invoices(db, household_id=household_id, hint=account_hint)
    if len(invoices) == 0:
        return _needs_disambiguation(
            f'Não encontrei fatura em aberto sincronizada para "{account_hint}". Abra Contas a pagar '
            "para sincronizar a fatura antes de pedir ao Assistente para pagá-la."
        )
    if len(invoices) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de uma fatura em aberto para esse cartão. Qual delas?",
            candidates=tuple(_serialize_candidate(item, "card_invoice") for item in invoices),
            candidate_kind="card_invoice",
        )
    invoice = invoices[0]

    amount = parse_amount_text(fields.get("amount_text"))
    from app.services.card_invoice_lifecycle import outstanding_balance

    if amount is None:
        amount = outstanding_balance(invoice)

    paying_hint = fields.get("target_hint") if fields.get("target_hint") != account_hint else None
    paying_accounts = _candidate_accounts(
        db, household_id=household_id, hint=paying_hint or "", account_types=("checking",)
    )
    if len(paying_accounts) != 1:
        return _needs_disambiguation("De qual conta corrente sai o pagamento dessa fatura?")

    booked_at = parse_date_text(fields.get("date_text")) or date.today()
    payload = {
        "paying_account_id": paying_accounts[0].id,
        "amount": str(amount),
        "booked_at": booked_at.isoformat(),
        "description": fields.get("description") or f"Pagamento fatura {invoice.competence}",
        "confirmed": True,
    }
    return TypedActionProposal(
        can_execute=True,
        typed_action="pay_card_invoice",
        payload=payload,
        path_params={"invoice_id": invoice.id},
    )


def _propose_register_refund(db: Session, *, household_id: str, fields: dict[str, str]) -> TypedActionProposal:
    hint = fields.get("target_hint") or fields.get("counterparty_hint") or fields.get("description") or ""
    amount = parse_amount_text(fields.get("amount_text"))

    refund_candidates = _candidate_refund_transactions(db, household_id=household_id, hint=hint)
    if len(refund_candidates) != 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question=(
                "Não encontrei um lançamento de estorno já registrado com certeza. Cadastre o "
                "estorno em Saídas/Entradas primeiro, ou me diga qual lançamento é o estorno."
            ),
            candidates=tuple(_serialize_candidate(item, "transaction") for item in refund_candidates),
            candidate_kind="transaction" if refund_candidates else None,
        )
    refund_transaction = refund_candidates[0]

    original_candidates = _candidate_refund_targets(db, household_id=household_id, hint=hint, amount=amount)
    if len(original_candidates) == 0:
        return _needs_disambiguation("Não encontrei a compra original desse estorno. Qual foi a compra?")
    if len(original_candidates) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de uma compra parecida. Qual delas o estorno neutraliza?",
            candidates=tuple(_serialize_candidate(item, "transaction") for item in original_candidates),
            candidate_kind="transaction",
        )
    original_transaction = original_candidates[0]

    payload = {
        "refund_transaction_id": refund_transaction.id,
        "original_transaction_id": original_transaction.id,
        "reason": "Vínculo confirmado via Assistente Financeiro",
    }
    return TypedActionProposal(
        can_execute=True,
        typed_action="register_refund",
        payload=payload,
        path_params={"transaction_id": refund_transaction.id},
    )


# ---------------------------------------------------------------------------
# Execution -- dispatches to the exact existing deterministic endpoint.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TypedActionSpec:
    schema_name: str
    dispatcher_name: str
    path_param_names: tuple[str, ...] = ()
    entity_type: str = "transaction"


_TYPED_ACTION_SPECS: dict[str, _TypedActionSpec] = {
    "create_expense": _TypedActionSpec("ManualTransactionRequest", "create_manual_transaction"),
    "create_income": _TypedActionSpec("ManualTransactionRequest", "create_manual_transaction"),
    "create_internal_transfer": _TypedActionSpec("TransferRequest", "create_manual_transfer"),
    "pay_obligation": _TypedActionSpec(
        "ObligationPaymentRequest", "pay_obligation", ("obligation_id",), entity_type="obligation"
    ),
    "pay_card_invoice": _TypedActionSpec(
        "CardInvoicePayRequest", "pay_card_invoice_lifecycle", ("invoice_id",), entity_type="card_invoice"
    ),
    "register_refund": _TypedActionSpec(
        "RefundLinkRequest", "link_refund_transaction", ("transaction_id",)
    ),
}


def _target_entity_ids(typed_action: str, response: dict[str, Any], path_params: dict[str, str]) -> list[str]:
    if typed_action in ("create_expense", "create_income"):
        # `create_manual_transaction` returns `{"id": transaction.id}`.
        return [str(response["id"])] if response.get("id") else []
    if typed_action == "create_internal_transfer":
        return [
            value
            for value in (response.get("from_transaction_id"), response.get("to_transaction_id"))
            if value
        ]
    if typed_action == "pay_obligation":
        return [path_params["obligation_id"]]
    if typed_action == "pay_card_invoice":
        return [path_params["invoice_id"]]
    if typed_action == "register_refund":
        return [path_params["transaction_id"]]
    return []


def _find_existing_action_by_trace(db: Session, *, household_id: str, trace_id: str) -> Any | None:
    from app.models import AssistantActionEvent, AuditEvent

    return db.scalar(
        select(AssistantActionEvent)
        .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
        .where(AuditEvent.household_id == household_id, AuditEvent.trace_id == trace_id)
    )


def _serialize_action_event(event: Any, *, audit_event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "typed_action": event.typed_action,
        "trace_id": audit_event.trace_id,
        "target_entity_type": event.target_entity_type,
        "target_entity_ids": event.target_entity_ids,
        "before_state": event.before_state,
        "after_state": event.after_state,
        "undoable": event.undoable,
        "non_reversible_reason": event.non_reversible_reason,
        "undone_at": event.undone_at.isoformat() if event.undone_at else None,
        "created_at": event.created_at.isoformat(),
    }


def execute_typed_action(
    db: Session,
    *,
    user: Any,
    typed_action: str,
    payload: dict[str, Any],
    path_params: dict[str, str] | None = None,
    original_message: str,
    structured_interpretation: dict[str, Any] | None,
    disambiguation_qa: list[dict[str, Any]] | None,
    trace_id: str,
) -> dict[str, Any]:
    """Validate, dispatch and audit one typed action.

    Idempotency: a retry with the same `trace_id` (already scoped to this
    household through the `AuditEvent` join) returns the original result
    without executing anything a second time -- see module docstring. This
    is a convenience layer on top of, never a replacement for, each
    underlying endpoint's own duplicate-detection/fingerprint guard, which
    still applies unconditionally.
    """

    import app.api as api
    import app.schemas as schemas
    from app.models import AssistantActionEvent

    if typed_action not in TYPED_ACTIONS:
        raise AssistantActionError(f"Ação tipada desconhecida: {typed_action}")

    existing = _find_existing_action_by_trace(db, household_id=user.household_id, trace_id=trace_id)
    if existing is not None:
        from app.models import AuditEvent

        audit_event = db.get(AuditEvent, existing.audit_event_id)
        return {"idempotent_replay": True, "action": _serialize_action_event(existing, audit_event=audit_event)}

    spec = _TYPED_ACTION_SPECS[typed_action]
    schema_cls = getattr(schemas, spec.schema_name)
    path_params = path_params or {}
    missing_path_params = [name for name in spec.path_param_names if name not in path_params]
    if missing_path_params:
        raise AssistantActionError(f"Parâmetros obrigatórios ausentes: {', '.join(missing_path_params)}")

    if typed_action in ("create_expense", "create_income"):
        # Never trust the caller's `movement_type` (if any slipped into
        # `payload`) over the `typed_action` the caller separately declared
        # and this function already validated against `TYPED_ACTIONS` --
        # deriving it here again, unconditionally, means a mismatched
        # payload can never smuggle `movement_type="income"` under
        # `typed_action="create_expense"` or vice-versa.
        payload = {**payload, "movement_type": "expense" if typed_action == "create_expense" else "income"}
    try:
        request_payload = schema_cls(**payload)
    except PydanticValidationError as exc:
        # A payload that does not validate against the target endpoint's
        # own schema is, by definition, an incomplete/ambiguous action --
        # rejected before any write, exactly like a materially incomplete
        # message never executes automatically (Work Order invariant
        # "Nenhuma ação ambígua é executada automaticamente").
        raise AssistantActionError(f"Dados incompletos ou inválidos para {typed_action}: {exc}") from exc
    dispatcher = getattr(api, spec.dispatcher_name)

    if typed_action == "pay_obligation":
        response = dispatcher(path_params["obligation_id"], request_payload, user, db)
    elif typed_action == "pay_card_invoice":
        response = dispatcher(path_params["invoice_id"], request_payload, user, db)
    elif typed_action == "register_refund":
        response = dispatcher(path_params["transaction_id"], request_payload, user, db)
    else:
        response = dispatcher(request_payload, user, db)

    target_entity_ids = _target_entity_ids(typed_action, response, path_params)
    undoable, non_reversible_reason = _undo_capability(typed_action)

    # A second, distinct `AuditEvent` from the one the dispatched endpoint
    # already recorded for the financial fact itself (e.g.
    # `create_manual_transaction`'s own `"transaction.create_manual"`
    # event) -- this one narrates that the Assistant, specifically,
    # originated the call, and is what `AssistantActionEvent` is 1:1 with.
    # Built directly (not through `app.api.audit()`, which returns `None`)
    # because this function needs the row's generated id immediately.
    from app.models import AuditEvent as AuditEventModel

    audit_event_row = AuditEventModel(
        household_id=user.household_id,
        user_id=user.id,
        event_type="assistant.execute",
        entity_type=spec.entity_type,
        entity_id=target_entity_ids[0] if target_entity_ids else None,
        details=json.dumps({"typed_action": typed_action, "response": response}, ensure_ascii=False, default=str),
        trace_id=trace_id,
        source="assistant",
    )
    db.add(audit_event_row)
    db.flush()
    action_event = AssistantActionEvent(
        audit_event_id=audit_event_row.id,
        original_message=original_message[:2000],
        structured_interpretation=structured_interpretation,
        disambiguation_qa=disambiguation_qa or [],
        typed_action=typed_action,
        target_entity_type=spec.entity_type,
        target_entity_ids=target_entity_ids,
        before_state=None,
        after_state=_json_safe(response),
        undoable=undoable,
        non_reversible_reason=non_reversible_reason,
    )
    db.add(action_event)
    db.commit()
    return {
        "idempotent_replay": False,
        "response": response,
        "action": _serialize_action_event(action_event, audit_event=audit_event_row),
    }


def _undo_capability(typed_action: str) -> tuple[bool, str | None]:
    if typed_action == "pay_card_invoice":
        return False, (
            "Pagamento de fatura não possui reversão automática determinística nesta versão; "
            "corrija manualmente na tela de faturas caso necessário."
        )
    return True, None


def undo_assistant_action(
    db: Session, *, user: Any, action_id: str, reason: str
) -> dict[str, Any]:
    import app.api as api
    import app.schemas as schemas
    from app.models import AssistantActionEvent, AuditEvent

    action_event = db.scalar(
        select(AssistantActionEvent)
        .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
        .where(AssistantActionEvent.id == action_id, AuditEvent.household_id == user.household_id)
    )
    if action_event is None:
        raise AssistantActionError("Ação do Assistente não encontrada")
    if action_event.undone_at is not None:
        raise AssistantActionError("Esta ação já foi desfeita")
    if not action_event.undoable:
        raise AssistantActionError(
            action_event.non_reversible_reason or "Esta ação não pode ser desfeita automaticamente"
        )

    typed_action = action_event.typed_action
    target_ids = action_event.target_entity_ids or []

    if typed_action in ("create_expense", "create_income", "create_internal_transfer"):
        if not target_ids:
            raise AssistantActionError("Não há lançamento para desfazer")
        api.delete_manual_transaction(target_ids[0], user, db)
    elif typed_action == "pay_obligation":
        if not target_ids:
            raise AssistantActionError("Não há obrigação para desfazer")
        api.unpay_obligation(
            target_ids[0], schemas.ObligationPaymentUndoRequest(reason=reason), user, db
        )
    elif typed_action == "register_refund":
        if not target_ids:
            raise AssistantActionError("Não há vínculo de estorno para desfazer")
        api.unlink_refund_transaction(
            target_ids[0], schemas.RefundUnlinkRequest(refund_transaction_id=target_ids[0], reason=reason), user, db
        )
    else:
        raise AssistantActionError(
            action_event.non_reversible_reason or "Esta ação não pode ser desfeita automaticamente"
        )

    # A distinct `AuditEvent` for the reversal itself -- in addition to
    # whichever event the reversal endpoint above already recorded for its
    # own domain (`"obligation.unpay"`, `"refund.unlink"`,
    # `"transaction.delete_manual"`/`"transfer.delete"`). Never overwrites
    # or deletes the original `assistant.execute` event: the trail grows,
    # it never shrinks.
    from app.models import AuditEvent as AuditEventModel

    undo_audit_event = AuditEventModel(
        household_id=user.household_id,
        user_id=user.id,
        event_type="assistant.undo",
        entity_type=action_event.target_entity_type,
        entity_id=target_ids[0] if target_ids else None,
        details=json.dumps({"typed_action": typed_action, "action_event_id": action_event.id}, ensure_ascii=False),
        reason=reason,
        source="assistant",
    )
    db.add(undo_audit_event)
    db.flush()

    action_event.undone_at = datetime.now(UTC)
    action_event.undone_by = user.id
    action_event.undo_audit_event_id = undo_audit_event.id
    db.commit()
    return {"ok": True, "action_id": action_event.id, "typed_action": typed_action}


def list_assistant_actions(db: Session, *, user: Any, limit: int = 50) -> list[dict[str, Any]]:
    from app.models import AssistantActionEvent, AuditEvent

    rows = db.execute(
        select(AssistantActionEvent, AuditEvent)
        .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
        .where(AuditEvent.household_id == user.household_id)
        .order_by(AssistantActionEvent.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    return [_serialize_action_event(event, audit_event=audit_event) for event, audit_event in rows]
