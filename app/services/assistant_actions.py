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
- Autoridade de execução (engineering review of PR #92, blocker 1):
  `POST /assistant/interpret` calls `build_typed_action_proposal` and, only
  when the result has `can_execute=True`, persists it via
  `persist_action_proposal` as an `AssistantActionProposal` row -- the
  single source of truth for what may be executed. `POST /assistant/execute`
  never accepts a `typed_action`/`payload`/`path_params`/
  `structured_interpretation` from the HTTP caller: `execute_typed_action`
  takes only a `proposal_id`, loads that row (household-scoped), and reads
  every one of those fields back from it. A client can therefore never skip
  `/assistant/interpret` and hand-execute a fabricated or spoofed
  interpretation for a message that was actually ambiguous -- there is no
  code path from an HTTP request straight into `TYPED_ACTIONS` dispatch
  that does not pass through the deterministic resolution in this module
  first. A proposal is single-use (`consumed_at`/`consumed_action_event_id`)
  and time-boxed (`expires_at`, `_PROPOSAL_TTL_MINUTES`).
- `execute_typed_action` never re-implements financial logic: every typed
  action is a direct, in-process call into the exact same endpoint
  function's body (`app.api._create_manual_transaction_impl`,
  `._create_manual_transfer_impl`, `._pay_obligation_impl`,
  `._pay_card_invoice_lifecycle_impl`, `._link_refund_transaction_impl` --
  the underlying implementation `create_manual_transaction`/etc. themselves
  thinly wrap for the public HTTP contract) that a human's manual-entry
  form already calls over HTTP -- imported locally to avoid a circular
  import (`app.api` will import this module to expose `/assistant/*`).
  Household isolation, `_require_admin`, large-amount confirmation,
  duplicate detection and the underlying domain `AuditEvent` are therefore
  all enforced exactly once, by the same code a human path already
  exercises -- this module adds nothing to that authorization surface, it
  only narrates the call in `AssistantActionEvent`.
- Atomicidade (engineering review of PR #92, blocker 5): the dispatched
  `_impl` function is called with `commit=False`, so its own domain write
  and this module's own `AssistantActionEvent`/proposal-consumption write
  land in one open transaction; `execute_typed_action` issues the single
  `db.commit()` that finalizes both together, and rolls back the whole
  thing on any failure in between -- there is no window where the domain
  fact is durably persisted but the Assistant's own audit/undo trail is
  not (Work Order invariant "Nenhuma ação do Assistente fica sem audit
  trail/trace").
- `undo_assistant_action` never deletes audit history: it calls the
  existing deterministic reversal's `_impl` (`_unpay_obligation_impl`,
  `_unlink_refund_transaction_impl`, or `_delete_manual_transaction_impl`,
  same `commit=False` atomicity as above) and records a brand new
  `AuditEvent` for the reversal, marking the original `AssistantActionEvent`
  as undone -- it never rewrites or removes the original row.
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

# Typed actions the Assistant may propose/execute. Deliberately a closed
# list matching `app.services.assistant_sanitizer.KNOWN_INTENTS` minus the
# two non-mutating intents (`query`/`unknown`, which never reach
# `execute_typed_action`) -- see the Work Order's mandatory scope (§ escopo
# obrigatório item 5). `update_asset_value`/`register_asset_contribution`
# (October Go-Live Slice 6, P0 #87 -- `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_6.md`)
# were deliberately excluded before this slice because `Investment` did not
# exist yet; both now dispatch into `app.api._update_investment_value_impl`/
# `_register_investment_contribution_impl`, the exact same functions the
# manual `POST /investments/{id}/valuations`/`/contributions` endpoints call.
TYPED_ACTIONS = (
    "create_expense",
    "create_income",
    "create_internal_transfer",
    "pay_obligation",
    "pay_card_invoice",
    "register_refund",
    "update_asset_value",
    "register_asset_contribution",
)

_INTENT_TO_TYPED_ACTION = {
    "create_expense": "create_expense",
    "create_income": "create_income",
    "create_internal_transfer": "create_internal_transfer",
    "pay_obligation": "pay_obligation",
    "pay_card_invoice": "pay_card_invoice",
    "register_refund": "register_refund",
    "update_asset_value": "update_asset_value",
    "register_asset_contribution": "register_asset_contribution",
}

MAX_CANDIDATES = 5

# Blocker 1 (proposal binding): how long a resolved, ready-to-execute
# proposal stays eligible for `POST /assistant/execute` after `POST
# /assistant/interpret` persisted it. Long enough for a human to read a
# confirmation and tap once; short enough that a stale proposal cannot
# execute against household data that has since materially changed (an
# account closed, an obligation already paid another way).
PROPOSAL_TTL_MINUTES = 30

_DUPLICATE_DECISIONS = ("skip", "import_anyway", "view_existing")


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


@dataclass(frozen=True, slots=True)
class DuplicateResolution:
    """An explicit, discrete human decision on a possible-duplicate warning
    a prior `build_typed_action_proposal` call already surfaced (Work Order
    "deduplicação provável com as três escolhas humanas" -- engineering
    review of PR #92, blocker 4). `transaction_id` must name the exact
    candidate the warning showed; a decision that no longer matches the
    current duplicate scan (household data changed, or the candidate is
    stale) is never honored silently -- the caller falls back to asking
    again, never to skipping the check."""

    decision: str
    transaction_id: str


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


def _candidate_investments(db: Session, *, household_id: str, hint: str) -> list[Any]:
    """Candidate active `Investment` rows matching `hint` by name (October
    Go-Live Slice 6). The Studio is resolved the same way as any other
    asset -- no special-cased lookup by a hardcoded name."""

    from app.models import Investment

    if not hint:
        return []
    normalized_hint = _strip_accents(hint).strip().lower()
    if not normalized_hint:
        return []
    investments = list(
        db.scalars(
            select(Investment).where(
                Investment.household_id == household_id, Investment.active.is_(True)
            )
        )
    )
    return [
        investment
        for investment in investments
        if normalized_hint in _strip_accents(investment.name).lower()
    ][:MAX_CANDIDATES]


def candidate_investment_funding_transactions(
    db: Session, *, household_id: str, amount: Decimal | None
) -> list[Any]:
    """Candidate already-existing "Transferência patrimonial" cash-out
    transactions (`movement_type="investment"` manual entries, or an
    imported equivalent) a contribution might link to -- the same
    already-existing-fact requirement `_candidate_refund_transactions`
    applies to `register_refund` (Work Order "sem fabricar ... origem de
    caixa": this module never creates the cash movement itself, it only
    resolves a link to one that already exists). Narrowed by amount when
    known, since a "Transferência patrimonial" transaction carries no
    description tying it to a specific asset.

    Exported (no leading underscore) since `GET /investments/contribution-
    candidates` (`app.api`) also calls it -- the manual Aporte UI must
    resolve/ask for a funding origin exactly like the Assistant does
    (engineering review on PR #94, blocking item 2), so both paths share
    this one candidate-matching query instead of each re-deriving it."""

    from app.api import _LEDGER_PATRIMONIAL_CATEGORY_NAME
    from app.models import Category, InvestmentValuation, Transaction

    query = (
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transaction_type == "transfer",
            Transaction.amount < 0,
            Category.name == _LEDGER_PATRIMONIAL_CATEGORY_NAME,
        )
    )
    if amount is not None:
        query = query.where(
            Transaction.amount.between(-(amount + Decimal("0.01")), -(amount - Decimal("0.01")))
        )
    candidates = list(db.scalars(query.order_by(Transaction.booked_at.desc()).limit(50)))
    already_linked_ids = set(
        db.scalars(
            select(InvestmentValuation.funding_transaction_id).where(
                InvestmentValuation.household_id == household_id,
                InvestmentValuation.funding_transaction_id.isnot(None),
                InvestmentValuation.invalidated_at.is_(None),
            )
        )
    )
    return [item for item in candidates if item.id not in already_linked_ids][:MAX_CANDIDATES]


