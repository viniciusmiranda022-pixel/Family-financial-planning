"""First deterministic anomaly layer based on robust, reconciled baselines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from statistics import median


@dataclass(frozen=True, slots=True)
class HistoricalAmount:
    period: str
    amount: Decimal
    reconciled: bool


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    check_id: str
    status: str
    message: str
    baseline_months: int
    median: Decimal | None = None
    median_absolute_deviation: Decimal | None = None
    threshold: Decimal | None = None
    actual: Decimal | None = None
    reason: str | None = None


def evaluate_amount_anomaly(
    current_amount: Decimal,
    history: Sequence[HistoricalAmount],
    *,
    check_id: str = "ANOM-001",
) -> AnomalyResult:
    """Flag an amount above median + max(3*MAD, 50% of median).

    Only explicitly reconciled months enter the baseline. Three distinct months are
    required; an insufficient sample returns unknown rather than an anomaly.
    """

    valid = tuple(item for item in history if item.reconciled)
    periods = {item.period for item in valid}
    if len(periods) < 3:
        return AnomalyResult(
            check_id=check_id,
            status="unknown",
            message="Amostra reconciliada insuficiente para avaliar anomalia.",
            baseline_months=len(periods),
            actual=_money(abs(current_amount)),
            reason="insufficient_reconciled_baseline",
        )
    values = tuple(_money(abs(item.amount)) for item in valid)
    center = _money(Decimal(median(values)))
    deviations = tuple(abs(value - center) for value in values)
    mad = _money(Decimal(median(deviations)))
    threshold = _money(center + max(Decimal("3") * mad, center * Decimal("0.50")))
    actual = _money(abs(current_amount))
    warning = actual > threshold
    return AnomalyResult(
        check_id=check_id,
        status="warning" if warning else "pass",
        message=(
            "Valor acima da baseline robusta dos meses reconciliados."
            if warning
            else "Valor dentro da baseline robusta dos meses reconciliados."
        ),
        baseline_months=len(periods),
        median=center,
        median_absolute_deviation=mad,
        threshold=threshold,
        actual=actual,
    )


def evaluate_category_total_anomaly(
    current_total: Decimal,
    monthly_totals: Sequence[HistoricalAmount],
) -> AnomalyResult:
    return evaluate_amount_anomaly(
        current_total,
        monthly_totals,
        check_id="ANOM-002",
    )


def evaluate_signature_increase(
    current_amount: Decimal,
    historical_signature_amounts: Sequence[HistoricalAmount],
) -> AnomalyResult:
    return evaluate_amount_anomaly(
        current_amount,
        historical_signature_amounts,
        check_id="ANOM-003",
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
