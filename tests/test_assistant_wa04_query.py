"""Tests for WA-04 (`docs/WORK_ORDER_WA_04.md`, issue #76) -- the generic
read/query layer (`financial_aggregate`, `get_income`, `get_expenses`,
`get_commitments`, `get_installments`, `search_transactions`) and its
deterministic period-range resolver (`resolve_period_range`).

Mirrors the structure/fixtures of `tests/test_assistant_orchestrator.py`
(WA-02): a seeded in-memory SQLite session, numeric parity against the
canonical snapshot engine, household isolation, and fail-closed/never-a-
guess clarification behaviour. Reuses that file's `FakePlanClient`/
`_valid_plan_response`/`_session_factory`/`_admin_user` fixtures instead of
duplicating them.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.models import (
    Account,
    Category,
    FinancialProfile,
    Household,
    Obligation,
    Transaction,
    User,
)
from app.services.assistant_orchestrator import plan_and_execute
from app.services.assistant_tools import run_tool
from app.services.financial_query import resolve_period_range
from app.services.financial_snapshots import build_snapshot, report_month_monetary_publication
from tests.test_assistant_orchestrator import (
    FakePlanClient,
    _admin_user,
    _session_factory,
    _valid_plan_response,
)

TODAY = date(2026, 9, 17)


# ---------------------------------------------------------------------------
# 1. resolve_period_range -- deterministic, never a guess.
# ---------------------------------------------------------------------------


def test_resolve_period_range_delegates_single_month_forms_to_resolve_period() -> None:
    assert resolve_period_range("setembro", today=TODAY) == ("2026-09", "2026-09")
    assert resolve_period_range(None, today=TODAY) == ("2026-09", "2026-09")
    assert resolve_period_range("mês passado", today=TODAY) == ("2026-08", "2026-08")


def test_resolve_period_range_last_n_months() -> None:
    assert resolve_period_range("últimos 3 meses", today=TODAY) == ("2026-07", "2026-09")
    assert resolve_period_range("ultimos 2 meses", today=TODAY) == ("2026-08", "2026-09")


def test_resolve_period_range_last_n_days_rounds_up_to_whole_months() -> None:
    # ceil(90 / 30) = 3 months, ending at the current month -- the same
    # days-to-months idiom `_tool_project_horizon` already uses.
    assert resolve_period_range("últimos 90 dias", today=TODAY) == ("2026-07", "2026-09")
    assert resolve_period_range("últimos 45 dias", today=TODAY) == ("2026-08", "2026-09")


def test_resolve_period_range_calendar_year_forms() -> None:
    assert resolve_period_range("este ano", today=TODAY) == ("2026-01", "2026-09")
    assert resolve_period_range("ano passado", today=TODAY) == ("2025-01", "2025-12")
    assert resolve_period_range("último ano", today=TODAY) == ("2025-10", "2026-09")
    assert resolve_period_range("últimos 12 meses", today=TODAY) == ("2025-10", "2026-09")


def test_resolve_period_range_unresolvable_or_out_of_bounds_returns_none() -> None:
    assert resolve_period_range("qualquer coisa aleatória", today=TODAY) is None
    assert resolve_period_range("últimos 25 meses", today=TODAY) is None
    assert resolve_period_range("últimos 400 dias", today=TODAY) is None
    assert resolve_period_range("", today=TODAY) == ("2026-09", "2026-09")


# ---------------------------------------------------------------------------
# 2. Seeded fixtures.
# ---------------------------------------------------------------------------


def _fingerprint() -> str:
    return uuid.uuid4().hex.ljust(64, "0")[:64]


def _seed_household(session_factory, *, username: str) -> dict:
    with session_factory() as db:
        household = Household(name=f"Família {username}")
        db.add(household)
        db.flush()
        household_id = household.id
        db.add(FinancialProfile(household_id=household_id, monthly_cash_cap=Decimal("5000")))
        checking = Account(household_id=household_id, name="Conta Corrente", account_type="checking")
        card = Account(household_id=household_id, name="Cartão Nubank", account_type="credit_card")
        db.add(checking)
        db.add(card)
        market = Category(household_id=household_id, name="Mercado")
        fuel = Category(household_id=household_id, name="Combustível")
        income_category = Category(household_id=household_id, name="Receitas")
        patrimonial = Category(household_id=household_id, name="Transferência patrimonial")
        internal_transfer = Category(household_id=household_id, name="Transferência interna")
        for category in (market, fuel, income_category, patrimonial, internal_transfer):
            db.add(category)
        db.add(
            User(
                household_id=household_id,
                name="Admin",
                username=username,
                password_hash="x",
                is_admin=True,
            )
        )
        db.flush()
        ids = {
            "household_id": household_id,
            "checking_id": checking.id,
            "card_id": card.id,
            "market_id": market.id,
            "fuel_id": fuel.id,
            "income_id": income_category.id,
            "patrimonial_id": patrimonial.id,
            "internal_transfer_id": internal_transfer.id,
        }
        db.commit()
    return ids


def _seed_transaction(
    session_factory,
    *,
    household_id: str,
    account_id: str,
    category_id: str | None,
    booked_at: date,
    amount: Decimal,
    transaction_type: str,
    description: str = "Lançamento de teste",
    owner_label: str = "Família",
    installment_current: int | None = None,
    installment_total: int | None = None,
    competence: str | None = None,
) -> str:
    with session_factory() as db:
        transaction = Transaction(
            household_id=household_id,
            account_id=account_id,
            category_id=category_id,
            booked_at=booked_at,
            occurred_at=booked_at,
            competence=competence or f"{booked_at.year:04d}-{booked_at.month:02d}",
            description=description,
            normalized_description=description.upper(),
            amount=amount,
            transaction_type=transaction_type,
            owner_label=owner_label,
            fingerprint=_fingerprint(),
            source_priority=50,
            confidence=Decimal("1"),
            reviewed=True,
            installment_current=installment_current,
            installment_total=installment_total,
        )
        db.add(transaction)
        db.commit()
        return transaction.id


# ---------------------------------------------------------------------------
# 3. get_expenses / get_income -- range parity, filters, exclusion semantics.
# ---------------------------------------------------------------------------


def test_get_expenses_sums_canonical_per_month_totals_across_a_range() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-range")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 8, 5),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "últimos 2 meses"},
            message="quanto gastei nos últimos 2 meses?",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.facts["start_period"] == "2026-08"
    assert outcome.facts["end_period"] == "2026-09"
    assert outcome.facts["expenses"] == Decimal("380.00")

    # Parity: identical to manually summing the canonical per-month snapshot
    # publication -- never a second, independently-computed total.
    with session_factory() as db:
        total = Decimal("0")
        for period in ("2026-08", "2026-09"):
            snapshot = build_snapshot(db, household_id=ids["household_id"], period=period)
            total += Decimal(str(report_month_monetary_publication(snapshot)["spending"]))
    assert outcome.facts["expenses"] == total


def test_get_expenses_category_filter_narrows_total_to_that_category() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-category")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        description="Supermercado Extra",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
        description="Posto Shell",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "category_hint": "combustível"},
            message="quanto gastei de combustível esse mês?",
            today=TODAY,
        )
    assert outcome.facts["expenses"] == Decimal("200.00")


def test_get_expenses_combines_category_and_account_hint_simultaneously() -> None:
    """PR #106 review round 1 -- MERGE BLOCKED item 2: `category_hint` and
    `account_hint` used to silently override each other (category winning),
    so this exact scenario returned the whole "Mercado" category total
    (R$350) instead of only the purchase that matches *both* filters at
    once (R$300). `filtered_expense_total` fixes this by ANDing every
    provided hint against `RangeTotals.detail_rows` instead of picking one
    dimension's pre-aggregated rows."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-combined")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        description="Supermercado na Conta Corrente",
    )
    # Same category, different account -- must NOT count once account_hint
    # narrows to "Conta Corrente".
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-50.00"),
        transaction_type="expense",
        description="Supermercado no Cartão",
    )
    # Same account, different category -- must NOT count once category_hint
    # narrows to "Mercado".
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-80.00"),
        transaction_type="expense",
        description="Combustível na Conta Corrente",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={
                "period_text": "este mês",
                "category_hint": "mercado",
                "account_hint": "conta corrente",
            },
            message="quanto gastei de mercado na conta corrente esse mês?",
            today=TODAY,
        )
    assert outcome.facts["expenses"] == Decimal("300.00")


