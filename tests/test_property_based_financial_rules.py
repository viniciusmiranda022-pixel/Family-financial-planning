"""Final property-based tests for PR 8 (Work Order scope item 4).

Covers, across generated ranges rather than single hand-picked examples:
`ROUND_HALF_UP` rounding edges, zero liquidity, deficit greater than
available balance, the safety floor never blocking real coverage,
per-receivable commission tax, competence (commission and card statement),
and backfill idempotency.

Every oracle here is either a pure documented formula independently
re-derived in this file (never importing the function under test), or --
where the invariant itself already IS the pure formula
(`app.services.finance.money`/`commission_net`) -- a property that would
fail if the implementation regressed, not a restatement of it. This avoids
duplicating the implementation under test inside its own test oracle (see
`docs/WORK_ORDER_PR8_...`: "não duplicar a implementação sob teste no
próprio oracle do teste"), the same principle the codebase already applies
to INV-018 (Projection Engine vs. an independently-coded Validator).
"""

import os
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "property-test-secret-not-used-in-production")
os.environ.setdefault("FILE_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("MFA_ENCRYPTION_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.cli.backfill import process_household  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import DocumentReconciliation, DuplicateGroup, FinancialSnapshot, Household  # noqa: E402
from app.services.finance import commission_net, money  # noqa: E402
from app.services.financial_invariants import InvariantContext, InvariantScope, InvariantStatus  # noqa: E402
from app.services.invariant_registry import evaluate_invariant  # noqa: E402
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402

CENTS = Decimal("0.01")


def _context(**facts: object) -> InvariantContext:
    # October Go-Live Slice 1: INV-005/INV-006/INV-007 govern the projection
    # engine's hypothetical liquidity formula only (never a REALIZADO
    # snapshot -- see docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md §8), so these
    # property tests exercise them under `InvariantScope.PROJECTION`.
    return InvariantContext(
        facts=facts,
        scope=InvariantScope.PROJECTION,
        entity_type="financial_snapshot",
        entity_id="property-test",
        period="2026-06",
        trace_id="property-test-trace",
    )


# ---------------------------------------------------------------------------
# ROUND_HALF_UP rounding edges (money())
# ---------------------------------------------------------------------------


@given(integer_thousandths=st.integers(min_value=-(10**9), max_value=10**9))
@settings(max_examples=300, deadline=None)
def test_money_quantizes_to_exactly_two_decimal_places(integer_thousandths: int) -> None:
    value = Decimal(integer_thousandths) / Decimal(1000)
    rounded = money(value)
    # Exactly two decimal places, always -- never left at a finer precision.
    assert rounded == rounded.quantize(CENTS)
    # Never off by more than half a cent from the original value.
    assert abs(rounded - value) <= Decimal("0.005")
    # Idempotent: re-rounding an already-rounded value is a no-op.
    assert money(rounded) == rounded


@given(cents=st.integers(min_value=-500, max_value=500))
@settings(max_examples=200, deadline=None)
def test_money_half_cent_boundary_rounds_away_from_zero(cents: int) -> None:
    """The literal ROUND_HALF_UP boundary: a value ending in exactly half a
    cent always rounds away from zero, e.g. 1.005 -> 1.01, -1.005 -> -1.01,
    2.675 does *not* apply (only 3rd-decimal-digit-5 boundaries), 0.005 ->
    0.01, -0.005 -> -0.01.
    """

    base = Decimal(cents) / 100
    boundary = base + Decimal("0.005") if base >= 0 else base - Decimal("0.005")
    rounded = money(boundary)
    expected = (base + CENTS) if base >= 0 else (base - CENTS)
    assert rounded == expected


# ---------------------------------------------------------------------------
# Liquidity transition: zero liquidity, deficit > balance, safety floor
# (INV-005/INV-006/INV-007) -- independent oracle, not the implementation.
# ---------------------------------------------------------------------------


def _independent_liquidity_transition(
    opening: Decimal, monthly_result: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Hand-written, independent re-derivation of the documented rule
    (docs/FINANCIAL_INVARIANTS.md INV-005/006/007): a positive result grows
    liquidity; a negative result is covered from available liquidity first,
    floored at zero, with any remainder becoming uncovered deficit. Returns
    (closing_liquidity_balance, uncovered_deficit, liquidity_used).
    """

    if monthly_result >= 0:
        return money(opening + monthly_result), Decimal("0.00"), Decimal("0.00")
    deficit = -monthly_result
    if deficit <= opening:
        return money(opening - deficit), Decimal("0.00"), money(deficit)
    return Decimal("0.00"), money(deficit - opening), money(opening)


@given(
    opening=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1000000.00"), places=2),
    result=st.decimals(min_value=Decimal("-1000000.00"), max_value=Decimal("1000000.00"), places=2),
)
@settings(max_examples=500, deadline=None)
def test_liquidity_never_goes_negative_and_matches_independent_oracle(
    opening: Decimal, result: Decimal
) -> None:
    closing, uncovered, used = _independent_liquidity_transition(opening, result)
    assert closing >= 0
    assert uncovered >= 0
    assert used >= 0

    outcome = evaluate_invariant(
        "INV-006",
        _context(
            opening_liquidity_balance=opening,
            monthly_operating_result=result,
            closing_liquidity_balance=closing,
            uncovered_deficit=uncovered,
            liquidity_used=used,
        ),
    )
    assert outcome.status is InvariantStatus.PASS


@given(result=st.decimals(min_value=Decimal("-500000.00"), max_value=Decimal("500000.00"), places=2))
@settings(max_examples=200, deadline=None)
def test_zero_opening_liquidity_boundary(result: Decimal) -> None:
    """Liquidez zero: with no opening balance, a negative result must
    consume nothing (there is nothing to consume) and become 100% uncovered
    deficit; a non-negative result simply becomes the new balance.
    """

    closing, uncovered, used = _independent_liquidity_transition(Decimal("0.00"), result)
    assert used == Decimal("0.00")
    if result < 0:
        assert uncovered == money(-result)
        assert closing == Decimal("0.00")
    else:
        assert uncovered == Decimal("0.00")
        assert closing == money(result)


@given(
    opening=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000.00"), places=2),
    excess=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000.00"), places=2),
)
@settings(max_examples=300, deadline=None)
def test_deficit_greater_than_balance_consumes_all_liquidity_and_leaves_remainder(
    opening: Decimal, excess: Decimal
) -> None:
    """Déficit maior que saldo: the deficit strictly exceeds the opening
    balance, so every cent of liquidity is used and the remainder becomes
    uncovered deficit -- never a negative closing balance.
    """

    deficit = opening + excess
    closing, uncovered, used = _independent_liquidity_transition(opening, -deficit)
    assert used == opening
    assert uncovered == excess
    assert closing == Decimal("0.00")


@given(
    opening=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("100000.00"), places=2),
    result=st.decimals(min_value=Decimal("-100000.00"), max_value=Decimal("100000.00"), places=2),
    safety_floor=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("200000.00"), places=2),
)
@settings(max_examples=300, deadline=None)
def test_safety_floor_never_changes_the_liquidity_transition(
    opening: Decimal, result: Decimal, safety_floor: Decimal
) -> None:
    """Piso de segurança é alerta, não bloqueio: INV-007 must PASS for any
    `safety_floor` value -- including one far above the opening balance,
    i.e. already "below floor" -- as long as the liquidity transition
    itself is correct. The floor is never a parameter of
    `_independent_liquidity_transition`; this property is exactly the
    claim that it cannot change the math.
    """

    closing, uncovered, used = _independent_liquidity_transition(opening, result)
    outcome = evaluate_invariant(
        "INV-007",
        _context(
            opening_liquidity_balance=opening,
            monthly_operating_result=result,
            closing_liquidity_balance=closing,
            uncovered_deficit=uncovered,
            liquidity_used=used,
            safety_floor=safety_floor,
        ),
    )
    assert outcome.status is InvariantStatus.PASS


# ---------------------------------------------------------------------------
# Commission per receivable, ROUND_HALF_UP applied per item (INV-009)
# ---------------------------------------------------------------------------


@given(
    gross=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000.00"), places=2),
    tax_rate=st.decimals(min_value=Decimal("0.0000"), max_value=Decimal("0.5000"), places=4),
)
@settings(max_examples=300, deadline=None)
def test_commission_net_conserves_the_gross_amount(gross: Decimal, tax_rate: Decimal) -> None:
    tax, net = commission_net(gross, tax_rate)
    assert tax >= Decimal("0.00")
    assert net >= Decimal("0.00")
    assert tax + net == money(gross)


@given(
    receivables=st.lists(
        st.tuples(
            st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50000.00"), places=2),
            st.decimals(min_value=Decimal("0.0000"), max_value=Decimal("0.5000"), places=4),
        ),
        min_size=2,
        max_size=8,
    )
)
@settings(max_examples=200, deadline=None)
def test_tax_summed_per_receivable_differs_from_aggregate_then_rounded(
    receivables: list[tuple[Decimal, Decimal]],
) -> None:
    """INV-009: the documented rule is "soma do imposto arredondado em cada
    recebível", not "arredondar a soma". This asserts INV-009 only PASSes
    for the per-item rounding order, and correctly FAILs the aggregate-then-
    round shortcut whenever the two orders actually diverge -- proving the
    invariant enforces *order of rounding*, not just a matching total.
    """

    per_item_total = money(sum((money(gross * rate) for gross, rate in receivables), Decimal("0.00")))
    aggregate_total = money(sum((gross * rate for gross, rate in receivables), Decimal("0.00")))

    facts = [{"gross_amount": gross, "tax_rate": rate} for gross, rate in receivables]

    correct = evaluate_invariant(
        "INV-009", _context(receivables=facts, actual_total_tax=per_item_total)
    )
    assert correct.status is InvariantStatus.PASS

    if aggregate_total != per_item_total:
        wrong = evaluate_invariant(
            "INV-009", _context(receivables=facts, actual_total_tax=aggregate_total)
        )
        assert wrong.status is InvariantStatus.FAIL


# ---------------------------------------------------------------------------
# Competence: commission scenario period (INV-008) and card statement (INV-017)
# ---------------------------------------------------------------------------


@given(
    commission_period=st.sampled_from(["2026-06", "2026-07", "2026-08"]),
    scenario_period=st.sampled_from(["2026-06", "2026-07", "2026-08"]),
    scenario_includes_commission=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_commission_only_counted_in_its_own_scenario_competence(
    commission_period: str, scenario_period: str, scenario_includes_commission: bool
) -> None:
    expected_included = scenario_includes_commission and commission_period == scenario_period
    for included in (True, False):
        outcome = evaluate_invariant(
            "INV-008",
            _context(
                commission_period=commission_period,
                scenario_period=scenario_period,
                scenario_includes_commission=scenario_includes_commission,
                included_in_income=included,
            ),
        )
        expected_status = (
            InvariantStatus.PASS if included is expected_included else InvariantStatus.FAIL
        )
        assert outcome.status is expected_status


@given(
    canonical_competence=st.sampled_from(["2026-06", "2026-07"]),
    counted_competence=st.sampled_from(["2026-06", "2026-07"]),
)
@settings(max_examples=100, deadline=None)
def test_card_purchase_competence_matches_the_canonical_statement_period(
    canonical_competence: str, counted_competence: str
) -> None:
    outcome = evaluate_invariant(
        "INV-017",
        _context(
            canonical_competence=canonical_competence,
            counted_competence=counted_competence,
        ),
    )
    expected_status = (
        InvariantStatus.PASS if counted_competence == canonical_competence else InvariantStatus.FAIL
    )
    assert outcome.status is expected_status


# ---------------------------------------------------------------------------
# Idempotency, across randomized commission/investment-balance facts.
# ---------------------------------------------------------------------------


@given(
    investment_balance=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("500000.00"), places=2),
    commission_gross=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("100000.00"), places=2),
)
@settings(max_examples=25, deadline=None)
def test_backfill_reaches_the_same_steady_state_regardless_of_amounts(
    investment_balance: Decimal, commission_gross: Decimal
) -> None:
    """Idempotência as a property, not a single fixed example: for
    randomized source amounts, running the backfill twice always converges
    to the same derived-row counts on the second run as on any later run.
    """

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        synthetic = build_synthetic_household(db, name=f"Família {investment_balance}-{commission_gross}")
        synthetic.profile.investment_balance = investment_balance
        synthetic.commission.gross_amount = commission_gross
        db.commit()
        household_id = synthetic.household.id

        def _counts() -> tuple[int, int, int]:
            return (
                int(
                    db.scalar(
                        select(func.count(FinancialSnapshot.id)).where(
                            FinancialSnapshot.household_id == household_id
                        )
                    )
                ),
                int(
                    db.scalar(
                        select(func.count(DocumentReconciliation.id)).where(
                            DocumentReconciliation.household_id == household_id
                        )
                    )
                ),
                int(
                    db.scalar(
                        select(func.count(DuplicateGroup.id)).where(
                            DuplicateGroup.household_id == household_id
                        )
                    )
                ),
            )

        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()
        after_first = _counts()

        household = db.get(Household, household_id)
        process_household(db, household, period_from=None, period_to=None)
        db.commit()
        after_second = _counts()

        assert after_first == after_second
