from datetime import date
from decimal import Decimal

from app.services.finance import (
    ForecastCommission,
    ForecastInput,
    build_forecast,
    commission_net,
    monthly_net_rate,
)
from app.services.projection_validator import validate_projection


def test_commission_tax_is_rounded_per_receivable() -> None:
    tax, net = commission_net(Decimal("1234.56"), Decimal("0.06"))
    assert tax == Decimal("74.07")
    assert net == Decimal("1160.49")


def test_commissions_keep_their_calendar_year() -> None:
    forecast = build_forecast(
        ForecastInput(
            start_month=date(2026, 10, 1),
            end_month=date(2027, 5, 1),
            starting_balance=Decimal("0"),
            monthly_salary=Decimal("0"),
            monthly_cash_cap=Decimal("0"),
            monthly_investment_rate=Decimal("0"),
            obligations={},
            installments={},
            payroll_extras={},
            commissions=(
                ForecastCommission(date(2026, 10, 1), Decimal("1000"), 60),
                ForecastCommission(date(2027, 1, 1), Decimal("2000"), 60),
                ForecastCommission(date(2027, 3, 1), Decimal("3000"), 60),
            ),
        )
    )
    by_month = {row["month"]: row for row in forecast}
    assert by_month["2026-10"]["commission_no_commission"] == Decimal("0.00")
    assert by_month["2026-10"]["commission_expected"] == Decimal("1000.00")
    assert by_month["2026-10"]["commission_delayed"] == Decimal("0.00")
    assert by_month["2026-12"]["commission_delayed"] == Decimal("1000.00")
    assert by_month["2027-03"]["commission_delayed"] == Decimal("2000.00")
    assert by_month["2027-05"]["commission_delayed"] == Decimal("3000.00")
    assert by_month["2026-12"]["commission_delayed"] != Decimal("6000.00")


def test_payroll_extra_is_not_repeated_monthly() -> None:
    forecast = build_forecast(
        ForecastInput(
            start_month=date(2026, 11, 1),
            end_month=date(2026, 12, 1),
            starting_balance=Decimal("0"),
            monthly_salary=Decimal("5000"),
            monthly_cash_cap=Decimal("0"),
            monthly_investment_rate=Decimal("0"),
            obligations={},
            installments={},
            payroll_extras={"2026-11": Decimal("2500"), "2026-12": Decimal("1800")},
            commissions=(),
        )
    )
    assert forecast[0]["payroll_extras"] == Decimal("2500.00")
    assert forecast[1]["payroll_extras"] == Decimal("1800.00")
    assert forecast[1]["balance_delayed"] == Decimal("14300.00")


def test_privilege_rate_is_conservative_after_tax() -> None:
    rate = monthly_net_rate(Decimal("0.1402265"), Decimal("0.225"))
    assert Decimal("0.0085") < rate < Decimal("0.0086")


def test_projection_carries_uncovered_deficit_without_negative_balance() -> None:
    data = ForecastInput(
        start_month=date(2026, 9, 1),
        end_month=date(2026, 9, 1),
        starting_balance=Decimal("87068.54"),
        monthly_salary=Decimal("0"),
        monthly_cash_cap=Decimal("270014.03"),
        monthly_investment_rate=Decimal("0"),
        obligations={},
        installments={},
        payroll_extras={},
        commissions=(),
        safety_floor=Decimal("10000"),
    )

    (september,) = build_forecast(data)

    assert september["result_delayed"] == Decimal("-270014.03")
    assert september["investment_return_delayed"] == Decimal("0.00")
    assert september["balance_delayed"] == Decimal("0.00")
    assert september["uncovered_deficit_delayed"] == Decimal("182945.49")
    assert validate_projection(data, [september]).valid


def test_projection_pays_prior_debt_before_rebuilding_liquidity() -> None:
    data = ForecastInput(
        start_month=date(2026, 9, 1),
        end_month=date(2026, 10, 1),
        starting_balance=Decimal("100"),
        starting_uncovered_deficit=Decimal("0"),
        monthly_salary=Decimal("0"),
        monthly_cash_cap=Decimal("200"),
        monthly_investment_rate=Decimal("0.10"),
        obligations={},
        installments={},
        payroll_extras={"2026-10": Decimal("350")},
        commissions=(),
    )

    september, october = build_forecast(data)

    assert september["balance_expected"] == Decimal("0.00")
    assert september["uncovered_deficit_expected"] == Decimal("90.00")
    assert october["investment_return_expected"] == Decimal("0.00")
    assert october["balance_expected"] == Decimal("60.00")
    assert october["uncovered_deficit_expected"] == Decimal("0.00")


def test_projection_validator_detects_only_differences_above_tolerance() -> None:
    data = ForecastInput(
        start_month=date(2026, 9, 1),
        end_month=date(2026, 9, 1),
        starting_balance=Decimal("100"),
        monthly_salary=Decimal("1000"),
        monthly_cash_cap=Decimal("200"),
        monthly_investment_rate=Decimal("0.01"),
        obligations={},
        installments={},
        payroll_extras={},
        commissions=(),
    )
    rows = build_forecast(data)
    rows[0]["balance_expected"] += Decimal("0.01")
    assert validate_projection(data, rows).valid

    rows[0]["balance_expected"] += Decimal("0.01")
    validation = validate_projection(data, rows)
    assert not validation.valid
    assert validation.mismatches[0].key == "2026-09.balance_expected"
    assert validation.mismatches[0].difference == Decimal("0.02")