def test_get_expenses_holder_hint_combines_with_category_hint() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-holder")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-120.00"),
        transaction_type="expense",
        owner_label="Kelly",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "category_hint": "mercado", "holder_hint": "Vinicius"},
            message="quanto o Vinicius gastou de mercado esse mês?",
            today=TODAY,
        )
    assert outcome.facts["expenses"] == Decimal("300.00")


def test_get_income_excludes_internal_transfer_and_investment() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="income-excl")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["income_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("5000.00"),
        transaction_type="income",
        description="Salário",
    )
    # Internal transfer and a Privilège application -- neither is income,
    # regardless of being a positive-amount cash movement (rebaseline §14).
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["internal_transfer_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("1000.00"),
        transaction_type="transfer",
        description="Transferência entre contas",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["patrimonial_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-2000.00"),
        transaction_type="transfer",
        description="Aplicação Privilège",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_income",
            arguments={"period_text": "este mês"},
            message="qual foi minha renda esse mês?",
            today=TODAY,
        )
    assert outcome.facts["income"] == Decimal("5000.00")


def test_get_income_unresolvable_period_asks_never_guesses() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="income-ask")
    user = _admin_user(session_factory, household_id=ids["household_id"])
    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_income",
            arguments={"period_text": "sei lá quando"},
            message="qual foi minha renda?",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.clarifying_question is not None
    assert outcome.facts == {}


# ---------------------------------------------------------------------------
# 4. financial_aggregate -- grouping, metrics, top_n, zero-denominator.
# ---------------------------------------------------------------------------


