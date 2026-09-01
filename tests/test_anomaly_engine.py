from decimal import Decimal

from app.services.anomaly_engine import HistoricalAmount, evaluate_amount_anomaly


def test_anomaly_requires_three_distinct_reconciled_months() -> None:
    result = evaluate_amount_anomaly(
        Decimal("999"),
        [
            HistoricalAmount("2026-05", Decimal("100"), True),
            HistoricalAmount("2026-06", Decimal("100"), True),
            HistoricalAmount("2026-07", Decimal("100"), False),
        ],
    )

    assert result.status == "unknown"
    assert result.baseline_months == 2
    assert result.reason == "insufficient_reconciled_baseline"


def test_anomaly_baseline_ignores_unreconciled_outlier_and_uses_median_mad() -> None:
    history = [
        HistoricalAmount("2026-04", Decimal("100"), True),
        HistoricalAmount("2026-05", Decimal("110"), True),
        HistoricalAmount("2026-06", Decimal("90"), True),
        HistoricalAmount("2026-07", Decimal("10000"), False),
    ]

    warning = evaluate_amount_anomaly(Decimal("200"), history)
    normal = evaluate_amount_anomaly(Decimal("140"), history)

    assert warning.status == "warning"
    assert warning.median == Decimal("100.00")
    assert warning.median_absolute_deviation == Decimal("10.00")
    assert warning.threshold == Decimal("150.00")
    assert normal.status == "pass"
