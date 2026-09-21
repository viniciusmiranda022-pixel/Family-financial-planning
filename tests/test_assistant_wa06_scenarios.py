"""Tests for WA-06 (`docs/WORK_ORDER_WA_06.md`, issue #78) -- the
`simulate_purchase` hypothetical purchase/cash-impact scenario tool.

Mirrors the structure/fixtures of `tests/test_assistant_wa04_query.py`/
`tests/test_assistant_orchestrator.py` (WA-02/WA-04): a seeded in-memory
SQLite session, `run_tool`/`plan_and_execute` exercised directly (never
through HTTP), and reuse of that file's `FakePlanClient`/
`_valid_plan_response`/`_session_factory`/`_admin_user` fixtures instead of
duplicating them.

Covers the Work Order's "Testes obrigatórios": zero/negative amount,
month/year boundaries, leap February, an existing commitment, zero DB
mutation (no `Transaction`/`Obligation`/`IntegrityRun` created), REALIZADO/
COMPROMETIDO/PREVISTO/HIPÓTESE separation in the rendered answer, and that
no favorable/caution/not_recommended verdict is ever synthesized.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-assistant-wa06-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "assistant-wa06-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.models import (  # noqa: E402
    AuditEvent,
    FinancialProfile,
    Household,
    IntegrityRun,
    Obligation,
    Transaction,
    User,
)
from app.services.assistant_orchestrator import _format_step, plan_and_execute  # noqa: E402
from app.services.assistant_tool_catalog import ALLOWED_TOOLS, TOOL_ARGUMENT_KEYS  # noqa: E402
from app.services.assistant_tools import run_tool  # noqa: E402
from app.services.finance import add_months, money  # noqa: E402
from tests.test_assistant_orchestrator import (  # noqa: E402
    FakePlanClient,
    _admin_user,
    _session_factory,
    _valid_plan_response,
)

TODAY = date(2026, 9, 17)


def _seed_household(
    session_factory,
    *,
    username: str,
    monthly_cash_cap: Decimal = Decimal("5000"),
    emergency_floor: Decimal = Decimal("1000"),
    monthly_salary_net: Decimal = Decimal("0"),
    projection_end: date = date(2026, 12, 1),
) -> str:
    with session_factory() as db:
        household = Household(name=f"Família {username}")
        db.add(household)
        db.flush()
        household_id = household.id
        db.add(
            FinancialProfile(
                household_id=household_id,
                monthly_cash_cap=monthly_cash_cap,
                emergency_floor=emergency_floor,
                monthly_salary_net=monthly_salary_net,
                projection_end=projection_end,
            )
        )
        db.add(
            User(
                household_id=household_id,
                name="Admin",
                username=username,
                password_hash="x",
                is_admin=True,
            )
        )
        db.commit()
    return household_id


# ---------------------------------------------------------------------------
# 1. Catalog wiring -- present, allowlisted, argument keys as documented.
# ---------------------------------------------------------------------------


def test_simulate_purchase_is_registered_in_the_catalog() -> None:
    assert "simulate_purchase" in ALLOWED_TOOLS
    assert TOOL_ARGUMENT_KEYS["simulate_purchase"] == {
        "amount_text",
        "installments_text",
        "monthly_interest_rate_text",
    }


# ---------------------------------------------------------------------------
# 2. Never a guess -- amount is mandatory, zero/negative is not a value.
# ---------------------------------------------------------------------------


def _run(session_factory, *, household_id: str, arguments: dict[str, str], today: date = TODAY):
    user = _admin_user(session_factory, household_id=household_id)
    with session_factory() as db:
        return run_tool(
            db,
            user=user,
            tool="simulate_purchase",
            arguments=arguments,
            message="posso gastar?",
            today=today,
        )


def test_missing_amount_asks_never_guesses() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-missing")
    outcome = _run(session_factory, household_id=household_id, arguments={})
    assert outcome.ok is True
    assert outcome.clarifying_question
    assert outcome.facts == {}


def test_zero_and_negative_amount_are_never_treated_as_a_value() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-zero-negative")
    for amount_text in ("0", "-100", "abacate"):
        outcome = _run(
            session_factory, household_id=household_id, arguments={"amount_text": amount_text}
        )
        assert outcome.ok is True
        assert outcome.clarifying_question, f"amount_text={amount_text!r} should not resolve to a value"


# ---------------------------------------------------------------------------
# 3. Cash purchase (à vista) -- deterministic amortization, zero writes.
# ---------------------------------------------------------------------------


def test_cash_purchase_reports_full_amount_as_a_single_installment() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-cash")
    outcome = _run(session_factory, household_id=household_id, arguments={"amount_text": "300"})
    assert outcome.ok is True
    assert outcome.clarifying_question is None
    facts = outcome.facts
    assert facts["amount"] == Decimal("300.00")
    assert facts["installments"] == 1
    assert facts["monthly_interest_rate"] == Decimal("0")
    assert facts["monthly_payment"] == Decimal("300.00")
    assert facts["total_cost"] == Decimal("300.00")
    assert facts["projection_start_month"] == "2026-10"
    assert facts["current_period"] == "2026-09"
    # Baseline/with-purchase both present, and the purchase's committed
    # schedule can never *improve* the projected minimum balance.
    assert facts["with_purchase_minimum_balance"] <= facts["baseline_minimum_balance"]
    assert isinstance(facts["baseline_crosses_safety_floor"], bool)
    assert isinstance(facts["with_purchase_crosses_safety_floor"], bool)


def test_installments_with_interest_use_the_canonical_amortization_formula() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-installments")
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={
            "amount_text": "1000",
            "installments_text": "10",
            "monthly_interest_rate_text": "2%",
        },
    )
    assert outcome.ok is True
    facts = outcome.facts
    assert facts["installments"] == 10
    assert facts["monthly_interest_rate"] == Decimal("0.02")
    # Parity against the exact same shared primitive, never a second formula.
    from app.services.finance import amortized_installment_payment

    expected_payment, expected_total = amortized_installment_payment(
        Decimal("1000"), 10, Decimal("0.02")
    )
    assert facts["monthly_payment"] == expected_payment
    assert facts["total_cost"] == expected_total
    assert facts["total_cost"] > facts["amount"]  # interest is never free


def test_installments_without_interest_split_evenly_with_half_up_rounding() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-rounding")
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "100", "installments_text": "3"},
    )
    facts = outcome.facts
    assert facts["monthly_payment"] == Decimal("33.33")
    assert facts["total_cost"] == Decimal("99.99")


def test_out_of_range_or_garbled_installments_and_rate_fall_back_to_documented_defaults() -> None:
    """An optional field's own default (1x, sem juros) is used instead of
    blocking on a clarifying question for a soft/garbled hint -- same
    tolerance idiom as `_normalize_type_hint`."""

    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-garbled")
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={
            "amount_text": "300",
            "installments_text": "duzentas",
            "monthly_interest_rate_text": "novecentos",
        },
    )
    assert outcome.ok is True
    assert outcome.clarifying_question is None
    assert outcome.facts["installments"] == 1
    assert outcome.facts["monthly_interest_rate"] == Decimal("0")

    too_many = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "300", "installments_text": "999"},
    )
    assert too_many.facts["installments"] == 1

    absurd_rate = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "300", "monthly_interest_rate_text": "500%"},
    )
    assert absurd_rate.facts["monthly_interest_rate"] == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Month/year boundaries, leap February -- no crash, correct horizon.
# ---------------------------------------------------------------------------


def test_year_boundary_rolls_projection_start_into_january() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(
        session_factory, username="wa06-year-boundary", projection_end=date(2027, 3, 1)
    )
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "300"},
        today=date(2026, 12, 31),
    )
    assert outcome.ok is True
    assert outcome.facts["projection_start_month"] == "2027-01"
    assert outcome.facts["current_period"] == "2026-12"


def test_leap_february_boundary_does_not_crash_and_resolves_correctly() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(
        session_factory, username="wa06-leap-feb", projection_end=date(2028, 4, 1)
    )
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "300", "installments_text": "3"},
        today=date(2028, 1, 15),
    )
    assert outcome.ok is True
    assert outcome.facts["projection_start_month"] == "2028-02"
    assert add_months(date(2028, 2, 1), 1) == date(2028, 3, 1)  # 2028 is a leap year (29-day Feb)


# ---------------------------------------------------------------------------
# 5. Existing commitments -- the hypothetical purchase adds on top, never
#    replacing/hiding what was already committed.
# ---------------------------------------------------------------------------


def test_existing_commitment_is_reflected_in_both_baseline_and_with_purchase() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(
        session_factory,
        username="wa06-existing-commitment",
        monthly_cash_cap=Decimal("2000"),
        emergency_floor=Decimal("500"),
    )
    with session_factory() as db:
        db.add(
            Obligation(
                household_id=household_id,
                name="Aluguel da chácara",
                due_date=date(2026, 10, 10),
                amount=Decimal("1800.00"),
                category="moradia",
            )
        )
        db.commit()

    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "500", "installments_text": "6"},
    )
    assert outcome.ok is True
    facts = outcome.facts
    # Adding the hypothetical purchase's installments on top of the already
    # committed obligation never produces a *better* minimum balance.
    assert facts["with_purchase_minimum_balance"] <= facts["baseline_minimum_balance"]


# ---------------------------------------------------------------------------
# 6. Zero DB mutation -- no Transaction/Obligation/IntegrityRun is ever
#    created by this purely hypothetical tool.
# ---------------------------------------------------------------------------


def _row_counts(session_factory, *, household_id: str) -> tuple[int, int, int]:
    with session_factory() as db:
        transactions = db.query(Transaction).filter(Transaction.household_id == household_id).count()
        obligations = db.query(Obligation).filter(Obligation.household_id == household_id).count()
        integrity_runs = db.query(IntegrityRun).filter(IntegrityRun.household_id == household_id).count()
    return transactions, obligations, integrity_runs


def test_simulate_purchase_never_persists_a_transaction_obligation_or_integrity_run() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-zero-mutation")
    before = _row_counts(session_factory, household_id=household_id)
    _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "1200", "installments_text": "12", "monthly_interest_rate_text": "1.5%"},
    )
    after = _row_counts(session_factory, household_id=household_id)
    assert after == before


# ---------------------------------------------------------------------------
# 7. Orchestrator formatting -- HIPÓTESE labeled, no synthesized verdict.
# ---------------------------------------------------------------------------


def test_formatted_answer_labels_hipotese_and_never_synthesizes_a_verdict() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-format")
    outcome = _run(
        session_factory, household_id=household_id, arguments={"amount_text": "300"}
    )
    text = _format_step("simulate_purchase", outcome.facts)
    assert "HIPÓTESE" in text
    lowered = text.lower()
    for verdict_word in ("recomendo", "não recomendo", "recomendado", "favorável", "desfavorável"):
        assert verdict_word not in lowered


def test_plan_and_execute_runs_simulate_purchase_with_only_the_read_scoped_audit() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-orchestrator")
    user = _admin_user(session_factory, household_id=household_id)

    before_audit = None
    with session_factory() as db:
        before_audit = db.query(AuditEvent).filter(AuditEvent.household_id == household_id).count()

    response = _valid_plan_response(
        steps=[{"tool": "simulate_purchase", "arguments": {"amount_text": "300"}}]
    )
    with session_factory() as db:
        result = plan_and_execute(
            db, user=user, message="posso gastar 300 reais?", client=FakePlanClient(response=response)
        )
    assert "HIPÓTESE" in result.answer
    assert result.needs_clarification is False

    transactions, obligations, integrity_runs = _row_counts(session_factory, household_id=household_id)
    assert (transactions, obligations, integrity_runs) == (0, 0, 0)

    with session_factory() as db:
        after_audit = db.query(AuditEvent).filter(AuditEvent.household_id == household_id).count()
    assert after_audit == before_audit + 1
    with session_factory() as db:
        event = (
            db.query(AuditEvent)
            .filter(AuditEvent.household_id == household_id)
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert event.event_type == "assistant.orchestrate"


# ---------------------------------------------------------------------------
# 8. Household isolation -- one household's cap/floor never leak into
#    another household's simulation.
# ---------------------------------------------------------------------------


def test_household_isolation_uses_each_households_own_profile() -> None:
    session_factory = _session_factory()
    household_a = _seed_household(
        session_factory, username="wa06-house-a", monthly_cash_cap=Decimal("1000")
    )
    household_b = _seed_household(
        session_factory, username="wa06-house-b", monthly_cash_cap=Decimal("9000")
    )
    outcome_a = _run(session_factory, household_id=household_a, arguments={"amount_text": "300"})
    outcome_b = _run(session_factory, household_id=household_b, arguments={"amount_text": "300"})
    assert outcome_a.facts["cash_cap"] == money(Decimal("1000"))
    assert outcome_b.facts["cash_cap"] == money(Decimal("9000"))


# ---------------------------------------------------------------------------
# 9. Insufficient cash -- a baseline already below the safety floor never
#    crashes, and the purchase never makes the projected minimum better.
# ---------------------------------------------------------------------------


def test_insufficient_cash_baseline_already_below_floor_does_not_crash() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(
        session_factory,
        username="wa06-insufficient-cash",
        monthly_cash_cap=Decimal("100"),
        emergency_floor=Decimal("50000"),  # unreachable floor -> baseline already crosses it
        monthly_salary_net=Decimal("0"),
    )
    outcome = _run(
        session_factory,
        household_id=household_id,
        arguments={"amount_text": "2000", "installments_text": "5"},
    )
    assert outcome.ok is True
    facts = outcome.facts
    assert facts["baseline_crosses_safety_floor"] is True
    assert facts["with_purchase_crosses_safety_floor"] is True
    assert facts["with_purchase_minimum_balance"] <= facts["baseline_minimum_balance"]


# ---------------------------------------------------------------------------
# 10. Open diagnostic composition -- bounded multi-tool plan, no invented
#     arithmetic beyond what each tool already returned.
# ---------------------------------------------------------------------------


def test_open_diagnostic_composes_bounded_existing_tools_not_simulate_purchase() -> None:
    """'Por que meu dinheiro está acabando mais rápido?' has no tool of its
    own (Work Order WA-06): it is answered by composing existing read-only
    tools (income, expenses, concentration, trend, commitments) within the
    same `MAX_PLAN_STEPS` bound every other compound plan already respects."""

    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-diagnostic")
    user = _admin_user(session_factory, household_id=household_id)

    diagnostic_steps = [
        {"tool": "get_income", "arguments": {"period_text": "este mês"}},
        {"tool": "get_expenses", "arguments": {"period_text": "este mês"}},
        {
            "tool": "financial_aggregate",
            "arguments": {"dimension": "categoria", "metric": "participacao", "period_text": "este mês"},
        },
        {
            "tool": "financial_aggregate",
            "arguments": {"dimension": "mes", "period_text": "últimos 3 meses"},
        },
        {"tool": "get_commitments", "arguments": {}},
    ]
    assert len(diagnostic_steps) == 5  # exactly MAX_PLAN_STEPS -- the documented bound
    response = _valid_plan_response(steps=diagnostic_steps)
    with session_factory() as db:
        result = plan_and_execute(
            db,
            user=user,
            message="por que meu dinheiro está acabando mais rápido?",
            client=FakePlanClient(response=response),
        )
    assert result.needs_clarification is False
    tool_trace = [entry.get("tool") for entry in result.trace if entry["stage"] == "tool"]
    assert tool_trace == [
        "get_income",
        "get_expenses",
        "financial_aggregate",
        "financial_aggregate",
        "get_commitments",
    ]
    # Every step actually resolved to facts (nothing failed/needed clarification
    # mid-plan) -- the final answer is the concatenation of what the tools
    # returned, never a number invented on top of them.
    assert all(entry["ok"] for entry in result.trace if entry["stage"] == "tool")


# ---------------------------------------------------------------------------
# 11. WA-05 conversational context -- simulate_purchase is a read-only hint
#     eligible for a follow-up turn, exactly like the other WA-04/05 tools.
# ---------------------------------------------------------------------------


def test_simulate_purchase_step_is_remembered_as_a_context_hint_for_the_next_turn() -> None:
    session_factory = _session_factory()
    household_id = _seed_household(session_factory, username="wa06-context")
    user = _admin_user(session_factory, household_id=household_id)

    first_response = _valid_plan_response(
        steps=[{"tool": "simulate_purchase", "arguments": {"amount_text": "300"}}]
    )
    with session_factory() as db:
        plan_and_execute(
            db,
            user=user,
            message="posso gastar 300 reais à vista?",
            conversation_id="ctx-wa06",
            client=FakePlanClient(response=first_response),
        )

    second_client = FakePlanClient(
        response=_valid_plan_response(steps=[{"tool": "query_facts", "arguments": {"topic": "saldo"}}])
    )
    with session_factory() as db:
        plan_and_execute(
            db,
            user=user,
            message="e qual meu saldo?",
            conversation_id="ctx-wa06",
            client=second_client,
        )
    assert second_client.captured_payload["context_hints"] == [
        {"tool": "simulate_purchase", "arguments": {"amount_text": "300"}}
    ]