@dataclass(frozen=True, slots=True)
class _PendingTransaction:
    """The minimal shape `app.services.duplicates.assess_duplicate` needs
    (`.fingerprint`/`.amount`/`.booked_at`/`.description`/`.account_id`/
    `.installment_current`/`.installment_total`/`.document_id`), for a
    manual expense/income the Assistant has not created yet. Never has a
    `fingerprint` (the real one is only computed by
    `create_manual_transaction` itself at write time), so `exact_fingerprint`
    is always `False` here -- a pre-flight scan can only ever find a
    *probable* match, never re-detect an idempotent replay of a fingerprint
    that does not exist yet."""

    amount: Decimal
    booked_at: date
    description: str
    account_id: str
    installment_current: int | None = None
    installment_total: int | None = None
    document_id: str | None = None
    fingerprint: str | None = None
    competence: str | None = None


def _check_possible_duplicate(
    db: Session,
    *,
    household_id: str,
    account_id: str,
    signed_amount: Decimal,
    booked_at: date,
    description: str,
) -> dict[str, Any] | None:
    """Read-only pre-flight probable-duplicate scan for a not-yet-created
    manual expense/income (Work Order "deduplicação provável com as três
    escolhas humanas" -- engineering review of PR #92, blocker 4).

    Reuses `app.services.duplicates.assess_duplicate` -- the exact same
    scoring rule `register_transaction_duplicates` already applies after
    the fact for the manual-entry form -- against real, already-persisted
    transactions in the same +/-3-day window that function already scans.
    Never writes anything (no `DuplicateGroup`, no flag on any row) before
    the human has chosen: the post-create pass
    (`register_transaction_duplicates`, unchanged) still runs once the
    human picks "importar mesmo assim", exactly like the manual-entry form
    already does, so the derived evidence a human resolves later in
    `GET /duplicate-groups` is unaffected either way.

    Returns `None` when no existing transaction reaches the "probable"
    confidence band; otherwise a small dict describing the strongest match,
    for `TypedActionProposal.candidates`."""

    from app.models import Transaction
    from app.services.duplicates import PROBABLE_THRESHOLD, assess_duplicate

    window_start = booked_at - timedelta(days=3)
    window_end = booked_at + timedelta(days=3)
    pending = _PendingTransaction(
        amount=signed_amount,
        booked_at=booked_at,
        description=description,
        account_id=account_id,
    )
    existing_rows = list(
        db.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.booked_at >= window_start,
                Transaction.booked_at <= window_end,
                Transaction.amount.between(signed_amount - Decimal("0.01"), signed_amount + Decimal("0.01")),
            )
        )
    )
    best_row = None
    best_assessment = None
    for row in existing_rows:
        assessment = assess_duplicate(pending, row)
        if assessment.confidence < PROBABLE_THRESHOLD:
            continue
        if best_assessment is None or assessment.confidence > best_assessment.confidence:
            best_row, best_assessment = row, assessment
    if best_row is None or best_assessment is None:
        return None
    return {
        "transaction": _serialize_candidate(best_row, "transaction"),
        "band": best_assessment.band,
        "confidence": str(best_assessment.confidence),
    }


