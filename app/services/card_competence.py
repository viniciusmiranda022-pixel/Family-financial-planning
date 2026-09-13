"""Canonical card-invoice competence engine.

FAMILY_FINANCE_CARD_CYCLES_PRIVILEGE_V13 / PR #81
(`docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md`) established that a
card purchase's `Transaction.competence` must follow the invoice's canonical
competence -- derived from the account's own persisted `card_closing_day`/
`card_due_day` -- never `booked_at`'s calendar month. That hotfix's own
history is exactly why this module exists: the original implementation lived
as a private helper inside `app/api.py`, and PR #81 found it had previously
been *duplicated* there (a dead, Itaú/Nubank/Mercado-Pago-hardcoded pair
silently shadowed by the real one, since Python keeps only the last
definition of a name).

The P0 Work Order for historical competence repair
(`docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md`, section "Arquitetura
obrigatória" item 1) requires this formula to stop being a private helper
private to `app/api.py` the moment a second consumer -- the historical
detector/repairer -- needs to call the exact same rule: "É proibido duplicar
a fórmula em API, CLI, JavaScript, testes ou outro serviço." This module is
that single canonical implementation. `app/api.py` imports it directly (as
`_card_invoice_competence`/`_resolve_expense_competence`, preserving every
existing call site and the public test surface
`tests/test_card_open_invoice_competence.py` already relies on) instead of
keeping its own copy; `app/services/card_competence_repair.py` (the P0
detector/preview/apply engine) imports it too. Every consumer -- manual
entry, Smart Capture confirmation, installment preview/projection, and the
historical repair detector -- resolves competence through this one function.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from fastapi import HTTPException

from app.models import Account
from app.services.finance import add_months, month_key


def card_invoice_competence(account: Account, booked_at: date) -> str:
    """The canonical "YYYY-MM" competence for a purchase on `account`.

    Non-card accounts use `booked_at`'s own month (no invoice cycle applies).
    A `credit_card` account without a fully configured cycle
    (`card_closing_day`/`card_due_day`) fails closed with a 422 -- no
    competence is ever invented for it (docs/FINANCIAL_RULES.md,
    docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md criterion 5).

    Cutoff is exclusive on the closing day itself: a purchase made exactly on
    `card_closing_day` still belongs to the invoice that closes that day, not
    the next one; a purchase the day after closing rolls to the next invoice.
    The competence follows the invoice's *due* month, not necessarily its
    closing month -- when `card_due_day < card_closing_day` the due date
    falls in the month after closing, so competence is one month past the
    closing month even for a purchase made before closing.
    """

    if account.account_type != "credit_card":
        return booked_at.strftime("%Y-%m")
    closing_day = account.card_closing_day
    due_day = account.card_due_day
    if not closing_day or not due_day:
        raise HTTPException(
            status_code=422,
            detail=(
                f"O cartão {account.name} não tem fechamento/vencimento configurados. "
                "Configure o ciclo antes de lançar a compra."
            ),
        )
    closing_month = booked_at.replace(day=1)
    if booked_at.day > closing_day:
        closing_month = add_months(closing_month, 1)
    due_month = add_months(closing_month, 1 if due_day < closing_day else 0)
    return month_key(due_month)


def resolve_expense_competence(
    *,
    account: Account | None = None,
    account_type: str | None = None,
    competence: str | None,
    booked_at: date,
) -> str:
    """Resolve the competence an expense should be persisted under.

    For a `credit_card` account, delegates to `card_invoice_competence` and,
    when the caller also supplied an explicit `competence`, requires it to
    match the calculated value exactly -- the client never gets to choose an
    arbitrary competence for a card purchase (INV-017). For any other
    account, an explicit `competence` must equal `booked_at`'s own month;
    checking/cash accounts have no invoice cycle to diverge from.
    """

    booked_month = booked_at.strftime("%Y-%m")
    resolved_type = account.account_type if account is not None else account_type

    if resolved_type == "credit_card":
        if account is None:
            if competence is None:
                raise HTTPException(
                    status_code=422,
                    detail="Compra no cartão exige conta com ciclo configurado.",
                )
            return competence

        calculated = card_invoice_competence(account, booked_at)
        if competence is not None and competence != calculated:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A competência informada ({competence}) diverge do ciclo do cartão. "
                    f"Pelo fechamento/vencimento, esta compra pertence à fatura {calculated}."
                ),
            )
        return calculated

    if competence is not None and competence != booked_month:
        raise HTTPException(
            status_code=422,
            detail=(
                "Competência divergente da data do lançamento só é permitida para cartões; "
                "contas correntes usam o mês da própria data."
            ),
        )
    return booked_month


def _clip_day(year: int, month: int, day: int) -> int:
    return min(day, monthrange(year, month)[1])


def card_invoice_window(account: Account, competence: str) -> tuple[date, date, date]:
    """Inverse of `card_invoice_competence`: the `(opens_at, closes_at,
    due_date)` window of the invoice identified by `competence` for
    `account` -- October Go-Live Slice 2
    (`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §3.1).

    Pure function, deterministic from the exact same two persisted fields
    `card_invoice_competence` reads (`card_closing_day`/`card_due_day`), so
    a purchase on any date in `[opens_at, closes_at]` always resolves back
    to this same `competence` through the forward function -- proven by
    `tests/test_card_invoice_lifecycle.py`'s round-trip property test. Day
    clipping mirrors the forward function's own implicit behaviour: a
    closing/due day beyond a short month's length (e.g. 31 in February)
    never advances past that month, exactly like `card_invoice_competence`
    never lets `booked_at.day > closing_day` become true for a short month.

    Raises `HTTPException` (422) for a card without a fully configured
    cycle -- never invents a window for it, matching
    `card_invoice_competence`'s own fail-closed contract.
    """

    if account.account_type != "credit_card":
        raise HTTPException(
            status_code=422,
            detail=f"A conta {account.name} não é um cartão de crédito.",
        )
    closing_day = account.card_closing_day
    due_day = account.card_due_day
    if not closing_day or not due_day:
        raise HTTPException(
            status_code=422,
            detail=(
                f"O cartão {account.name} não tem fechamento/vencimento configurados. "
                "Configure o ciclo antes de consultar a fatura."
            ),
        )

    due_year, due_month_num = (int(part) for part in competence.split("-"))
    due_month = date(due_year, due_month_num, 1)
    closing_month = add_months(due_month, -1) if due_day < closing_day else due_month

    closes_at = date(
        closing_month.year, closing_month.month, _clip_day(closing_month.year, closing_month.month, closing_day)
    )
    due_date = date(due_month.year, due_month.month, _clip_day(due_month.year, due_month.month, due_day))

    previous_closing_month = add_months(closing_month, -1)
    previous_closes_at = date(
        previous_closing_month.year,
        previous_closing_month.month,
        _clip_day(previous_closing_month.year, previous_closing_month.month, closing_day),
    )
    opens_at = previous_closes_at + timedelta(days=1)
    return opens_at, closes_at, due_date


__all__ = ["card_invoice_competence", "card_invoice_window", "resolve_expense_competence"]