def test_financial_aggregate_participacao_shares_sum_to_the_whole() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-share")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-100.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "categoria", "metric": "participacao", "period_text": "este mês"},
            message="qual a participação de cada categoria esse mês?",
            today=TODAY,
        )
    rows = outcome.facts["rows"]
    total_share = sum((row["share"] for row in rows), Decimal("0"))
    assert total_share == Decimal("1")
    market_row = next(row for row in rows if row["label"] == "Mercado")
    assert market_row["share"] == Decimal("0.75")


def test_financial_aggregate_variacao_percentual_zero_denominator_is_none_never_fabricated() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-variation")
    # "Mercado" exists in both months (growth is computable); "Combustível"
    # only exists this month (previous amount is zero -- undefined growth).
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 8, 3),
        amount=Decimal("-100.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={
                "dimension": "categoria",
                "metric": "variacao_percentual",
                "period_text": "este mês",
            },
            message="quais categorias mais cresceram?",
            today=TODAY,
        )
    rows = {row["label"]: row for row in outcome.facts["rows"]}
    assert rows["Mercado"]["variation_percent"] == Decimal("50.00")
    assert rows["Combustível"]["variation_percent"] is None
    # Compared against the immediately preceding equal-length range by
    # default -- never a fabricated/guessed comparison period.
    assert outcome.facts["compare_start_period"] == "2026-08"
    assert outcome.facts["compare_end_period"] == "2026-08"


def test_financial_aggregate_top_n_sorts_and_truncates_deterministically() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-topn")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-50.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={
                "dimension": "categoria",
                "metric": "total",
                "period_text": "este mês",
                "top_n": "1",
            },
            message="qual foi minha maior categoria de gasto?",
            today=TODAY,
        )
    rows = outcome.facts["rows"]
    assert len(rows) == 1
    assert rows[0]["label"] == "Combustível"


def test_financial_aggregate_missing_dimension_asks_never_guesses() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-ask")
    user = _admin_user(session_factory, household_id=ids["household_id"])
    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"period_text": "este mês"},
            message="me mostra os gastos",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.clarifying_question is not None
    assert outcome.facts == {}


def test_financial_aggregate_titular_dimension_groups_by_holder() -> None:
    """PR #106 review round 1 -- MERGE BLOCKED item 1: `titular` (holder)
    was documented as out of scope; it is now a real grouping dimension,
    reading `RangeTotals.detail_rows` (`app.services.financial_snapshots.
    expense_detail_rows`) instead of a second reclassification of
    `Transaction`."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-titular")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-100.00"),
        transaction_type="expense",
        owner_label="Kelly",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "titular", "period_text": "este mês"},
            message="quanto cada um gastou esse mês?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"Vinicius": Decimal("300.00"), "Kelly": Decimal("100.00")}
    # Parity: the grouped total must reproduce the same canonical total
    # `get_expenses`/`report_month_monetary_publication` report.
    with session_factory() as db:
        expenses = run_tool(
            db, user=user, tool="get_expenses", arguments={"period_text": "este mês"}, message="x", today=TODAY
        )
    assert sum(rows.values(), Decimal("0")) == expenses.facts["expenses"]


def test_financial_aggregate_titular_dimension_combines_with_category_hint() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-titular-category")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    # Same holder, different category -- must not inflate the "Mercado"-only
    # breakdown below.
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-100.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-50.00"),
        transaction_type="expense",
        owner_label="Kelly",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "titular", "period_text": "este mês", "category_hint": "mercado"},
            message="quanto cada um gastou de mercado esse mês?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"Vinicius": Decimal("300.00"), "Kelly": Decimal("50.00")}


# ---------------------------------------------------------------------------
# 4b. WA-05 (docs/WORK_ORDER_WA_05.md, issue #77) "e se tirar mercado?" --
#     exclude_category_hint on get_expenses/financial_aggregate: symmetric
#     to category_hint, but removes a matching row/category from the total
#     instead of narrowing to it. Composes with every other filter (AND),
#     never overrides them.
# ---------------------------------------------------------------------------


def test_get_expenses_exclude_category_hint_drops_that_category_from_the_total() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        description="Supermercado Extra",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
        description="Posto Shell",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        full = run_tool(
            db, user=user, tool="get_expenses", arguments={"period_text": "este mês"}, message="x", today=TODAY
        )
    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "exclude_category_hint": "mercado"},
            message="e se tirar mercado?",
            today=TODAY,
        )
    assert outcome.facts["expenses"] == Decimal("200.00")
    assert outcome.facts["expenses"] == full.facts["expenses"] - Decimal("300.00")
    assert all(row["label"] != "Mercado" for row in outcome.facts["by_category"])


def test_get_expenses_exclude_category_hint_composes_with_account_hint() -> None:
    """`exclude_category_hint` and `account_hint` combine (AND), neither
    silently overriding the other -- same discipline PR #106 already
    established for `category_hint`/`account_hint`."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="expenses-exclude-account")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    # Different account -- must not count once account_hint narrows to
    # "Conta Corrente", regardless of exclude_category_hint.
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-999.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={
                "period_text": "este mês",
                "account_hint": "conta corrente",
                "exclude_category_hint": "mercado",
            },
            message="e se tirar mercado da conta corrente?",
            today=TODAY,
        )
    assert outcome.facts["expenses"] == Decimal("80.00")