def _serialize_candidate(entity: Any, kind: str) -> dict[str, Any]:
    if kind == "account":
        return {"id": entity.id, "label": f"{entity.name} ({entity.account_type})"}
    if kind == "obligation":
        return {"id": entity.id, "label": f"{entity.name} - vencimento {entity.due_date.isoformat()}"}
    if kind == "card_invoice":
        return {"id": entity.id, "label": f"Fatura {entity.competence} ({entity.status})"}
    if kind == "investment":
        return {"id": entity.id, "label": f"{entity.name} (R$ {entity.current_value:,.2f})".replace(",", "_").replace(".", ",").replace("_", ".")}
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
    duplicate_resolution: DuplicateResolution | None = None,
) -> TypedActionProposal:
    """Deterministically resolve a Codex interpretation into either a
    ready-to-confirm typed-action proposal or a clarifying question.

    Read-only: never creates, updates or deletes a row. The returned
    `payload`/`path_params`, when `can_execute` is true, are exactly the
    fields the matching `app.schemas` request model requires -- but
    `execute_typed_action` re-validates and re-authorizes all of it from
    scratch; nothing here is trusted at execute time just because it was
    proposed here. `duplicate_resolution`, when given, answers a possible-
    duplicate warning a *prior* call already returned (`candidate_kind ==
    "possible_duplicate"`) -- see `_propose_create_transaction`.
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
        return _propose_create_transaction(
            db,
            household_id=household_id,
            typed_action=typed_action,
            fields=fields,
            duplicate_resolution=duplicate_resolution,
        )
    if typed_action == "create_internal_transfer":
        return _propose_internal_transfer(db, household_id=household_id, fields=fields)
    if typed_action == "pay_obligation":
        return _propose_pay_obligation(db, household_id=household_id, fields=fields)
    if typed_action == "pay_card_invoice":
        return _propose_pay_card_invoice(db, household_id=household_id, fields=fields)
    if typed_action == "register_refund":
        return _propose_register_refund(db, household_id=household_id, fields=fields)
    if typed_action == "update_asset_value":
        return _propose_update_asset_value(db, household_id=household_id, fields=fields)
    if typed_action == "register_asset_contribution":
        return _propose_register_asset_contribution(db, household_id=household_id, fields=fields)
    return _needs_disambiguation("Não sei executar esse tipo de ação ainda pelo Assistente.")


def _propose_create_transaction(
    db: Session,
    *,
    household_id: str,
    typed_action: str,
    fields: dict[str, str],
    duplicate_resolution: DuplicateResolution | None = None,
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

    # Pre-flight probable-duplicate check (Work Order "deduplicação
    # provável com as três escolhas humanas" -- engineering review of PR
    # #92, blocker 4). Only for `create_expense`/`create_income`: the
    # underlying `register_transaction_duplicates` engine this reuses
    # scores `Transaction` rows, and every other typed action already has
    # its own equivalent-or-stronger fail-safe guard before writing --
    # `pay_obligation`'s same-account/near-date collision 409,
    # `pay_card_invoice`'s exact-match idempotent-replay short-circuit, and
    # `register_refund`'s own linkage invariants (INV-027) -- so this pass
    # is not duplicated there.
    signed_amount = -amount if typed_action == "create_expense" else amount
    duplicate = _check_possible_duplicate(
        db,
        household_id=household_id,
        account_id=accounts[0].id,
        signed_amount=signed_amount,
        booked_at=booked_at,
        description=description,
    )
    if duplicate is not None:
        duplicate_transaction_id = duplicate["transaction"]["id"]
        resolved = (
            duplicate_resolution is not None
            and duplicate_resolution.transaction_id == duplicate_transaction_id
            and duplicate_resolution.decision in _DUPLICATE_DECISIONS
        )
        if not resolved:
            return TypedActionProposal(
                can_execute=False,
                clarifying_question=(
                    f'Encontrei um lançamento parecido já registrado: "{duplicate["transaction"]["label"]}". '
                    "Quer pular, importar mesmo assim, ou ver o existente?"
                ),
                candidates=(duplicate["transaction"],),
                candidate_kind="possible_duplicate",
            )
        if duplicate_resolution.decision == "skip":
            return TypedActionProposal(
                can_execute=False,
                clarifying_question="Ok, não registrei esse lançamento.",
            )
        if duplicate_resolution.decision == "view_existing":
            return TypedActionProposal(
                can_execute=False,
                clarifying_question="Aqui está o lançamento existente.",
                candidates=(duplicate["transaction"],),
                candidate_kind="possible_duplicate",
            )
        # decision == "import_anyway": fall through and build the normal
        # executable proposal below. `register_transaction_duplicates`
        # still runs at execute time and still flags/reviews it, exactly
        # like the manual-entry form already does.

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


def _propose_update_asset_value(
    db: Session, *, household_id: str, fields: dict[str, str]
) -> TypedActionProposal:
    """`update_asset_value` (October Go-Live Slice 6): rebaseline §16.3 --
    "Hoje acho que o Studio vale 35 mil" updates `current_value`; "A
    previsão agora é receber 45 mil" updates `expected_receivable_value`.
    Never guesses which field a bare amount refers to: Codex must supply
    `asset_value_kind_hint`, or this asks."""

    hint = fields.get("target_hint") or fields.get("counterparty_hint") or fields.get("description")
    if not hint:
        return _needs_disambiguation("Qual ativo/investimento você quer atualizar?", missing_fields=("target",))
    investments = _candidate_investments(db, household_id=household_id, hint=hint)
    if len(investments) == 0:
        return _needs_disambiguation(f'Não encontrei nenhum investimento cadastrado parecido com "{hint}".')
    if len(investments) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de um investimento parecido. Qual deles?",
            candidates=tuple(_serialize_candidate(item, "investment") for item in investments),
            candidate_kind="investment",
        )
    investment = investments[0]

    amount = parse_amount_text(fields.get("amount_text"))
    if amount is None:
        return _needs_disambiguation("Qual é o novo valor?", missing_fields=("amount",))

    value_kind = fields.get("asset_value_kind_hint")
    if value_kind not in ("current_value", "expected_receivable_value"):
        return _needs_disambiguation(
            "Esse valor é o valor de hoje do ativo ou o valor previsto a receber no futuro?"
        )

    valuation_date = parse_date_text(fields.get("date_text")) or date.today()
    payload: dict[str, Any] = {"valuation_date": valuation_date.isoformat()}
    payload[value_kind] = str(amount)
    return TypedActionProposal(
        can_execute=True,
        typed_action="update_asset_value",
        payload=payload,
        path_params={"investment_id": investment.id},
    )


def _propose_register_asset_contribution(
    db: Session, *, household_id: str, fields: dict[str, str]
) -> TypedActionProposal:
    """`register_asset_contribution` (October Go-Live Slice 6): rebaseline
    §16.3 -- "Coloquei mais 5 mil no Studio" increases `historical_cost`
    and asks the funding origin when it is materially undefined, exactly
    like `_propose_register_refund` requires the refund transaction to
    already exist rather than fabricating it (Work Order "sem fabricar ...
    origem de caixa")."""

    hint = fields.get("target_hint") or fields.get("counterparty_hint") or fields.get("description")
    if not hint:
        return _needs_disambiguation("Em qual ativo/investimento você fez esse aporte?", missing_fields=("target",))
    investments = _candidate_investments(db, household_id=household_id, hint=hint)
    if len(investments) == 0:
        return _needs_disambiguation(f'Não encontrei nenhum investimento cadastrado parecido com "{hint}".')
    if len(investments) > 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question="Encontrei mais de um investimento parecido. Em qual deles?",
            candidates=tuple(_serialize_candidate(item, "investment") for item in investments),
            candidate_kind="investment",
        )
    investment = investments[0]

    amount = parse_amount_text(fields.get("amount_text"))
    if amount is None:
        return _needs_disambiguation("Qual foi o valor do aporte?", missing_fields=("amount",))

    funding_candidates = candidate_investment_funding_transactions(
        db, household_id=household_id, amount=amount
    )
    if len(funding_candidates) != 1:
        return TypedActionProposal(
            can_execute=False,
            clarifying_question=(
                "De onde saiu esse aporte? Registre a saída em Saídas como \"Transferência "
                "patrimonial\" primeiro, ou me diga qual lançamento já existente corresponde a "
                "esse aporte."
            ),
            candidates=tuple(_serialize_candidate(item, "transaction") for item in funding_candidates),
            candidate_kind="transaction" if funding_candidates else None,
        )
    funding_transaction = funding_candidates[0]

    valuation_date = parse_date_text(fields.get("date_text")) or date.today()
    payload = {
        "contribution_amount": str(amount),
        "valuation_date": valuation_date.isoformat(),
        "funding_transaction_id": funding_transaction.id,
    }
    return TypedActionProposal(
        can_execute=True,
        typed_action="register_asset_contribution",
        payload=payload,
        path_params={"investment_id": investment.id},
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
    "create_expense": _TypedActionSpec("ManualTransactionRequest", "_create_manual_transaction_impl"),
    "create_income": _TypedActionSpec("ManualTransactionRequest", "_create_manual_transaction_impl"),
    "create_internal_transfer": _TypedActionSpec("TransferRequest", "_create_manual_transfer_impl"),
    "pay_obligation": _TypedActionSpec(
        "ObligationPaymentRequest", "_pay_obligation_impl", ("obligation_id",), entity_type="obligation"
    ),
    "pay_card_invoice": _TypedActionSpec(
        "CardInvoicePayRequest",
        "_pay_card_invoice_lifecycle_impl",
        ("invoice_id",),
        entity_type="card_invoice",
    ),
    "register_refund": _TypedActionSpec(
        "RefundLinkRequest", "_link_refund_transaction_impl", ("transaction_id",)
    ),
    "update_asset_value": _TypedActionSpec(
        "InvestmentValuationRequest",
        "_update_investment_value_impl",
        ("investment_id",),
        entity_type="investment",
    ),
    "register_asset_contribution": _TypedActionSpec(
        "InvestmentContributionRequest",
        "_register_investment_contribution_impl",
        ("investment_id",),
        entity_type="investment",
    ),
}

