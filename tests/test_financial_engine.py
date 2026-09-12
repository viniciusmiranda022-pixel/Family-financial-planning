from decimal import Decimal

from app.services.financial_engine import (
    FinancialEngineInput,
    calculate_actual_snapshot,
    reconcile_actual_liquidity,
    settle_liquidity,
    settle_liquidity_projection,
)


def test_privilege_deficit_is_limited_by_available_balance() -> None:
    """`settle_liquidity`/`settle_liquidity_projection` is the PROJECTION-only
    hypothetical formula (October Go-Live Slice 1) -- unchanged by the
    rebaseline, since projection scenarios are exactly where "what if the
    household kept this pace" math belongs."""

    transition = settle_liquidity_projection(
        opening_balance=Decimal("87068.54"),
        operating_result=Decimal("-270014.03"),
        safety_floor=Decimal("10000"),
    )

    assert transition.liquidity_used == Decimal("87068.54")
    assert transition.closing_balance == Decimal("0.00")
    assert transition.closing_uncovered_deficit == Decimal("182945.49")
    assert transition.distance_to_floor == Decimal("-10000.00")
    # The two names are the same function -- the rename only clarifies intent.
    assert settle_liquidity is settle_liquidity_projection


def test_future_surplus_pays_uncovered_deficit_before_rebuilding_liquidity() -> None:
    still_in_debt = settle_liquidity_projection(
        opening_balance=Decimal("999"),
        opening_uncovered_deficit=Decimal("1000"),
        operating_result=Decimal("600"),
    )
    recovered = settle_liquidity_projection(
        opening_balance=Decimal("0"),
        opening_uncovered_deficit=Decimal("1000"),
        operating_result=Decimal("1200"),
    )

    assert still_in_debt.opening_balance == Decimal("0.00")
    assert still_in_debt.closing_balance == Decimal("0.00")
    assert still_in_debt.closing_uncovered_deficit == Decimal("400.00")
    assert recovered.closing_uncovered_deficit == Decimal("0.00")
    assert recovered.closing_balance == Decimal("200.00")


def test_snapshot_checksum_is_deterministic_and_money_is_rounded() -> None:
    data = FinancialEngineInput(
        period="2026-08",
        operating_income=Decimal("5000.004"),
        operating_expenses=Decimal("1200.005"),
        opening_liquidity_balance=Decimal("10000"),
        budget_cap=Decimal("3000"),
        source_count=2,
    )

    first = calculate_actual_snapshot(data)
    second = calculate_actual_snapshot(data)

    assert first.operating_income == Decimal("5000.00")
    assert first.operating_expenses == Decimal("1200.01")
    # October Go-Live Slice 1: with no evidenced "Transferência patrimonial"
    # movement (`investments`/`redemptions` both default to zero), the
    # closing Privilège balance is simply the opening balance, untouched --
    # never synthesized from the operating result's sign.
    assert first.closing_liquidity_balance == Decimal("10000.00")
    assert first.liquidity_used == Decimal("0.00")
    assert first.liquidity_deposit == Decimal("0.00")
    assert first.has_transfer_evidence is False
    assert first.unexplained_operating_result == first.operating_result
    assert first.checksum() == second.checksum()


# ---------------------------------------------------------------------------
# October Go-Live Slice 1 -- `reconcile_actual_liquidity` /
# `calculate_actual_snapshot` (REALIZADO): evidence-based liquidity only.
# ---------------------------------------------------------------------------


def test_actual_snapshot_never_fabricates_liquidity_withdrawal_without_transfer_movement_or_confirmed_action() -> (
    None
):
    """Rebaseline §4.3: a negative operating result alone must never produce
    a Privilège withdrawal. Without any evidenced "Transferência
    patrimonial" transaction, the closing balance stays exactly the opening
    balance and the deficit is surfaced as `unexplained_operating_result`."""

    data = FinancialEngineInput(
        period="2026-09",
        operating_income=Decimal("1000"),
        operating_expenses=Decimal("4000"),
        opening_liquidity_balance=Decimal("20000"),
        investments=Decimal("0"),
        redemptions=Decimal("0"),
    )

    result = calculate_actual_snapshot(data)

    assert result.operating_result == Decimal("-3000.00")
    assert result.liquidity_used == Decimal("0.00")
    assert result.closing_liquidity_balance == Decimal("20000.00")
    assert result.has_transfer_evidence is False
    assert result.unexplained_operating_result == Decimal("-3000.00")