def test_financial_aggregate_exclude_category_hint_drops_row_and_reports_grand_total() -> None:
    """dimension=categoria, no category_hint, exclude_category_hint=Mercado
    -- the excluded category never resurfaces in `rows`, and metric=total's
    `total` fact is the grand sum of every remaining row (so a caller asking
    a single "e se tirar mercado, quanto sobra?" question never has to sum
    `rows` itself -- INV-021, no arithmetic outside the deterministic
    engine)."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "categoria", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="e se tirar mercado?",
            today=TODAY,
        )
    labels = {row["label"] for row in outcome.facts["rows"]}
    assert "Mercado" not in labels
    assert outcome.facts["total"] == Decimal("200.00")

    # Parity: the same grand total `get_expenses` reports with the identical
    # exclusion filter -- never a second, independently-computed total.
    with session_factory() as db:
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    assert outcome.facts["total"] == expenses.facts["expenses"]


def test_financial_aggregate_total_metric_always_reports_the_grand_total_fact() -> None:
    """Regression: metric=total's `total` fact is always the plain decimal
    sum of every matching (pre-top_n) row, independent of any exclude
    filter -- covers the ordinary case with no exclusion at all."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-total-fact")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "categoria", "period_text": "este mês"},
            message="quanto gastei por categoria?",
            today=TODAY,
        )
    assert outcome.facts["total"] == Decimal("500.00")
    assert outcome.facts["total"] == sum((row["amount"] for row in outcome.facts["rows"]), Decimal("0"))


def test_financial_aggregate_titular_dimension_combines_with_exclude_category_hint() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-titular-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-100.00"),
        transaction_type="expense",
        owner_label="Vinicius",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "titular", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="quanto o Vinicius gastou esse mês, tirando mercado?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"Vinicius": Decimal("100.00")}


def test_financial_aggregate_dimension_conta_exclude_category_hint_subtracts_only_that_category() -> None:
    """PR #107 review round 2: for `dimension != "categoria"`, `_rows_for`
    used to forward `exclude_category_hint` straight into `dimension_rows`'s
    `label_hint`-shaped exclude, which compares against the ROW's own label
    (an account name for `dimension="conta"`) instead of each transaction's
    category -- so excluding "mercado" never actually dropped Mercado
    spending from an account's total unless the account itself happened to
    be named "mercado". This seeds one checking account with both Mercado
    and Combustível spend and asserts only the Mercado portion is
    subtracted, the rest of that same account's total survives untouched."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-conta-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    # Card spend must never leak into `dimension="conta"` rows, excluded or not.
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-999.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "conta", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="quanto gastei por conta, tirando mercado?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"Conta Corrente": Decimal("80.00")}
    assert outcome.facts["total"] == Decimal("80.00")


def test_financial_aggregate_dimension_cartao_exclude_category_hint_composes_with_account_hint() -> None:
    """Same bug class as the `conta` test above, on `dimension="cartao"`,
    plus composition with `account_hint` (label_hint) narrowing to a single
    card -- neither filter overrides the other."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-cartao-exclude")
    with session_factory() as db:
        second_card = Account(household_id=ids["household_id"], name="Cartão Itaú", account_type="credit_card")
        db.add(second_card)
        db.commit()
        second_card_id = second_card.id
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=second_card_id,
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-500.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={
                "dimension": "cartao",
                "period_text": "este mês",
                "account_hint": "nubank",
                "exclude_category_hint": "mercado",
            },
            message="quanto gastei no Nubank, tirando mercado?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"Cartão Nubank": Decimal("200.00")}


def test_financial_aggregate_dimension_mes_exclude_category_hint_subtracts_per_month() -> None:
    """`dimension="mes"` groups by period, not by category -- excluding
    Mercado must subtract only that month's Mercado contribution from each
    month's row, never the whole row nor another month's figure."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-mes-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 8, 10),
        amount=Decimal("-100.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 8, 11),
        amount=Decimal("-40.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "mes", "period_text": "ultimos 2 meses", "exclude_category_hint": "mercado"},
            message="quanto gastei por mês, tirando mercado?",
            today=TODAY,
        )
    rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
    assert rows == {"2026-08": Decimal("40.00"), "2026-09": Decimal("80.00")}


def test_financial_aggregate_conta_plus_cartao_exclude_category_hint_parity_with_get_expenses() -> None:
    """Parity requirement (PR #107 review round 2): summing `dimension="conta"`
    and `dimension="cartao"` rows under the same `exclude_category_hint`
    must equal `get_expenses`'s own `expenses` total under the identical
    filter -- both read the exact same `totals.detail_rows`, never a second,
    independently-computed number."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="aggregate-parity-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 6),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        conta = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "conta", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        cartao = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "cartao", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    combined = conta.facts["total"] + cartao.facts["total"]
    assert combined == Decimal("280.00")
    assert combined == expenses.facts["expenses"]