# Typed actions whose dispatched `_impl` mutates an *existing* entity and
# therefore accepts `domain_audit_sink` so `execute_typed_action` can copy
# its canonical before/after state -- see `_pay_obligation_impl`'s
# docstring and blocker 2 of the engineering review of PR #92. The other
# three typed actions *create* a new row (`before_state=None` is the
# accepted shape for a create, per that same review) and their `_impl`s do
# not accept the parameter at all. `update_asset_value`/
# `register_asset_contribution` (October Go-Live Slice 6) also mutate an
# existing `Investment`, so they join this set too.
_ENTITY_MUTATION_TYPED_ACTIONS = frozenset(
    {
        "pay_obligation",
        "pay_card_invoice",
        "register_refund",
        "update_asset_value",
        "register_asset_contribution",
    }
)


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
    if typed_action in ("update_asset_value", "register_asset_contribution"):
        # `investment_id` first, `valuation_id` second -- `undo_assistant_action`
        # needs both to invalidate the exact `InvestmentValuation` row this
        # event created, not just "the latest one" (see
        # `app.api._undo_investment_valuation_impl`).
        ids = [path_params["investment_id"]]
        if response.get("valuation_id"):
            ids.append(str(response["valuation_id"]))
        return ids
    return []


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


def persist_action_proposal(
    db: Session,
    *,
    household_id: str,
    user_id: str,
    trace_id: str,
    original_message: str,
    structured_interpretation: dict[str, Any],
    proposal: TypedActionProposal,
) -> str:
    """Persists a ready-to-execute proposal so `execute_typed_action` can
    later dispatch it without trusting any `typed_action`/`payload`/
    `path_params`/`structured_interpretation` the HTTP caller resupplies --
    engineering review of PR #92, blocker 1; see module docstring
    "Autoridade de execução". Called by `POST /assistant/interpret` only
    when `proposal.can_execute` is true: a proposal still needing
    disambiguation carries nothing `execute_typed_action` could ever
    dispatch, so there is nothing to protect by persisting it. Commits on
    its own -- this is the one thing `/assistant/interpret` writes."""

    if not proposal.can_execute or proposal.typed_action is None:
        raise ValueError("Somente uma proposta pronta para executar pode ser persistida")

    from app.models import AssistantActionProposal

    row = AssistantActionProposal(
        household_id=household_id,
        user_id=user_id,
        trace_id=trace_id,
        original_message=original_message[:2000],
        structured_interpretation=_json_safe(structured_interpretation),
        typed_action=proposal.typed_action,
        payload=_json_safe(proposal.payload) or {},
        path_params=_json_safe(proposal.path_params) or {},
        expires_at=datetime.now(UTC) + timedelta(minutes=PROPOSAL_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    return row.id


def execute_typed_action(
    db: Session,
    *,
    user: Any,
    proposal_id: str,
    disambiguation_qa: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dispatch and audit the one typed action `proposal_id` names.

    Every mutation-relevant field (`typed_action`/`payload`/`path_params`/
    `original_message`/`structured_interpretation`/`trace_id`) is read back
    from the `AssistantActionProposal` row `POST /assistant/interpret`
    already persisted -- never from an argument the HTTP caller could
    supply directly (engineering review of PR #92, blocker 1).

    Idempotency: a proposal is single-use. A retry with the same
    `proposal_id` after it was already consumed returns the original
    result without executing anything a second time -- a stronger,
    unambiguous key than a client-supplied `trace_id`. This is a
    convenience layer on top of, never a replacement for, each underlying
    endpoint's own duplicate-detection/fingerprint guard, which still
    applies unconditionally.

    Concurrency (engineering review of PR #92, second round): the proposal
    row is loaded with `SELECT ... FOR UPDATE` on PostgreSQL (a no-op on
    SQLite, same dialect gate as `lock_household_financial_revision` --
    SQLite has no cross-connection row lock and every test that needs true
    concurrency runs against real PostgreSQL) and that lock is held for the
    rest of this function, through the domain dispatch and the final
    `db.commit()` below. Two concurrent `execute_typed_action` calls racing
    on the same `proposal_id` therefore cannot both observe
    `consumed_at IS NULL`: the second blocks on the lock until the first
    commits or rolls back. If the first committed, the second's own locked
    read now sees `consumed_at`/`consumed_action_event_id` already set and
    takes the idempotent-replay branch below -- never a second dispatch. If
    the first rolled back (any exception before its commit), the lock is
    released with `consumed_at` still `NULL`, so the second proceeds as a
    legitimate first execution.

    Atomicity (blocker 5): the dispatched `_impl` is called with
    `commit=False`, so its domain write and this function's own
    `AssistantActionEvent`/proposal-consumption write share one open
    transaction, finalized by the single `db.commit()` at the end; any
    failure anywhere in between rolls back the whole thing, so a financial
    fact can never end up persisted without its Assistant audit trail.
    """

    import app.api as api
    import app.schemas as schemas
    from app.models import AssistantActionEvent
    from app.models import AssistantActionProposal as AssistantActionProposalModel
    from app.models import AuditEvent as AuditEventModel

    proposal_query = select(AssistantActionProposalModel).where(
        AssistantActionProposalModel.id == proposal_id,
        AssistantActionProposalModel.household_id == user.household_id,
    )
    if db.get_bind().dialect.name == "postgresql":
        # `populate_existing()` is not optional here (engineering review,
        # WA-03 issue #75): the WA-02 Tool Layer's own
        # `app.services.assistant_tools._find_pending_proposal` already ran
        # an unlocked `SELECT` for this exact row, in this exact session,
        # immediately before this call (`confirm_typed_action`) -- so this
        # row is already present in `db`'s identity map with `consumed_at`
        # cached from that earlier, unlocked read. Without
        # `populate_existing()`, SQLAlchemy's identity map returns that
        # *same Python object* for a repeat `SELECT` of the same primary
        # key and does not overwrite its already-loaded attributes with the
        # new query's row -- even though the row itself was just
        # `SELECT ... FOR UPDATE`-locked and re-fetched from PostgreSQL.
        # Reproduced directly: two sessions, A reads a row (cached
        # `flag=False`), B updates+commits it, A re-`SELECT ... FOR UPDATE`s
        # the same row and still sees the stale cached `False` without this
        # option. Concretely here: two concurrent WhatsApp confirmations of
        # the same proposal would otherwise both observe
        # `consumed_at is None` on their locked read -- the second
        # unblocking only to see its own session's stale pre-lock cache,
        # not the first's just-committed `consumed_at` -- and both dispatch,
        # producing two `Transaction`s from one proposal
        # (`tests/test_postgresql_integration.py::test_whatsapp_webhook_concurrent_confirm_from_two_messages_serializes_to_one_mutation`
        # reproduces exactly this without the fix). A direct call into this
        # function alone (`POST /assistant/execute`, or the concurrency test
        # above it in this file) never loaded the proposal earlier in the
        # same session, so it never observed this -- the bug only surfaces
        # once a caller (uniformly: every WA-02+ Tool Layer tool) looks the
        # proposal up once before calling this.
        proposal_query = proposal_query.with_for_update().execution_options(populate_existing=True)
    proposal_row = db.scalar(proposal_query)
    if proposal_row is None:
        raise AssistantActionError("Proposta do Assistente não encontrada")

    if proposal_row.consumed_at is not None:
        if proposal_row.consumed_action_event_id is None:
            raise AssistantActionError("Esta proposta já foi utilizada")
        action_event = db.get(AssistantActionEvent, proposal_row.consumed_action_event_id)
        audit_event = db.get(AuditEventModel, action_event.audit_event_id)
        return {
            "idempotent_replay": True,
            "action": _serialize_action_event(action_event, audit_event=audit_event),
        }

    now = datetime.now(UTC)
    expires_at = proposal_row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and expires_at < now:
        raise AssistantActionError(
            "Esta proposta expirou. Peça a ação novamente para gerar uma nova interpretação."
        )

    typed_action = proposal_row.typed_action
    if typed_action not in TYPED_ACTIONS:
        raise AssistantActionError(f"Ação tipada desconhecida: {typed_action}")

    payload = dict(proposal_row.payload or {})
    path_params = dict(proposal_row.path_params or {})
    trace_id = proposal_row.trace_id

    spec = _TYPED_ACTION_SPECS[typed_action]
    schema_cls = getattr(schemas, spec.schema_name)
    missing_path_params = [name for name in spec.path_param_names if name not in path_params]
    if missing_path_params:
        raise AssistantActionError(f"Parâmetros obrigatórios ausentes: {', '.join(missing_path_params)}")

    if typed_action in ("create_expense", "create_income"):
        # Never trust anything but the `typed_action` this proposal was
        # persisted under to decide `movement_type` -- deriving it here
        # again, unconditionally, means a corrupted/tampered payload can
        # never smuggle `movement_type="income"` under
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

    domain_audit_sink: list[Any] = []
    dispatch_kwargs: dict[str, Any] = {"commit": False}
    if typed_action in _ENTITY_MUTATION_TYPED_ACTIONS:
        dispatch_kwargs["domain_audit_sink"] = domain_audit_sink

    try:
        if typed_action == "pay_obligation":
            response = dispatcher(path_params["obligation_id"], request_payload, user, db, **dispatch_kwargs)
        elif typed_action == "pay_card_invoice":
            response = dispatcher(path_params["invoice_id"], request_payload, user, db, **dispatch_kwargs)
        elif typed_action == "register_refund":
            response = dispatcher(path_params["transaction_id"], request_payload, user, db, **dispatch_kwargs)
        elif typed_action in ("update_asset_value", "register_asset_contribution"):
            response = dispatcher(path_params["investment_id"], request_payload, user, db, **dispatch_kwargs)
        else:
            response = dispatcher(request_payload, user, db, **dispatch_kwargs)
    except Exception:
        db.rollback()
        raise

    target_entity_ids = _target_entity_ids(typed_action, response, path_params)
    undoable, non_reversible_reason = _undo_capability(typed_action)

    if domain_audit_sink:
        # The exact before/after the domain endpoint itself already
        # computed for its own `AuditEvent` -- never recomputed or guessed
        # a second time (engineering review of PR #92, blocker 2).
        domain_event = domain_audit_sink[0]
        before_state = domain_event.before_state
        after_state = domain_event.after_state
    else:
        before_state = None
        after_state = _json_safe(response)

    try:
        # A second, distinct `AuditEvent` from the one the dispatched
        # `_impl` already recorded for the financial fact itself (e.g.
        # `_create_manual_transaction_impl`'s own
        # `"transaction.create_manual"` event) -- this one narrates that
        # the Assistant, specifically, originated the call, and is what
        # `AssistantActionEvent` is 1:1 with.
        audit_event_row = AuditEventModel(
            household_id=user.household_id,
            user_id=user.id,
            event_type="assistant.execute",
            entity_type=spec.entity_type,
            entity_id=target_entity_ids[0] if target_entity_ids else None,
            details=json.dumps(
                {"typed_action": typed_action, "response": response}, ensure_ascii=False, default=str
            ),
            trace_id=trace_id,
            source="assistant",
        )
        db.add(audit_event_row)
        db.flush()
        action_event = AssistantActionEvent(
            audit_event_id=audit_event_row.id,
            original_message=proposal_row.original_message,
            structured_interpretation=proposal_row.structured_interpretation,
            disambiguation_qa=disambiguation_qa or [],
            typed_action=typed_action,
            target_entity_type=spec.entity_type,
            target_entity_ids=target_entity_ids,
            before_state=before_state,
            after_state=after_state,
            undoable=undoable,
            non_reversible_reason=non_reversible_reason,
        )
        db.add(action_event)
        db.flush()
        proposal_row.consumed_at = now
        proposal_row.consumed_action_event_id = action_event.id
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "idempotent_replay": False,
        "response": response,
        "action": _serialize_action_event(action_event, audit_event=audit_event_row),
    }


def cancel_action_proposal(db: Session, *, user: Any, proposal_id: str) -> dict[str, Any]:
    """WA-03 (`docs/WORK_ORDER_WA_03.md`, issue #75): explicit cancellation
    of a still-pending proposal -- "cancelamento não executa draft" plus
    "auditoria completa de ... cancelamento" (Work Order items 6/9). Not a
    new authority surface: cancelling never dispatches `TYPED_ACTIONS`, it
    only marks the proposal consumed-without-execution, the same
    `consumed_at`/`consumed_action_event_id IS NULL` combination
    `execute_typed_action`'s idempotent-replay branch already treats as
    "already used" -- so a stale "confirmar" arriving after a cancel takes
    that existing branch and raises the existing "Esta proposta já foi
    utilizada" message rather than a new one, no dispatcher change needed.

    Concurrency mirrors `execute_typed_action` exactly: `SELECT ... FOR
    UPDATE` on PostgreSQL, held through the single `db.commit()` below, so a
    confirm and a cancel racing on the same `proposal_id` can never both
    succeed -- whichever gets the lock first commits `consumed_at`, the
    second sees it already set and fails closed instead of double-acting on
    one proposal (`AssistantActionError`, treated as an idempotent no-op by
    callers, never a second write).

    Never used for an already-executed proposal: a proposal is either
    confirmed (a financial fact now exists) or cancelled (it never will) --
    the two are mutually exclusive by construction, since both take the
    same lock and check the same `consumed_at IS NULL` guard.
    """

    from app.models import AssistantActionProposal as AssistantActionProposalModel
    from app.models import AuditEvent as AuditEventModel

    proposal_query = select(AssistantActionProposalModel).where(
        AssistantActionProposalModel.id == proposal_id,
        AssistantActionProposalModel.household_id == user.household_id,
    )
    if db.get_bind().dialect.name == "postgresql":
        # `populate_existing()`: same identity-map staleness fix as
        # `execute_typed_action` above, and for the identical reason --
        # `app.services.assistant_tools._find_pending_proposal` already ran
        # an unlocked `SELECT` for this row, in this same session,
        # immediately before this call (`cancel_typed_action`).
        proposal_query = proposal_query.with_for_update().execution_options(populate_existing=True)
    proposal_row = db.scalar(proposal_query)
    if proposal_row is None:
        raise AssistantActionError("Proposta do Assistente não encontrada")
    if proposal_row.consumed_at is not None:
        raise AssistantActionError("Esta proposta já foi utilizada ou cancelada")

    try:
        audit_event_row = AuditEventModel(
            household_id=user.household_id,
            user_id=user.id,
            event_type="assistant.cancel",
            entity_type=None,
            entity_id=None,
            details=json.dumps(
                {"typed_action": proposal_row.typed_action, "proposal_id": proposal_row.id},
                ensure_ascii=False,
            ),
            trace_id=proposal_row.trace_id,
            source="assistant",
        )
        db.add(audit_event_row)
        db.flush()
        proposal_row.consumed_at = datetime.now(UTC)
        proposal_row.consumed_action_event_id = None
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"cancelled": True, "typed_action": proposal_row.typed_action}


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
    """Reverse the one typed action `action_id` names.

    Concurrency (engineering review of PR #105, MERGE BLOCKED comment): same
    contract as `execute_typed_action`/`cancel_action_proposal` above --
    `SELECT ... FOR UPDATE` on PostgreSQL, held through the reversal's own
    domain write and the final `db.commit()` below, so two concurrent undo
    attempts on the same `action_id` (e.g. two WhatsApp "desfaz" messages
    arriving together) can never both observe `undone_at IS NULL`: the
    second blocks on the lock until the first commits or rolls back, then
    its own locked read sees `undone_at` already set and fails closed with
    "Esta ação já foi desfeita" instead of reversing a second time.

    `populate_existing()` is required for the identical reason documented
    on `execute_typed_action`'s locking `SELECT`: every caller here
    (`app.services.assistant_tools._tool_undo_typed_action`) already ran an
    unlocked `_find_undoable_action` `SELECT` for this exact row, in this
    exact session, immediately before calling this function. Without
    `populate_existing()`, SQLAlchemy's identity map would hand back that
    same, pre-lock Python object -- with `undone_at` still cached as
    `None` -- even after this `SELECT ... FOR UPDATE` re-fetches the row
    from PostgreSQL and finds it already undone by the concurrent winner.
    """

    import app.api as api
    import app.schemas as schemas
    from app.models import AssistantActionEvent, AuditEvent

    action_query = (
        select(AssistantActionEvent)
        .join(AuditEvent, AuditEvent.id == AssistantActionEvent.audit_event_id)
        .where(AssistantActionEvent.id == action_id, AuditEvent.household_id == user.household_id)
    )
    if db.get_bind().dialect.name == "postgresql":
        action_query = action_query.with_for_update(of=AssistantActionEvent).execution_options(
            populate_existing=True
        )
    action_event = db.scalar(action_query)
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

    # `commit=False` on every reversal `_impl` call below -- same
    # atomicity contract as `execute_typed_action` (blocker 5): the
    # reversal's own domain write and this function's own undo
    # bookkeeping/`AuditEvent` are one transaction, finalized by the single
    # `db.commit()` at the end.
    try:
        if typed_action in ("create_expense", "create_income", "create_internal_transfer"):
            if not target_ids:
                raise AssistantActionError("Não há lançamento para desfazer")
            api._delete_manual_transaction_impl(target_ids[0], user, db, commit=False)
        elif typed_action == "pay_obligation":
            if not target_ids:
                raise AssistantActionError("Não há obrigação para desfazer")
            api._unpay_obligation_impl(
                target_ids[0], schemas.ObligationPaymentUndoRequest(reason=reason), user, db, commit=False
            )
        elif typed_action == "register_refund":
            if not target_ids:
                raise AssistantActionError("Não há vínculo de estorno para desfazer")
            api._unlink_refund_transaction_impl(
                target_ids[0],
                schemas.RefundUnlinkRequest(refund_transaction_id=target_ids[0], reason=reason),
                user,
                db,
                commit=False,
            )
        elif typed_action in ("update_asset_value", "register_asset_contribution"):
            if len(target_ids) < 2:
                raise AssistantActionError("Não há avaliação/aporte de ativo para desfazer")
            api._undo_investment_valuation_impl(
                target_ids[0], target_ids[1], reason, user, db, commit=False
            )
        else:
            raise AssistantActionError(
                action_event.non_reversible_reason or "Esta ação não pode ser desfeita automaticamente"
            )

        # A distinct `AuditEvent` for the reversal itself -- in addition to
        # whichever event the reversal `_impl` above already recorded for
        # its own domain (`"obligation.unpay"`, `"refund.unlink"`,
        # `"transaction.delete_manual"`/`"transfer.delete"`). Never
        # overwrites or deletes the original `assistant.execute` event: the
        # trail grows, it never shrinks.
        from app.models import AuditEvent as AuditEventModel

        undo_audit_event = AuditEventModel(
            household_id=user.household_id,
            user_id=user.id,
            event_type="assistant.undo",
            entity_type=action_event.target_entity_type,
            entity_id=target_ids[0] if target_ids else None,
            details=json.dumps(
                {"typed_action": typed_action, "action_event_id": action_event.id}, ensure_ascii=False
            ),
            reason=reason,
            source="assistant",
        )
        db.add(undo_audit_event)
        db.flush()

        action_event.undone_at = datetime.now(UTC)
        action_event.undone_by = user.id
        action_event.undo_audit_event_id = undo_audit_event.id
        db.commit()
    except Exception:
        db.rollback()
        raise
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
