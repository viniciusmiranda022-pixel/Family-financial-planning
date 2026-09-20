"""WA-04 (`docs/WORK_ORDER_WA_04.md`, issue #76) generic read/query layer --
deterministic period-range resolution, filtering and metric computation for
the Tool Layer's free-form analytics tools (`app.services.assistant_tools`'s
`financial_aggregate`/`get_income`/`get_expenses`).

This module adds no new financial arithmetic of its own. Every monetary
total it produces is either (a) plain decimal addition of numbers
`app.services.financial_snapshots.build_snapshot` already computed and
`GET /dashboard`/`GET /reports` already publish for a given month
(`report_month_monetary_publication`, `category_spending_rows`,
`account_cash_flow_rows` -- exactly the rebaseline's "snapshot canônico"),
or (b) a metric (average/count/min/max/share/variation) applied on top of
those already-canonical per-group amounts. There is no second
classification of a raw `Transaction` here -- reusing the existing engine,
never re-deriving what counts as income/expense/transfer/investment, is the
Work Order's explicit financial invariant (`docs/WORK_ORDER_WA_04.md`
"Reuse existing canonical classification ... do not create a second
financial engine").

Every filter is a free-text hint matched case/accent-insensitively against
real household labels (comma-separated for multiple values), the same
"hint, never a guess" idiom `app.services.assistant_actions` already
establishes for `account_hint`/`category_hint` -- never a raw id, table or
column name from the caller.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.services.finance import add_months, money, month_key
from app.services.financial_snapshots import (
    account_cash_flow_rows,
    build_snapshot,
    category_spending_rows,
    expense_detail_rows,
    report_month_monetary_publication,
)

# Grouping dimensions the canonical per-month snapshot already publishes
# rows for. "categoria"/"conta"/"cartao"/"mes" read `category_rows`/
# `account_rows`/`month_expense_rows` below (unchanged since WA-04's first
# round). "titular" (holder) reads `RangeTotals.detail_rows` instead --
# `app.services.financial_snapshots.expense_detail_rows`' per-(category,
# account, titular) canonical contributions (PR #106 review round 1) --
# the same numbers, just not collapsed across holder before being summed
# here, never a second classification of a raw `Transaction`.
DIMENSIONS: tuple[str, ...] = ("categoria", "conta", "cartao", "mes", "titular")

METRICS: tuple[str, ...] = (
    "total",
    "media",
    "contagem",
    "minimo",
    "maximo",
    "participacao",
    "variacao_absoluta",
    "variacao_percentual",
)
VARIATION_METRICS = frozenset({"variacao_absoluta", "variacao_percentual"})

MAX_RANGE_MONTHS = 24
MAX_TOP_N = 20

_LAST_N_MONTHS_RE = re.compile(r"^ultimos?\s+(\d{1,2})\s+meses$")
_LAST_N_DAYS_RE = re.compile(r"^ultimos?\s+(\d{1,3})\s+dias$")

_MONTH_NAMES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

_EXPLICIT_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SLASH_PERIOD_RE = re.compile(r"^(0[1-9]|1[0-2])/(\d{4})$")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _norm(value: str) -> str:
    return " ".join(_strip_accents(value).strip().lower().split())


def matches_hint(hint: str | None, label: str | None) -> bool:
    """True when `hint` (optionally comma-separated for multiple values) is
    found inside `label`, case/accent-insensitive. An empty/`None` hint
    always matches -- callers pass one only when that filter was actually
    requested, never a placeholder for "match everything"."""

    if not hint or not hint.strip():
        return True
    if not label:
        return False
    haystack = _norm(label)
    needles = [part.strip() for part in hint.split(",") if part.strip()]
    if not needles:
        return True
    return any(_norm(needle) in haystack for needle in needles)


def excludes_hint(hint: str | None, label: str | None) -> bool:
    """True when `hint` is present and matches `label` -- the exclusion
    counterpart to `matches_hint`, with the opposite empty-hint default: an
    empty/`None` exclude-hint excludes nothing (WA-05, issue #77 "e se tirar
    mercado?" -- callers pass an exclude-hint only when the caller actually
    asked to drop something; it must never silently drop every row when
    absent, unlike `matches_hint`'s own "empty hint matches everything")."""

    if not hint or not hint.strip():
        return False
    return matches_hint(hint, label)


def resolve_period(text: str | None, *, today: date) -> str | None:
    """Deterministically resolve a free-text period hint into a `"YYYY-MM"`
    snapshot period key, or `None` when it cannot be resolved with
    confidence -- callers must treat `None` as "ask the user", never as
    "assume this month" (an absent/empty hint is the one case that
    legitimately defaults to the current month, the same default
    `GET /reports`'s own `end_month=None` already uses). Supports: empty/
    "hoje"/"este mês", "mês passado"/"mês retrasado", an explicit month
    name with an optional year ("setembro", "setembro de 2026"),
    `"YYYY-MM"`, and `"MM/YYYY"`. Anything else (a range, "últimos N
    meses", an unrecognized word) is intentionally left unresolved rather
    than guessed -- `resolve_period_range` below is the range-aware
    superset of this exact contract.

    Originally WA-02's own `app.services.assistant_tools.resolve_period`
    (moved here, and re-exported there, in WA-04 so this range-aware
    module can reuse it without an import cycle -- see this module's
    docstring)."""

    if text is None or not text.strip():
        return month_key(today.replace(day=1))

    cleaned = _norm(text)

    if cleaned in ("hoje", "este mes", "esse mes", "mes atual", "mes corrente", "atual"):
        return month_key(today.replace(day=1))
    if cleaned in ("mes passado", "mes anterior", "ultimo mes"):
        return month_key(add_months(today.replace(day=1), -1))
    if cleaned == "mes retrasado":
        return month_key(add_months(today.replace(day=1), -2))

    compact = cleaned.replace(" ", "")
    match = _EXPLICIT_PERIOD_RE.match(compact)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = _SLASH_PERIOD_RE.match(compact)
    if match:
        return f"{match.group(2)}-{match.group(1)}"

    tokens = [token for token in re.split(r"[\s/]+", cleaned) if token and token != "de"]
    if tokens and tokens[0] in _MONTH_NAMES:
        month = _MONTH_NAMES[tokens[0]]
        year = today.year
        if len(tokens) >= 2 and tokens[1].isdigit() and len(tokens[1]) == 4:
            year = int(tokens[1])
        elif month > today.month:
            # A bare month name later than the current month means "last
            # year's occurrence" ("em outubro" said in September means
            # October last year, never a future month the household has no
            # data for yet) -- never the current year's not-yet-happened month.
            year -= 1
        return f"{year:04d}-{month:02d}"

    return None


def resolve_period_range(text: str | None, *, today: date) -> tuple[str, str] | None:
    """Deterministically resolve a free-text period hint into an inclusive
    `(start_period, end_period)` `"YYYY-MM"` range, oldest first, or `None`
    when it cannot be resolved with confidence -- callers must treat `None`
    as "ask the user", never as "assume a range".

    Delegates every single-month form (empty/"hoje"/"este mês", "mês
    passado"/"mês retrasado", an explicit month name, `"YYYY-MM"`,
    `"MM/YYYY"`) to `app.services.assistant_tools.resolve_period` -- this
    function is strictly additive, it never resolves a text that function
    already resolves differently. It adds true ranges: "últimos N meses"
    (1-24), "últimos N dias" (1-365, converted to whole months via
    `ceil(dias / 30)` -- the same days-to-months idiom
    `app.services.assistant_tools._tool_project_horizon` already uses for
    its own 30/60/90-day horizon), "este ano", "ano passado" (the full
    previous calendar year) and "último ano"/"últimos 12 meses".
    """

    single = resolve_period(text, today=today)
    if single is not None:
        return (single, single)
    if text is None or not text.strip():
        return None

    cleaned = _norm(text)
    this_month = today.replace(day=1)

    if cleaned in ("este ano", "esse ano", "ano atual", "ano corrente"):
        return (month_key(date(today.year, 1, 1)), month_key(this_month))
    if cleaned == "ano passado":
        return (month_key(date(today.year - 1, 1, 1)), month_key(date(today.year - 1, 12, 1)))
    if cleaned in ("ultimo ano", "ultimos 12 meses", "ultimos doze meses"):
        return (month_key(add_months(this_month, -11)), month_key(this_month))

    match = _LAST_N_MONTHS_RE.match(cleaned)
    if match:
        n = int(match.group(1))
        if not (1 <= n <= MAX_RANGE_MONTHS):
            return None
        return (month_key(add_months(this_month, -(n - 1))), month_key(this_month))

    match = _LAST_N_DAYS_RE.match(cleaned)
    if match:
        days = int(match.group(1))
        if not (1 <= days <= 365):
            return None
        months = -(-days // 30)  # ceil(days / 30)
        return (month_key(add_months(this_month, -(months - 1))), month_key(this_month))

    return None


def period_range_months(start_period: str, end_period: str) -> list[str]:
    """Ordered `"YYYY-MM"` keys from `start_period` to `end_period`
    inclusive."""

    start = datetime.strptime(start_period, "%Y-%m").date()
    end = datetime.strptime(end_period, "%Y-%m").date()
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(month_key(cursor))
        cursor = add_months(cursor, 1)
    return months


def previous_equal_length_range(start_period: str, end_period: str) -> tuple[str, str]:
    """The immediately preceding range of the same length in months --
    the deterministic convention this module uses for "growth"/variation
    metrics when the caller does not name an explicit `compare_period_text`
    (Work Order mandatory test "top 5 categories by growth over 90 days"
    names no explicit comparison period). This is a structural default,
    never a fabricated financial fact: it is always this exact range
    immediately before the requested one, computed the same way every
    time."""

    months = period_range_months(start_period, end_period)
    span = len(months)
    start = datetime.strptime(start_period, "%Y-%m").date()
    previous_end = add_months(start, -1)
    previous_start = add_months(previous_end, -(span - 1))
    return (month_key(previous_start), month_key(previous_end))


@dataclass(frozen=True, slots=True)
class RangeTotals:
    start_period: str
    end_period: str
    income: Decimal
    expenses: Decimal
    category_rows: dict[str, Decimal]
    account_rows: dict[str, dict[str, Any]]
    month_income_rows: dict[str, Decimal]
    month_expense_rows: dict[str, Decimal]
    # WA-04 review round 1 (PR #106): the range's per-(category, account,
    # titular) canonical expense contributions, concatenated verbatim from
    # each month's `expense_detail_rows` (see `collect_range_totals`) --
    # `holder_rows`/`filtered_expense_total` below group/filter this list on
    # more than one dimension at once instead of picking a single
    # `label_hint` the way `dimension_rows` does for the four dimensions
    # above.
    detail_rows: tuple[dict[str, Any], ...]


def collect_range_totals(
    db: Session, *, household_id: str, generated_by: str, start_period: str, end_period: str
) -> RangeTotals:
    """Sum already-canonical, per-month snapshot outputs across
    `[start_period, end_period]`.

    Each individual month's figures are exactly what `GET /dashboard`/
    `GET /reports` already publish for that month
    (`report_month_monetary_publication`, `category_spending_rows`,
    `account_cash_flow_rows`); this function only adds them across months
    -- plain decimal addition, never a second classification of a raw
    transaction."""

    income = Decimal("0")
    expenses = Decimal("0")
    category_rows: dict[str, Decimal] = {}
    account_rows: dict[str, dict[str, Any]] = {}
    month_income_rows: dict[str, Decimal] = {}
    month_expense_rows: dict[str, Decimal] = {}
    detail_rows: list[dict[str, Any]] = []

    for period in period_range_months(start_period, end_period):
        snapshot = build_snapshot(db, household_id=household_id, period=period, generated_by=generated_by)
        published = report_month_monetary_publication(snapshot)
        month_income = money(Decimal(str(published["cash_in"])))
        month_expense = money(Decimal(str(published["spending"])))
        income += month_income
        expenses += month_expense
        month_income_rows[period] = month_income
        month_expense_rows[period] = month_expense
        for row in category_spending_rows(snapshot):
            label = str(row.get("category") or "Sem categoria")
            category_rows[label] = category_rows.get(label, Decimal("0")) + money(
                Decimal(str(row.get("amount", 0)))
            )
        for row in account_cash_flow_rows(snapshot):
            label = str(row.get("account") or "Sem conta identificada")
            bucket = account_rows.setdefault(
                label, {"cash_out": Decimal("0"), "account_type": row.get("account_type") or "other"}
            )
            bucket["cash_out"] = bucket["cash_out"] + money(Decimal(str(row.get("cash_out", 0))))
        for row in expense_detail_rows(snapshot):
            detail_rows.append({**row, "period": period})

    return RangeTotals(
        start_period=start_period,
        end_period=end_period,
        income=money(income),
        expenses=money(expenses),
        category_rows=category_rows,
        account_rows=account_rows,
        month_income_rows=month_income_rows,
        month_expense_rows=month_expense_rows,
        detail_rows=tuple(detail_rows),
    )


def dimension_rows(
    totals: RangeTotals,
    dimension: str,
    *,
    label_hint: str | None = None,
    exclude_label_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Per-group `{"label", "amount"}` rows for `dimension`, already
    summed across `totals`'s whole range by `collect_range_totals`, then
    narrowed by `label_hint` (a free-text filter, never a guess -- see
    `matches_hint`) and, symmetrically, with any row matching
    `exclude_label_hint` dropped (WA-05, issue #77 "e se tirar mercado?" --
    both hints compose: `label_hint` narrows, `exclude_label_hint` removes,
    neither overrides the other)."""

    if dimension == "mes":
        rows = [{"label": key, "amount": value} for key, value in totals.month_expense_rows.items()]
    elif dimension == "categoria":
        rows = [{"label": key, "amount": value} for key, value in totals.category_rows.items()]
    else:
        wants_card = dimension == "cartao"
        rows = [
            {"label": label, "amount": bucket["cash_out"]}
            for label, bucket in totals.account_rows.items()
            if (bucket.get("account_type") == "credit_card") == wants_card
        ]
    return [
        row
        for row in rows
        if matches_hint(label_hint, row["label"]) and not excludes_hint(exclude_label_hint, row["label"])
    ]


def _detail_matches(
    row: dict[str, Any],
    *,
    category_hint: str | None,
    account_hint: str | None,
    holder_hint: str | None,
    exclude_category_hint: str | None = None,
) -> bool:
    """True when `row` (one of `RangeTotals.detail_rows`) satisfies every
    provided hint at once (AND, never "last one wins") -- a hint left
    absent/`None` matches everything, same contract as `matches_hint`.
    `exclude_category_hint` is the one exception: it *removes* a row whose
    category matches it, symmetric to `category_hint` narrowing one in (WA-05,
    issue #77 "e se tirar mercado?" -- composes with every other filter here,
    never a second classification of a raw transaction)."""

    return (
        matches_hint(category_hint, row.get("category"))
        and matches_hint(account_hint, row.get("account"))
        and matches_hint(holder_hint, row.get("holder"))
        and not excludes_hint(exclude_category_hint, row.get("category"))
    )


def filtered_expense_total(
    totals: RangeTotals,
    *,
    category_hint: str | None = None,
    account_hint: str | None = None,
    holder_hint: str | None = None,
    exclude_category_hint: str | None = None,
) -> Decimal:
    """Operating-expense total narrowed by every provided hint at once.

    Reads `totals.detail_rows` -- the exact per-transaction category/
    account/holder contributions `RangeTotals.expenses`/`category_rows` are
    already built from (see `app.services.financial_snapshots.
    expense_detail_rows`) -- instead of picking a single dimension's
    pre-aggregated rows the way `dimension_rows` does; this is what lets a
    caller combine `category_hint` and `account_hint` (WA-04 PR #106 review
    round 1: "gasto de combustível no Nubank", not just one filter at a
    time, with `account_hint`/`category_hint` silently overriding each
    other). With no hint at all this returns `totals.expenses` itself
    (every detail row, unfiltered)."""

    matched = [
        row
        for row in totals.detail_rows
        if _detail_matches(
            row,
            category_hint=category_hint,
            account_hint=account_hint,
            holder_hint=holder_hint,
            exclude_category_hint=exclude_category_hint,
        )
    ]
    return money(sum((Decimal(str(row.get("amount", 0))) for row in matched), Decimal("0")))


def holder_rows(
    totals: RangeTotals,
    *,
    category_hint: str | None = None,
    account_hint: str | None = None,
    holder_hint: str | None = None,
    exclude_category_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Per-holder `{"label", "amount"}` rows for `financial_aggregate`'s
    `titular` dimension -- grouped from `totals.detail_rows`, narrowed by
    every provided hint at once first (see `filtered_expense_total`), so
    "quanto a Kelly gastou em Mercado" (a category filter combined with the
    holder dimension) is one query instead of two independently-filtered
    ones."""

    grouped: dict[str, Decimal] = {}
    for row in totals.detail_rows:
        if not _detail_matches(
            row,
            category_hint=category_hint,
            account_hint=account_hint,
            holder_hint=holder_hint,
            exclude_category_hint=exclude_category_hint,
        ):
            continue
        label = str(row.get("holder") or "Não informado")
        grouped[label] = grouped.get(label, Decimal("0")) + money(Decimal(str(row.get("amount", 0))))
    return [{"label": label, "amount": amount} for label, amount in grouped.items()]


def apply_metric(rows: Sequence[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Reshape already-grouped `rows` (`{"label", "amount"}`) for `metric`.

    `total` (and any grouped listing) is returned unchanged; `participacao`
    adds a `share` field (`amount / grand_total`, `0` -- never a
    `ZeroDivisionError` -- when the grand total is not positive);
    `media`/`contagem`/`minimo`/`maximo` collapse the whole set into one
    summary row, since those metrics describe the group as a whole, not
    each member of it."""

    if metric == "participacao":
        grand_total = sum((row["amount"] for row in rows), Decimal("0"))
        return [
            {**row, "share": (row["amount"] / grand_total) if grand_total > 0 else Decimal("0")}
            for row in rows
        ]
    if metric == "contagem":
        return [{"label": "total", "amount": Decimal(len(rows))}]
    if metric in ("media", "minimo", "maximo"):
        amounts = [row["amount"] for row in rows]
        if not amounts:
            return [{"label": "total", "amount": Decimal("0")}]
        if metric == "media":
            value = money(sum(amounts, Decimal("0")) / Decimal(len(amounts)))
        elif metric == "minimo":
            value = min(amounts)
        else:
            value = max(amounts)
        return [{"label": "total", "amount": value}]
    return list(rows)


def variation_rows(
    current_rows: Sequence[dict[str, Any]], previous_rows: Sequence[dict[str, Any]], *, percentage: bool
) -> list[dict[str, Any]]:
    """Per-label variation between two already-grouped row sets. A label
    absent from `previous_rows` is treated as a previous amount of `0`
    (a genuinely new category/account, not a data gap). For
    `percentage=True`, a `previous_amount` of `0` yields `variation_percent
    = None` (undefined growth from zero) -- never fabricated as `0%` or an
    infinite value."""

    previous_by_label = {row["label"]: row["amount"] for row in previous_rows}
    result: list[dict[str, Any]] = []
    for row in current_rows:
        previous_amount = previous_by_label.get(row["label"], Decimal("0"))
        absolute = money(row["amount"] - previous_amount)
        if percentage:
            variation = None if previous_amount == 0 else money((absolute / previous_amount) * Decimal("100"))
            result.append(
                {
                    "label": row["label"],
                    "amount": row["amount"],
                    "previous_amount": previous_amount,
                    "variation_percent": variation,
                }
            )
        else:
            result.append(
                {
                    "label": row["label"],
                    "amount": row["amount"],
                    "previous_amount": previous_amount,
                    "variation_absolute": absolute,
                }
            )
    return result


def apply_top_n(rows: Sequence[dict[str, Any]], top_n_raw: str | None, *, sort_key: str) -> list[dict[str, Any]]:
    """Sort `rows` by `sort_key` descending and keep the top `top_n_raw`
    (clamped to `[1, MAX_TOP_N]`) -- an already-sorted, already-truncated
    structured result, so the caller (the LLM) never has to sort or pick a
    "top N" out of raw numbers itself. `top_n_raw` absent/unparsable
    returns `rows` unchanged, in their original order."""

    if not top_n_raw:
        return list(rows)
    try:
        n = int(top_n_raw)
    except (TypeError, ValueError):
        return list(rows)
    n = max(1, min(n, MAX_TOP_N))
    ranked = [row for row in rows if row.get(sort_key) is not None]
    ranked.sort(key=lambda row: row[sort_key], reverse=True)
    return ranked[:n]
