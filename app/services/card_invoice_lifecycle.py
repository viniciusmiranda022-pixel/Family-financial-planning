"""Card-invoice lifecycle: `open -> closed -> partially_paid -> paid`.

P0 #87, October Go-Live Slice 2 (`docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_2.md`,
`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.1/§4). Introduces `CardInvoice`
as the canonical entity for a card's billing cycle, layered on top of
already-canonical facts instead of a second financial engine:

- competence/cycle boundaries: `app.services.card_competence`
  (`card_invoice_competence`/`card_invoice_window`), never duplicated here;
- which purchases belong to a cycle: `Transaction.competence`, already set
  by every purchase path (manual entry, Smart Capture, PDF import) through
  `app.services.card_competence.resolve_expense_competence`;
- the checking-side settlement leg's shape and duplicate-detection
  contract: reused verbatim from `app.services.card_payment_reconciliation`
  (`_is_card_payment_direction`, `register_transaction_duplicates`, the
  exact same `ParsedTransaction`/`transaction_fingerprint` construction
  `pay_card_invoice` uses) -- never a second reconciliation policy.

`CardInvoice.computed_total`/`declared_total` are refreshed from
`Transaction` history every time `get_or_sync_invoice` runs (the project's
"leitura preguiçosa no primeiro acesso" trigger -- there is no cron in this
slice, so every write action below calls it first); `principal_carried_in`/
`principal_carried_out` are the one pair of cache columns that also carry
real state (how much of a *closed* cycle's balance was actually left unpaid
at last sync) -- never independently edited, always derived from the prior
competence's own `computed_total + principal_carried_in - paid_total`.
`paid_total`/`status`/`closed_at` are genuine persisted facts, written only
by `close_invoice`/`pay_invoice` below.

Nothing here ever fabricates a purchase, a payment, an interest/IOF charge,
a resgate or an aplicação: every number is either a deterministic sum of
already-persisted `Transaction` rows or an explicit, human-confirmed
payment this module records as a new `reconciliation`-type `Transaction`
(never a new economic expense -- INV-002 holds by construction, exactly
like `pay_card_invoice`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.card_competence import card_invoice_window
from app.services.finance import add_months, money, month_key
from app.services.reconciliation import RECONCILIATION_TOLERANCE

# Same category name every card-payment leg in this project already uses
# (`app.api.DEFAULT_CATEGORIES`); interest/IOF/late fees are ordinary
# expenses tagged with this pre-existing category, never a new taxonomy.
FINANCE_CHARGE_CATEGORY_NAME = "Juros, IOF e tarifas"


class CardInvoiceError(ValueError):
    """Raised for a card-invoice lifecycle request that conflicts with current state."""


@dataclass(frozen=True, slots=True)
class InvoiceDivergence:
    status: str  # reconciled | unreconciled_explained | unreconciled_unexplained | not_applicable
    declared_total: Decimal | None
    expected_total: Decimal
    difference: Decimal | None
    explanations: tuple[dict[str, Any], ...]


def _competence_month(competence: str) -> date:
    year, month = (int(part) for part in competence.split("-"))
    return date(year, month, 1)


def _previous_competence(competence: str) -> str:
    return month_key(add_months(_competence_month(competence), -1))


def outstanding_balance(invoice: Any) -> Decimal:
    """Amount still owed on `invoice` right now: this cycle's purchases plus
    whatever principal it inherited, minus whatever has already been paid
    against it -- never negative (this slice does not model a card credit
    balance; see `pay_invoice`)."""

    total_due = invoice.computed_total + invoice.principal_carried_in
    return max(Decimal("0"), money(total_due - invoice.paid_total))


def get_or_sync_invoice(
    db: Session,
    *,
    household_id: str,
    account: Any,
    competence: str,
    as_of: date | None = None,
) -> Any:
    """Idempotent get-or-create + deterministic resync of the `CardInvoice`
    for `(account, competence)`. The one mutating entrypoint of this
    module -- every write action below (`close_invoice`, `pay_invoice`,
    `invoice_divergence`) calls this first so its figures are never stale.

    1. get-or-create the row (unique on `(account_id, competence)`,
       migration 0015);
    2. claim every still-unclaimed `expense` `Transaction` of this exact
       `(account_id, competence)` by setting its `card_invoice_id` --
       additive only, never overwrites an existing link, so a transaction
       is claimed by an invoice exactly once no matter how many times this
       runs;
    3. recompute `computed_total` as the live sum of every `expense`
       `Transaction` of this `(account_id, competence)` -- always derived,
       never an independently stored fact;
    4. recompute `declared_total` from any already-imported bank/card-
       statement `reconciliation` "payment received" line of this exact
       `(account_id, competence)`, when one exists -- evidence, never a
       human-typed correction;
    5. recompute `principal_carried_in` from the immediately preceding
       competence's own invoice outstanding balance (if that invoice
       exists), mirroring the same figure onto that prior invoice's
       `principal_carried_out` for audit display;
    6. if `status == "open"` and `as_of >= closes_at`, transition to
       `closed` -- idempotent; an already `closed`/`partially_paid`/`paid`
       row is never reopened here, only `pay_invoice` moves those forward.
    """

    from app.models import CardInvoice, Transaction

    as_of = as_of or date.today()
    invoice = db.scalar(
        select(CardInvoice).where(
            CardInvoice.household_id == household_id,
            CardInvoice.account_id == account.id,
            CardInvoice.competence == competence,
        )
    )
    opens_at, closes_at, due_date = card_invoice_window(account, competence)
    if invoice is None:
        invoice = CardInvoice(
            household_id=household_id,
            account_id=account.id,
            competence=competence,
            opens_at=opens_at,
            closes_at=closes_at,
            due_date=due_date,
            status="open",
            trace_id=str(uuid.uuid4()),
        )
        db.add(invoice)
        db.flush()
    elif invoice.opens_at is None or invoice.closes_at is None or invoice.due_date is None:
        # Defensive backfill for a row created before the window was
        # computed (should not happen through this function's own writes).
        invoice.opens_at, invoice.closes_at, invoice.due_date = opens_at, closes_at, due_date

    unclaimed = db.scalars(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.account_id == account.id,
            Transaction.competence == competence,
            Transaction.transaction_type == "expense",
            Transaction.card_invoice_id.is_(None),
        )
    ).all()
    for txn in unclaimed:
        txn.card_invoice_id = invoice.id
    if unclaimed:
        # The aggregate query right below reads `card_invoice_id` back from
        # the database, not from these pending in-memory attribute writes
        # -- without an explicit flush here, a session with `autoflush`
        # disabled (as the test suite's isolated sessions use) would sum
        # over the *previous* claim state and silently undercount.
        db.flush()

    gross_purchases = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.card_invoice_id == invoice.id,
            Transaction.transaction_type == "expense",
        )
    )
    # Only an *explicitly linked* refund (`link_refund`, never inferred)
    # reduces the amount owed on the invoice it falls into -- rebaseline
    # §6.6: a refund posted in a later cycle is a credit on that later
    # cycle, never a silent rewrite of the (possibly already-paid) invoice
    # the original purchase belonged to. An unlinked refund transaction
    # never touches any invoice total until a human/Assistant confirms the
    # link.
    linked_refunds = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.household_id == household_id,
            Transaction.account_id == account.id,
            Transaction.competence == competence,
            Transaction.transaction_type == "refund",
            Transaction.refund_of_transaction_id.is_not(None),
        )
    )
    invoice.computed_total = money(
        max(Decimal("0"), abs(Decimal(gross_purchases or 0)) - Decimal(linked_refunds or 0))
    )

    declared_row = db.scalar(
        select(Transaction)
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id == account.id,
            Transaction.competence == competence,
            Transaction.transaction_type == "reconciliation",
        )
        .order_by(Transaction.booked_at.desc(), Transaction.id.desc())
    )
    invoice.declared_total = money(abs(declared_row.amount)) if declared_row is not None else None

    previous_invoice = db.scalar(
        select(CardInvoice).where(
            CardInvoice.household_id == household_id,
            CardInvoice.account_id == account.id,
            CardInvoice.competence == _previous_competence(competence),
        )
    )
    if previous_invoice is not None:
        previous_outstanding = outstanding_balance(previous_invoice)
        invoice.principal_carried_in = previous_outstanding
        previous_invoice.principal_carried_out = previous_outstanding
    else:
        invoice.principal_carried_in = Decimal("0")

    if invoice.status == "open" and as_of >= invoice.closes_at:
        invoice.status = "closed"
        invoice.closed_at = datetime.now(UTC)

    return invoice


def close_invoice(
    db: Session,
    *,
    household_id: str,
    invoice_id: str,
    as_of: date | None = None,
) -> Any:
    """Explicit `POST /card-invoices/{id}/close`. Idempotent: closing an
    already-`closed`/`partially_paid`/`paid` invoice is a no-op resync, not
    an error. Fails closed (never force-closes early) when the invoice's
    own `closes_at` has not been reached yet -- rebaseline §6.2, the
    lifecycle transition is a real calendar event, not a user whim."""

    from app.models import Account, CardInvoice

    invoice = db.get(CardInvoice, invoice_id)
    if invoice is None or invoice.household_id != household_id:
        raise LookupError("invoice not found")
    account = db.get(Account, invoice.account_id)
    if account is None or account.household_id != household_id:
        raise LookupError("invoice not found")

    invoice = get_or_sync_invoice(
        db, household_id=household_id, account=account, competence=invoice.competence, as_of=as_of
    )
    if invoice.status == "open":
        raise CardInvoiceError(
            f"Esta fatura só fecha em {invoice.closes_at.isoformat()}; ainda não é possível fechá-la"
        )
    return invoice


def pay_invoice(
    db: Session,
    *,
    household_id: str,
    invoice_id: str,
    paying_account: Any,
    category: Any,
    amount: Decimal,
    booked_at: date,
    description: str,
) -> tuple[Any, Any, Any]:
    """Manual payment of a `CardInvoice` -- superset of
    `card_payment_reconciliation.pay_card_invoice`: accepts any amount up
    to the invoice's current `outstanding_balance`, not only an exact
    match (rebaseline §6.5).

    Each call creates its own `reconciliation`-type, `excluded=True`
    checking-side leg -- never a new economic expense (INV-002) -- and adds
    its amount to `CardInvoice.paid_total`. A second, later call for the
    remaining balance is exactly how a real partial payment is completed;
    neither call ever recreates or duplicates a leg for a previous call's
    payment, and the unpaid remainder is never itself materialized as a
    new compra/expense -- it only ever changes `principal_carried_in` of
    the *next* cycle's invoice, the next time that cycle is synced.

    Never accepts more than `outstanding_balance` (+ the standard R$ 0.01
    tolerance): this slice does not model a card credit balance, so an
    attempted overpayment fails closed instead of fabricating one.

    Raises `LookupError` (-> 404) when the invoice/account do not resolve
    to this household, `CardInvoiceError` (-> 409) for any business-rule
    conflict (still open, already paid, invalid amount/account).

    Atomicity: like `pay_card_invoice`, nothing here calls `db.commit()` --
    the caller commits once after this call (and its duplicate-detection
    pass) succeeds.
    """

    import uuid as uuid_module

    from app.models import Account, CardInvoice, Transaction
    from app.services.classifier import normalize_description
    from app.services.duplicates import register_transaction_duplicates, source_priority
    from app.services.importer import PARSER_CONTRACT_VERSION, ParsedTransaction, transaction_fingerprint

    invoice = db.get(CardInvoice, invoice_id)
    if invoice is None or invoice.household_id != household_id:
        raise LookupError("invoice not found")
    account = db.get(Account, invoice.account_id)
    if account is None or account.household_id != household_id:
        raise LookupError("invoice not found")

    invoice = get_or_sync_invoice(
        db, household_id=household_id, account=account, competence=invoice.competence
    )
    if invoice.status == "open":
        raise CardInvoiceError("A fatura ainda não fechou; aguarde a data de fechamento para pagar")
    if invoice.status == "paid":
        raise CardInvoiceError("Esta fatura já está totalmente paga")

    if paying_account.household_id != household_id:
        raise CardInvoiceError("A conta pagadora precisa pertencer a esta família")
    if not paying_account.active:
        raise CardInvoiceError("A conta pagadora precisa estar ativa")
    if paying_account.account_type != "checking":
        raise CardInvoiceError(
            "A conta pagadora precisa ser uma conta corrente; o contrato de pagamento de fatura só "
            "reconhece débito bancário nesse tipo de conta"
        )

    positive_amount = money(abs(amount))
    if positive_amount <= 0:
        raise CardInvoiceError("O valor pago deve ser maior que zero")
    outstanding = outstanding_balance(invoice)
    if positive_amount > outstanding + RECONCILIATION_TOLERANCE:
        raise CardInvoiceError(
            f"O valor pago (R$ {positive_amount}) excede o saldo em aberto desta fatura "
            f"(R$ {outstanding}); este contrato não representa saldo credor de cartão"
        )

    # Same financial direction `card_payment_reconciliation.
    # _is_card_payment_direction` enforces for the legacy contract: the
    # checking-side leg is always a negative bank debit. `positive_amount`
    # is already validated `> 0` above, so `debit_amount` is always
    # negative by construction -- no separate direction policy needed here.
    debit_amount = -positive_amount

    parsed = ParsedTransaction(
        booked_at=booked_at,
        description=description,
        amount=money(debit_amount),
        source_line=1,
        card_last_four=paying_account.last_four,
    )
    checking_leg = Transaction(
        household_id=household_id,
        account_id=paying_account.id,
        category_id=category.id,
        booked_at=booked_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=money(debit_amount),
        transaction_type="reconciliation",
        owner_label=paying_account.owner_label,
        card_last_four=paying_account.last_four,
        fingerprint=transaction_fingerprint(paying_account.id, parsed, paying_account.owner_label),
        occurred_at=booked_at,
        competence=booked_at.strftime("%Y-%m"),
        classification_source="manual_confirmed",
        classification_version=PARSER_CONTRACT_VERSION,
        canonical_status="unassigned",
        trace_id=str(uuid_module.uuid4()),
        source_priority=source_priority("manual"),
        confidence=Decimal("1"),
        excluded=True,
        possible_duplicate=False,
        reviewed=True,
        card_invoice_id=invoice.id,
    )
    db.add(checking_leg)
    db.flush()

    _, duplicate_assessment = register_transaction_duplicates(
        db, transaction=checking_leg, household_id=household_id
    )
    if duplicate_assessment is not None:
        checking_leg.reviewed = False

    invoice.paid_total = money(invoice.paid_total + positive_amount)
    new_outstanding = outstanding_balance(invoice)
    invoice.status = "paid" if new_outstanding <= RECONCILIATION_TOLERANCE else "partially_paid"

    return checking_leg, invoice, duplicate_assessment


def invoice_divergence(db: Session, *, household_id: str, invoice_id: str) -> InvoiceDivergence:
    """`GET /card-invoices/{id}/divergence` -- rebaseline §6.4.

    Never adjusts silently: when `declared_total` (an already-imported
    bank/card-statement line) diverges from `computed_total +
    principal_carried_in` beyond the standard tolerance, this looks for a
    deterministic, evidenced, not-yet-counted cause -- an unlinked refund
    booked in this exact competence, or a Juros/IOF/tarifas expense booked
    within this cycle's window that has not yet been claimed by any
    invoice -- and reports it as `explanations`. A cause that is *already*
    counted in `computed_total` can never explain a *remaining* gap, so
    neither search ever looks at already-claimed/already-linked rows. If
    nothing is found, `status` stays `unreconciled_unexplained` -- the
    caller must never turn that into a fabricated adjustment.
    """

    from app.models import Account, CardInvoice, Category, Transaction

    invoice = db.get(CardInvoice, invoice_id)
    if invoice is None or invoice.household_id != household_id:
        raise LookupError("invoice not found")
    account = db.get(Account, invoice.account_id)
    if account is None or account.household_id != household_id:
        raise LookupError("invoice not found")

    invoice = get_or_sync_invoice(
        db, household_id=household_id, account=account, competence=invoice.competence
    )
    expected_total = money(invoice.computed_total + invoice.principal_carried_in)

    if invoice.declared_total is None:
        return InvoiceDivergence(
            status="not_applicable",
            declared_total=None,
            expected_total=expected_total,
            difference=None,
            explanations=(),
        )

    difference = money(invoice.declared_total - expected_total)
    if abs(difference) <= RECONCILIATION_TOLERANCE:
        return InvoiceDivergence(
            status="reconciled",
            declared_total=invoice.declared_total,
            expected_total=expected_total,
            difference=difference,
            explanations=(),
        )

    explanations: list[dict[str, Any]] = []
    # A *linked* refund already reduced `computed_total` above -- it can
    # never be the cause of a remaining divergence. An *unlinked* refund in
    # this same competence is exactly the kind of candidate cause rebaseline
    # §6.4 asks the Assistant to surface ("estorno/crédito") -- never
    # auto-linked here, only reported so a human can confirm it via
    # `link_refund`.
    unlinked_refunds = db.scalars(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.account_id == invoice.account_id,
            Transaction.competence == invoice.competence,
            Transaction.transaction_type == "refund",
            Transaction.refund_of_transaction_id.is_(None),
        )
    ).all()
    for refund in unlinked_refunds:
        explanations.append(
            {
                "cause": "estorno_nao_vinculado_no_ciclo",
                "transaction_id": refund.id,
                "amount": str(money(abs(refund.amount))),
            }
        )

    # A finance charge (Juros/IOF/tarifa) already claimed by this invoice is
    # already inside `computed_total` and so can never be the cause of a
    # remaining gap. An *unclaimed* one -- booked within this exact cycle's
    # window but not yet linked to any `CardInvoice` -- is real, evidenced,
    # not-yet-counted evidence of exactly the kind rebaseline §6.4 asks the
    # Assistant to look for.
    unclaimed_charges = db.scalars(
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id == invoice.account_id,
            Transaction.transaction_type == "expense",
            Transaction.card_invoice_id.is_(None),
            Transaction.booked_at >= invoice.opens_at,
            Transaction.booked_at <= invoice.closes_at,
            Category.name == FINANCE_CHARGE_CATEGORY_NAME,
        )
    ).all()
    for charge in unclaimed_charges:
        explanations.append(
            {
                "cause": "juros_iof_tarifa",
                "transaction_id": charge.id,
                "amount": str(money(abs(charge.amount))),
            }
        )

    status = "unreconciled_explained" if explanations else "unreconciled_unexplained"
    return InvoiceDivergence(
        status=status,
        declared_total=invoice.declared_total,
        expected_total=expected_total,
        difference=difference,
        explanations=tuple(explanations),
    )


def link_refund(
    db: Session,
    *,
    household_id: str,
    refund_transaction_id: str,
    original_transaction_id: str,
) -> tuple[Any, Any]:
    """Explicit, human-confirmed link from a `refund` transaction to the
    original `expense` it neutralizes (rebaseline §6.6) -- never inferred
    from amount/date/description alone.

    Multiple partial refunds may point at the same original purchase (a
    partial estorno followed by another later one): the sum of every
    refund already linked to `original_transaction_id`, plus this one,
    must never exceed the original purchase's own amount beyond the
    standard tolerance -- "neutraliza somente o valor estornado", never
    more than was actually spent.
    """

    from app.models import Transaction

    refund = db.get(Transaction, refund_transaction_id)
    if refund is None or refund.household_id != household_id:
        raise LookupError("transaction not found")
    original = db.get(Transaction, original_transaction_id)
    if original is None or original.household_id != household_id:
        raise LookupError("transaction not found")

    if refund.id == original.id:
        raise CardInvoiceError("Selecione dois lançamentos diferentes")
    if refund.transaction_type != "refund":
        raise CardInvoiceError("O lançamento informado como estorno precisa ser do tipo 'refund'")
    if original.transaction_type != "expense":
        raise CardInvoiceError("O lançamento original precisa ser uma despesa")
    if refund.amount <= 0:
        raise CardInvoiceError("O valor do estorno precisa ser positivo")
    if refund.refund_of_transaction_id is not None:
        raise CardInvoiceError(
            "Este estorno já está vinculado a uma compra; desvincule antes de vincular a outra"
        )

    already_linked_total = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.household_id == household_id,
            Transaction.refund_of_transaction_id == original.id,
        )
    )
    prospective_total = money(Decimal(already_linked_total or 0) + refund.amount)
    if prospective_total > money(abs(original.amount)) + RECONCILIATION_TOLERANCE:
        raise CardInvoiceError(
            "A soma dos estornos vinculados a esta compra excederia o valor original gasto"
        )

    refund.refund_of_transaction_id = original.id
    return refund, original


def unlink_refund(db: Session, *, household_id: str, refund_transaction_id: str) -> tuple[Any, str]:
    """Reverse a confirmed refund link. Non-destructive: both rows keep
    existing, exactly like `unlink_card_payment`. Returns `(refund,
    previous_original_transaction_id)` so the caller can audit the
    before-state even though `refund.refund_of_transaction_id` is already
    cleared on the returned object.
    """

    from app.models import Transaction

    refund = db.get(Transaction, refund_transaction_id)
    if refund is None or refund.household_id != household_id:
        raise LookupError("transaction not found")
    if refund.refund_of_transaction_id is None:
        raise CardInvoiceError("Este estorno não está vinculado a nenhuma compra")
    previous_original_id = refund.refund_of_transaction_id
    refund.refund_of_transaction_id = None
    return refund, previous_original_id


def list_invoices(
    db: Session,
    *,
    household_id: str,
    account_id: str | None = None,
    period: str | None = None,
) -> list[Any]:
    """Read model for `GET /card-invoices`. Pure read of already-persisted
    rows -- never mutates -- except for the one narrow, documented
    exception the Work Order itself sanctions ("leitura preguiçosa no
    primeiro acesso"): when `account_id` is given, `period` is not, and the
    household's current billing cycle for that card has no `CardInvoice`
    row yet, this creates/syncs exactly that one row so a card's current
    cycle is never invisible before its first purchase or explicit sync.
    No other competence is ever created here; a past cycle with no row
    yet simply does not appear until `POST /card-invoices/sync` (or a
    close/pay/divergence call) creates it.
    """

    from app.models import Account, CardInvoice

    query = select(CardInvoice).where(CardInvoice.household_id == household_id)
    if account_id:
        query = query.where(CardInvoice.account_id == account_id)
    if period:
        query = query.where(CardInvoice.competence == period)
    invoices = list(db.scalars(query.order_by(CardInvoice.competence.desc())))

    if account_id and not period:
        account = db.get(Account, account_id)
        if account is not None and account.household_id == household_id and account.account_type == "credit_card":
            from app.services.card_competence import card_invoice_competence

            current_competence = card_invoice_competence(account, date.today())
            if not any(invoice.competence == current_competence for invoice in invoices):
                current = get_or_sync_invoice(
                    db, household_id=household_id, account=account, competence=current_competence
                )
                invoices.insert(0, current)
                invoices.sort(key=lambda invoice: invoice.competence, reverse=True)

    return invoices


def serialize_card_invoice(invoice: Any) -> dict[str, Any]:
    return {
        "id": invoice.id,
        "account_id": invoice.account_id,
        "competence": invoice.competence,
        "opens_at": invoice.opens_at.isoformat() if invoice.opens_at else None,
        "closes_at": invoice.closes_at.isoformat() if invoice.closes_at else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "status": invoice.status,
        "declared_total": str(invoice.declared_total) if invoice.declared_total is not None else None,
        "computed_total": str(invoice.computed_total),
        "principal_carried_in": str(invoice.principal_carried_in),
        "principal_carried_out": str(invoice.principal_carried_out),
        "paid_total": str(invoice.paid_total),
        "outstanding_balance": str(outstanding_balance(invoice)),
        "closed_at": invoice.closed_at.isoformat() if invoice.closed_at else None,
    }


def serialize_invoice_divergence(divergence: InvoiceDivergence) -> dict[str, Any]:
    return {
        "status": divergence.status,
        "declared_total": str(divergence.declared_total) if divergence.declared_total is not None else None,
        "expected_total": str(divergence.expected_total),
        "difference": str(divergence.difference) if divergence.difference is not None else None,
        "explanations": list(divergence.explanations),
    }


__all__ = [
    "CardInvoiceError",
    "InvoiceDivergence",
    "close_invoice",
    "get_or_sync_invoice",
    "invoice_divergence",
    "link_refund",
    "list_invoices",
    "outstanding_balance",
    "pay_invoice",
    "serialize_card_invoice",
    "serialize_invoice_divergence",
    "unlink_refund",
]