def _seed_card_invoice_payment(
    session_factory,
    ids: dict,
    *,
    booked_at: date,
    amount: Decimal,
) -> str:
    """Seeds the two reconciliation legs `pay_card_invoice`/`link_card_payment`
    always create for a real invoice payment (INV-002, same convention as
    `tests/test_financial_snapshots.py`): a card-side "payment received" leg
    (+`amount`) and the checking-side cash debit (-`amount`), both under a
    `Conciliação` category and `transaction_type="reconciliation"`. Only the
    checking leg counts once, on `card_payments`
    (`app/services/financial_snapshots.py::_collect`) -- never a second
    `expense_detail`/`category_spending` contribution."""

    with session_factory() as db:
        reconciliation = Category(household_id=ids["household_id"], name="Conciliação")
        db.add(reconciliation)
        db.commit()
        reconciliation_id = reconciliation.id
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=reconciliation_id,
        booked_at=booked_at,
        amount=amount,
        transaction_type="reconciliation",
        description="Pagamento de fatura recebido",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=reconciliation_id,
        booked_at=booked_at,
        amount=-amount,
        transaction_type="reconciliation",
        description="Pagamento de fatura",
    )
    return reconciliation_id


def test_financial_aggregate_dimension_conta_never_includes_card_invoice_payment() -> None:
    """PR #107 review round 3: a card purchase followed by paying that
    invoice from the checking account must never duplicate the gasto --
    `dimension="conta"` must report only the checking account's OWN
    operating expense (here, the R$80 fuel purchase), never the R$150
    invoice-payment cash debit that also left that same account. Before this
    fix, `account_rows` was built from `account_cash_flow_rows`'s `cash_out`
    (a physical-bank-outflow figure that deliberately includes the invoice
    payment debit for "quanto saiu desta conta?" -- rebaseline §16/§17),
    so `dimension="conta"` silently reported R$230 for an account with only
    R$80 of its own spending."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="conta-invoice-payment")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    _seed_card_invoice_payment(session_factory, ids, booked_at=date(2026, 9, 10), amount=Decimal("150.00"))
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        conta = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "conta", "period_text": "este mês"},
            message="quanto gastei por conta?",
            today=TODAY,
        )
    with session_factory() as db:
        cartao = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "cartao", "period_text": "este mês"},
            message="quanto gastei no cartão?",
            today=TODAY,
        )
    assert {row["label"]: row["amount"] for row in conta.facts["rows"]} == {"Conta Corrente": Decimal("80.00")}
    assert conta.facts["total"] == Decimal("80.00")
    assert {row["label"]: row["amount"] for row in cartao.facts["rows"]} == {"Cartão Nubank": Decimal("150.00")}


def test_financial_aggregate_conta_plus_cartao_parity_with_get_expenses_when_invoice_paid() -> None:
    """PR #107 review round 3, regression (3): summing `dimension="conta"`
    and `dimension="cartao"` must equal `get_expenses`'s own total even in
    the presence of a paid card invoice -- the invoice payment must not
    inflate either side, so the two never sum to more than the household's
    real operating expense."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="conta-cartao-parity-invoice")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    _seed_card_invoice_payment(session_factory, ids, booked_at=date(2026, 9, 10), amount=Decimal("150.00"))
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        conta = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "conta", "period_text": "este mês"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        cartao = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "cartao", "period_text": "este mês"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês"},
            message="x",
            today=TODAY,
        )
    combined = conta.facts["total"] + cartao.facts["total"]
    assert combined == Decimal("230.00")
    assert combined == expenses.facts["expenses"]