def test_actual_snapshot_never_fabricates_liquidity_deposit_without_transfer_movement_or_confirmed_action() -> (
    None
):
    """The mirror case (rebaseline §4.3): a positive operating result alone
    must never produce an automatic Privilège application."""

    data = FinancialEngineInput(
        period="2026-09",
        operating_income=Decimal("9000"),
        operating_expenses=Decimal("2000"),
        opening_liquidity_balance=Decimal("5000"),
    )

    result = calculate_actual_snapshot(data)

    assert result.operating_result == Decimal("7000.00")
    assert result.liquidity_deposit == Decimal("0.00")
    assert result.closing_liquidity_balance == Decimal("5000.00")
    assert result.has_transfer_evidence is False
    assert result.unexplained_operating_result == Decimal("7000.00")


def test_actual_snapshot_uses_evidenced_transfer_for_closing_balance_independent_of_operating_result() -> (
    None
):
    """When a real "Transferência patrimonial" movement is evidenced
    (`investments`/`redemptions`, populated from an imported/observed
    transaction or a confirmed action), it -- and only it -- drives the
    Privilège closing balance, regardless of whether the operating result is
    positive, negative or unrelated in size."""

    data = FinancialEngineInput(
        period="2026-09",
        operating_income=Decimal("1000"),
        operating_expenses=Decimal("9000"),  # large deficit, no bearing on liquidity here
        opening_liquidity_balance=Decimal("10000"),
        redemptions=Decimal("300"),
    )

    result = calculate_actual_snapshot(data)

    assert result.operating_result == Decimal("-8000.00")
    assert result.liquidity_used == Decimal("300.00")
    assert result.closing_liquidity_balance == Decimal("9700.00")
    assert result.has_transfer_evidence is True
    assert result.unexplained_operating_result == Decimal("0.00")


def test_reconcile_actual_liquidity_pays_down_legacy_debt_only_with_evidenced_deposit() -> None:
    """A carried `opening_uncovered_deficit` (from a period closed before
    this reconciliation existed) can only shrink through a real evidenced
    deposit -- never through an inferred operating surplus, however large."""

    no_evidence = reconcile_actual_liquidity(
        opening_balance=Decimal("0"),
        opening_uncovered_deficit=Decimal("1000"),
        operating_result=Decimal("50000"),
    )
    assert no_evidence.closing_uncovered_deficit == Decimal("1000.00")
    assert no_evidence.closing_balance == Decimal("0.00")

    partially_paid = reconcile_actual_liquidity(
        opening_balance=Decimal("0"),
        opening_uncovered_deficit=Decimal("1000"),
        evidenced_deposit=Decimal("400"),
        operating_result=Decimal("50000"),
    )
    assert partially_paid.closing_uncovered_deficit == Decimal("600.00")
    assert partially_paid.closing_balance == Decimal("0.00")

    fully_paid_with_remainder = reconcile_actual_liquidity(
        opening_balance=Decimal("0"),
        opening_uncovered_deficit=Decimal("1000"),
        evidenced_deposit=Decimal("1500"),
    )
    assert fully_paid_with_remainder.closing_uncovered_deficit == Decimal("0.00")
    assert fully_paid_with_remainder.closing_balance == Decimal("500.00")


def test_reconcile_actual_liquidity_surfaces_shortfall_when_evidenced_redemption_exceeds_available() -> (
    None
):
    """A real evidenced withdrawal larger than the opening balance plus any
    evidenced deposit is never silently clamped away: it is surfaced as an
    uncovered/unexplained shortfall for investigation."""

    reconciliation = reconcile_actual_liquidity(
        opening_balance=Decimal("100"),
        evidenced_used=Decimal("250"),
    )

    assert reconciliation.closing_balance == Decimal("0.00")
    assert reconciliation.closing_uncovered_deficit == Decimal("150.00")
    assert reconciliation.has_transfer_evidence is True
