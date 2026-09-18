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