def test_financial_aggregate_conta_plus_cartao_exclude_category_hint_parity_when_invoice_paid() -> None:
    """PR #107 review round 3, regression (4): `exclude_category_hint` must
    keep the same conta+cartao == get_expenses parity even when a card
    invoice was paid in the same period -- excluding Mercado must still only
    remove the Mercado-categorized rows, never touch the invoice payment
    (which was never counted as a gasto in the first place)."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="conta-cartao-parity-invoice-exclude")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    _seed_card_invoice_payment(session_factory, ids, booked_at=date(2026, 9, 10), amount=Decimal("150.00"))
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        conta = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "conta", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        cartao = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "cartao", "period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    with session_factory() as db:
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "exclude_category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    combined = conta.facts["total"] + cartao.facts["total"]
    assert combined == Decimal("80.00")
    assert combined == expenses.facts["expenses"]


def test_financial_aggregate_no_dimension_ever_counts_transfer_investment_or_reconciliation_as_expense() -> None:
    """PR #107 review round 3, regression (5): an internal transfer, a
    Privilège application/redemption and a card invoice payment must never
    surface as "gasto" under ANY `financial_aggregate` dimension
    (`categoria`, `conta`, `cartao`, `mes`, `titular`) -- only the one real
    R$80 expense should ever appear, and only under `conta`/`titular`/`mes`;
    `categoria`/`cartao` legitimately have nothing to show."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="no-dimension-counts-non-operating")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 4),
        amount=Decimal("-80.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["internal_transfer_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-1000.00"),
        transaction_type="transfer",
        description="Transferência entre contas",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["patrimonial_id"],
        booked_at=date(2026, 9, 6),
        amount=Decimal("-2000.00"),
        transaction_type="transfer",
        description="Aplicação Privilège",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["patrimonial_id"],
        booked_at=date(2026, 9, 7),
        amount=Decimal("500.00"),
        transaction_type="transfer",
        description="Resgate Privilège",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 8),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_card_invoice_payment(session_factory, ids, booked_at=date(2026, 9, 10), amount=Decimal("300.00"))
    user = _admin_user(session_factory, household_id=ids["household_id"])

    for dimension, expected in (
        ("categoria", {"Combustível": Decimal("80.00"), "Mercado": Decimal("300.00")}),
        ("conta", {"Conta Corrente": Decimal("80.00")}),
        ("cartao", {"Cartão Nubank": Decimal("300.00")}),
        ("mes", {"2026-09": Decimal("380.00")}),
        ("titular", {"Família": Decimal("380.00")}),
    ):
        with session_factory() as db:
            outcome = run_tool(
                db,
                user=user,
                tool="financial_aggregate",
                arguments={"dimension": dimension, "period_text": "este mês"},
                message="x",
                today=TODAY,
            )
        rows = {row["label"]: row["amount"] for row in outcome.facts["rows"]}
        assert rows == expected, f"dimension={dimension} leaked a non-operating movement: {rows}"
        assert outcome.facts["total"] == sum(expected.values(), Decimal("0"))


# ---------------------------------------------------------------------------
# 4c. WA-05 (docs/WORK_ORDER_WA_05.md, issue #77) "e se continuar nessa
#     média até o fim do mês?" -- project_category_pace: deterministic
#     linear pace estimate, current month only.
# ---------------------------------------------------------------------------


def test_project_category_pace_extrapolates_linearly_for_a_category() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-category")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 10),
        amount=Decimal("-150.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={"category_hint": "mercado"},
            message="e se continuar nessa média até o fim do mês?",
            today=TODAY,  # 2026-09-17: September has 30 days.
        )
    assert outcome.facts["period"] == "2026-09"
    assert outcome.facts["category_hint"] == "mercado"
    assert outcome.facts["elapsed_days"] == 17
    assert outcome.facts["days_in_month"] == 30
    assert outcome.facts["amount_so_far"] == Decimal("450.00")
    # 450 * 30 / 17 = 794.1176... -> money() rounds HALF_UP to the cent.
    assert outcome.facts["projected_total"] == Decimal("794.12")


def test_project_category_pace_without_category_hint_projects_total_operational_expense() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-total")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={},
            message="e se continuar nessa média até o fim do mês?",
            today=TODAY,
        )
    assert outcome.facts["category_hint"] is None
    assert outcome.facts["amount_so_far"] == Decimal("500.00")
    assert outcome.facts["projected_total"] == Decimal("882.35")  # 500 * 30 / 17


def test_project_category_pace_day_one_never_divides_by_zero() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-day-one")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 1),
        amount=Decimal("-100.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={"category_hint": "mercado"},
            message="e se continuar nessa média?",
            today=date(2026, 9, 1),
        )
    assert outcome.facts["elapsed_days"] == 1
    assert outcome.facts["amount_so_far"] == Decimal("100.00")
    assert outcome.facts["projected_total"] == Decimal("3000.00")  # 100 * 30 / 1


def test_project_category_pace_days_in_month_covers_28_29_30_31_day_boundaries() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-month-lengths")
    user = _admin_user(session_factory, household_id=ids["household_id"])

    # No transactions seeded: amount_so_far/projected_total are both 0 for
    # every case below -- this test only exercises `calendar.monthrange`'s
    # day-count boundary, not the linear extrapolation itself (covered by
    # the tests above).
    cases = [
        (date(2027, 2, 10), 28),  # February, non-leap year.
        (date(2028, 2, 10), 29),  # February, leap year.
        (date(2026, 4, 10), 30),  # April.
        (date(2026, 1, 10), 31),  # January.
    ]
    for today, expected_days in cases:
        with session_factory() as db:
            outcome = run_tool(
                db,
                user=user,
                tool="project_category_pace",
                arguments={},
                message="e se continuar nessa média?",
                today=today,
            )
        assert outcome.facts["days_in_month"] == expected_days
        assert outcome.facts["elapsed_days"] == 10
        assert outcome.facts["amount_so_far"] == Decimal("0")
        assert outcome.facts["projected_total"] == Decimal("0.00")


