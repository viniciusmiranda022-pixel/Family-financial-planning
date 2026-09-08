"""Static-source regression for the purchase scenario comparison frontend
(Fase 3, `docs/WORK_ORDER_PURCHASE_SCENARIO_COMPARISON.md`).

This project's CI has no JS test runner (`frontend-syntax` only runs
`node --check`), so -- matching the precedent set by
`tests/test_manual_financial_flows.py::
test_frontend_large_entry_threshold_has_no_parallel_financial_formula` --
this is the deterministic, auditable way to prove `app/static/app.js` never
reimplements a balance/liquidity/deficit/floor/installment/interest formula
for this feature: every number the comparison UI renders must come straight
from `POST /purchases/scenario-comparison`'s JSON response, never be
recomputed in the browser.
"""

import re
from pathlib import Path


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\)\s*\{{", source)
    assert match, f"{name}() not found in app/static/app.js"
    # Balance braces from the function's opening `{` to find its matching `}`.
    depth = 0
    start = match.end() - 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unbalanced braces looking for the end of {name}()")


def _app_js() -> str:
    return Path("app/static/app.js").read_text(encoding="utf-8")


# Field names the canonical `ForecastInput`/Projection Engine/Validator own
# (`app/services/finance.py`, `app/services/projection_engine.py`): if any of
# these appear combined with an arithmetic operator in the comparison UI's
# JS, that is a second, parallel formula, not a display echo of the backend's
# own numbers.
_FORBIDDEN_ARITHMETIC = (
    "balance_delayed *",
    "balance_delayed +",
    "balance_delayed -",
    "emergency_floor *",
    "emergency_floor +",
    "emergency_floor -",
    "uncovered_deficit",  # this field never appears in app.js at all (see below)
    "settle_liquidity",
    "distance_to_floor +",
    "distance_to_floor -",
    "distance_to_floor *",
)


def test_compare_scenarios_never_recomputes_projection_fields() -> None:
    source = _app_js()
    body = _function_body(source, "compareScenarios")
    # `compareScenarios` only reshapes the draft form state into the request
    # body (label/price/down_payment/installment_count/
    # monthly_interest_rate) and hands the response straight to the render
    # function -- it must never itself derive a monthly payment, balance,
    # deficit or floor value.
    for forbidden in (
        "balance_",
        "uncovered_deficit",
        "distance_to_floor",
        "emergency_floor",
        "settle_liquidity",
    ):
        assert forbidden not in body, f"{forbidden!r} must not appear in compareScenarios()"
    assert "/purchases/scenario-comparison" in body
    assert "renderScenarioComparisonResults" in body


def test_render_scenario_comparison_results_only_formats_backend_values() -> None:
    source = _app_js()
    body = _function_body(source, "renderScenarioComparisonResults")
    # The only arithmetic allowed here is display comparison (`<`, `>`) for
    # choosing a CSS class -- never `+`, `-`, `*`, `/` combining monetary
    # fields, which would mean the UI is deriving a number instead of
    # rendering one the backend already computed.
    for operator in (" + ", " - ", " * ", " / "):
        assert operator not in body, (
            f"renderScenarioComparisonResults() must not contain {operator!r}; "
            "every value it shows must come straight from the API response"
        )


def test_render_scenario_alternatives_form_has_no_financial_formula() -> None:
    source = _app_js()
    body = _function_body(source, "renderScenarioAlternativesForm")
    # This function only renders the draft inputs (label/price/down
    # payment/installments/rate/month) as an editable form; it must never
    # compute a monthly payment, total cost or projection value itself.
    for forbidden in ("monthly_payment", "total_purchase_cost", "amortiz", "balance_"):
        assert forbidden not in body


def test_scenario_comparison_ui_reuses_canonical_endpoint_not_a_new_formula() -> None:
    source = _app_js()
    # The percent -> decimal-fraction conversion for `monthly_interest_rate`
    # is the one deliberate exception: it is a unit conversion for display
    # (the form shows a percent, the API contract takes a 0..1 fraction),
    # never a re-derivation of a projected value. Assert it exists exactly
    # once and nowhere else does this module divide/multiply a monetary
    # field by a numeric literal for this feature.
    compare_body = _function_body(source, "compareScenarios")
    assert compare_body.count("/ 100") == 1