def test_project_category_pace_refuses_a_past_or_future_month() -> None:
    """Technical Challenge resolution (PR #107): a linear pace estimate is
    only meaningful for the CURRENT, still-in-progress month -- a past month
    already has its final total, and a future month has no data at all.
    Never guessed; always a clarifying question, never a mutation."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-wrong-period")
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={"period_text": "agosto"},
            message="e se continuar nessa média em agosto?",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.clarifying_question is not None
    assert outcome.facts == {}


def test_project_category_pace_unresolvable_period_text_asks_never_guesses() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-unresolvable")
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={"period_text": "período qualquer inexistente"},
            message="e aí, como fica?",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.clarifying_question is not None
    assert outcome.facts == {}


def test_project_category_pace_amount_so_far_has_parity_with_get_expenses_current_month() -> None:
    """`amount_so_far` is exactly the same canonical current-month category
    figure `get_expenses` already reports -- never a second,
    independently-computed number that could disagree with the Dashboard."""

    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="pace-parity")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        pace = run_tool(
            db,
            user=user,
            tool="project_category_pace",
            arguments={"category_hint": "mercado"},
            message="e se continuar nessa média?",
            today=TODAY,
        )
    with session_factory() as db:
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "category_hint": "mercado"},
            message="x",
            today=TODAY,
        )
    assert pace.facts["amount_so_far"] == expenses.facts["expenses"]


# ---------------------------------------------------------------------------
# 5. get_commitments -- REALIZADO/COMPROMETIDO never merged.
# ---------------------------------------------------------------------------


def test_get_commitments_keeps_realized_and_committed_separate() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="commitments")
    with session_factory() as db:
        db.add(
            Obligation(
                household_id=ids["household_id"],
                name="Aluguel da chácara",
                due_date=date(2026, 9, 10),
                amount=Decimal("1200.00"),
                category="chacara",
                status="paid",
                paid_at=date(2026, 9, 10),
            )
        )
        db.add(
            Obligation(
                household_id=ids["household_id"],
                name="Internet",
                due_date=date(2026, 9, 20),
                amount=Decimal("150.00"),
                category="utilidades",
                status="pending",
            )
        )
        db.commit()
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_commitments",
            arguments={"period_text": "este mês"},
            message="quais são minhas obrigações esse mês?",
            today=TODAY,
        )
    assert outcome.facts["realized_total"] == Decimal("1200.00")
    assert outcome.facts["committed_total"] == Decimal("150.00")
    assert len(outcome.facts["realized"]) == 1
    assert len(outcome.facts["committed"]) == 1
    assert outcome.facts["realized"][0]["financial_state"] == "REALIZADO"
    assert outcome.facts["committed"][0]["financial_state"] == "COMPROMETIDO"


# ---------------------------------------------------------------------------
# 6. get_installments -- contracted / impact this month / future remaining.
# ---------------------------------------------------------------------------


def test_get_installments_reports_contracted_impact_and_future_remaining() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="installments")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=None,
        booked_at=date(2026, 9, 5),
        amount=Decimal("-500.00"),
        transaction_type="expense",
        description="Loja XPTO Parcela 2/10",
        installment_current=2,
        installment_total=10,
        competence="2026-09",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_installments",
            arguments={"merchant_hint": "Loja XPTO"},
            message="como está a parcela da loja XPTO?",
            today=TODAY,
        )
    assert outcome.ok is True
    purchase = outcome.facts["purchases"][0]
    assert purchase["contracted_total"] == Decimal("5000.00")
    assert purchase["already_paid"] == Decimal("1000.00")
    assert purchase["future_remaining"] == Decimal("4000.00")
    assert purchase["impact_this_month"] == Decimal("500.00")
    assert purchase["installment_current"] == 2
    assert purchase["installment_total"] == 10


def test_get_installments_no_match_asks_never_guesses() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="installments-ask")
    user = _admin_user(session_factory, household_id=ids["household_id"])
    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="get_installments",
            arguments={"merchant_hint": "loja que não existe"},
            message="como está minha parcela?",
            today=TODAY,
        )
    assert outcome.ok is True
    assert outcome.clarifying_question is not None


# ---------------------------------------------------------------------------
# 7. search_transactions -- raw, filtered, bounded listing.
# ---------------------------------------------------------------------------


def test_search_transactions_filters_by_category_holder_and_type() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="search")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
        description="Supermercado Extra",
        owner_label="Vinicius",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["card_id"],
        category_id=ids["fuel_id"],
        booked_at=date(2026, 9, 5),
        amount=Decimal("-200.00"),
        transaction_type="expense",
        description="Posto Shell",
        owner_label="Kelly",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="search_transactions",
            arguments={"period_text": "este mês", "holder_hint": "Vinicius"},
            message="quais foram minhas transações esse mês?",
            today=TODAY,
        )
    assert outcome.facts["total_matches"] == 1
    assert outcome.facts["transactions"][0]["description"] == "Supermercado Extra"


def test_search_transactions_caps_results_never_dumps_the_whole_corpus() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="search-cap")
    for index in range(25):
        _seed_transaction(
            session_factory,
            household_id=ids["household_id"],
            account_id=ids["checking_id"],
            category_id=ids["market_id"],
            booked_at=date(2026, 9, 1 + (index % 28)),
            amount=Decimal("-10.00"),
            transaction_type="expense",
            description=f"Compra {index}",
        )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    with session_factory() as db:
        outcome = run_tool(
            db,
            user=user,
            tool="search_transactions",
            arguments={"period_text": "este mês"},
            message="lista minhas compras desse mês",
            today=TODAY,
        )
    assert outcome.facts["total_matches"] == 25
    assert len(outcome.facts["transactions"]) == 20


# ---------------------------------------------------------------------------
# 8. Household isolation -- every new tool.
# ---------------------------------------------------------------------------


def test_household_isolation_new_tools_never_see_another_households_data() -> None:
    session_factory = _session_factory()
    mine = _seed_household(session_factory, username="isolation-mine")
    theirs = _seed_household(session_factory, username="isolation-theirs")
    _seed_transaction(
        session_factory,
        household_id=theirs["household_id"],
        account_id=theirs["checking_id"],
        category_id=theirs["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-9999.00"),
        transaction_type="expense",
        description="Segredo de outra família",
    )
    with session_factory() as db:
        db.add(
            Obligation(
                household_id=theirs["household_id"],
                name="Obrigação de outra família",
                due_date=date(2026, 9, 10),
                amount=Decimal("9999.00"),
                status="pending",
            )
        )
        db.commit()
    user = _admin_user(session_factory, household_id=mine["household_id"])

    with session_factory() as db:
        expenses = run_tool(
            db, user=user, tool="get_expenses", arguments={"period_text": "este mês"}, message="x", today=TODAY
        )
        search = run_tool(
            db,
            user=user,
            tool="search_transactions",
            arguments={"period_text": "este mês"},
            message="x",
            today=TODAY,
        )
        commitments = run_tool(
            db, user=user, tool="get_commitments", arguments={"period_text": "este mês"}, message="x", today=TODAY
        )
    assert expenses.facts["expenses"] == Decimal("0.00")
    assert search.facts["total_matches"] == 0
    assert commitments.facts["committed_total"] == Decimal("0")


def test_household_isolation_titular_dimension_and_combined_filters() -> None:
    """PR #106 review round 1: the new `titular` grouping dimension and the
    new combined category+account+holder filtering both read `RangeTotals.
    detail_rows`, a brand new code path -- must be re-proven independently
    of the pre-existing isolation test above. The other household's holder
    label ("Vinicius") deliberately collides with a label our own household
    could plausibly also use, so this also proves isolation is keyed on
    `household_id`, never accidentally on the free-text holder string."""

    session_factory = _session_factory()
    mine = _seed_household(session_factory, username="isolation-titular-mine")
    theirs = _seed_household(session_factory, username="isolation-titular-theirs")
    _seed_transaction(
        session_factory,
        household_id=theirs["household_id"],
        account_id=theirs["checking_id"],
        category_id=theirs["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-9999.00"),
        transaction_type="expense",
        description="Segredo de outra família",
        owner_label="Vinicius",
    )
    user = _admin_user(session_factory, household_id=mine["household_id"])

    with session_factory() as db:
        aggregate = run_tool(
            db,
            user=user,
            tool="financial_aggregate",
            arguments={"dimension": "titular", "period_text": "este mês"},
            message="quanto cada um gastou esse mês?",
            today=TODAY,
        )
        expenses = run_tool(
            db,
            user=user,
            tool="get_expenses",
            arguments={"period_text": "este mês", "category_hint": "mercado", "holder_hint": "Vinicius"},
            message="x",
            today=TODAY,
        )
    assert aggregate.facts["rows"] == []
    assert expenses.facts["expenses"] == Decimal("0.00")


# ---------------------------------------------------------------------------
# 9. Orchestrator -- multi-tool composition with the new generic tools.
# ---------------------------------------------------------------------------


def test_plan_and_execute_composes_get_expenses_then_get_income_in_one_answer() -> None:
    session_factory = _session_factory()
    ids = _seed_household(session_factory, username="compose")
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["market_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("-300.00"),
        transaction_type="expense",
    )
    _seed_transaction(
        session_factory,
        household_id=ids["household_id"],
        account_id=ids["checking_id"],
        category_id=ids["income_id"],
        booked_at=date(2026, 9, 3),
        amount=Decimal("5000.00"),
        transaction_type="income",
    )
    user = _admin_user(session_factory, household_id=ids["household_id"])

    response = _valid_plan_response(
        steps=[
            {"tool": "get_expenses", "arguments": {"period_text": "este mês"}},
            {"tool": "get_income", "arguments": {"period_text": "este mês"}},
        ]
    )
    with session_factory() as db:
        result = plan_and_execute(
            db, user=user, message="quanto gastei e quanto recebi esse mês?", client=FakePlanClient(response=response)
        )
    assert result.needs_clarification is False
    assert "300,00" in result.answer
    assert "5.000,00" in result.answer
    assert len(result.trace) == 3  # plan + 2 tool steps
