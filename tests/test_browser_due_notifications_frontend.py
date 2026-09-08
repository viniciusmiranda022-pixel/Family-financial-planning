"""Static-source regression for the browser due-date notifications frontend
(Fase 3, `docs/WORK_ORDER_BROWSER_DUE_NOTIFICATIONS.md`).

This project's CI has no JS test runner (`frontend-syntax` only runs
`node --check`), so -- matching the precedent set by
`tests/test_purchase_scenario_comparison_frontend.py` and
`tests/test_manual_financial_flows.py::
test_frontend_large_entry_threshold_has_no_parallel_financial_formula` --
this is the deterministic, auditable way to prove `app/static/app.js` never
reimplements a due-date/recurrence/installment/amount calculation for this
feature, that requesting browser permission is always gated behind an
explicit human click, and that the feature degrades to an always-available
internal panel instead of failing when notifications are denied/unsupported/
throw.
"""

import re
from pathlib import Path


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\)\s*\{{", source)
    assert match, f"{name}() not found in app/static/app.js"
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


def _index_html() -> str:
    return Path("app/templates/index.html").read_text(encoding="utf-8")


def test_checking_due_notifications_only_reads_the_canonical_obligations_endpoint() -> None:
    source = _app_js()
    body = _function_body(source, "checkDueNotifications")
    assert '"/obligations"' in body or "'/obligations'" in body
    # Never the card-invoice ("Contas a pagar") endpoint: that surface
    # deliberately has no genuine due date
    # (`app/services/card_payment_reconciliation.py::CardInvoiceObligation`
    # docstring -- no parser stores the invoice's printed "VENCIMENTO" date),
    # so using it here would mean either fabricating a due date or silently
    # falling back to some other field as if it were one.
    assert "card-payment-reconciliations" not in body


def test_check_due_notifications_has_no_parallel_date_or_amount_arithmetic() -> None:
    source = _app_js()
    body = _function_body(source, "checkDueNotifications")
    # The only fields this function is allowed to branch on are the
    # categorical, already-computed `alert_level` values -- never a
    # recomputed day count, date difference or monetary formula.
    for forbidden in ("days_until_due", "next_due_date +", "next_due_date -", "amount *", "amount +", "amount -"):
        assert forbidden not in body


def test_due_alert_key_never_recomputes_the_alert_level() -> None:
    source = _app_js()
    body = _function_body(source, "dueAlertKey")
    # The dedup key is built purely from fields `GET /obligations` already
    # returns (`id`, `next_due_date`, `alert_level`); it must never contain
    # its own day-count/date-window logic.
    for forbidden in (" < ", " > ", " <= ", " >= ", "Date(", "getTime"):
        assert forbidden not in body
    assert "item.id" in body
    assert "item.next_due_date" in body
    assert "item.alert_level" in body


def test_enable_due_notifications_is_never_called_automatically() -> None:
    source = _app_js()
    # Permission may only be requested from an explicit human click
    # (Work Order item 3: "A UI pode solicitar permissão ao navegador
    # somente por ação humana explícita"). `initDueNotifications` (wired to
    # run automatically after login in `showApp()`) must never call
    # `enableDueNotifications`/`Notification.requestPermission` itself --
    # only `checkDueNotifications` (a read, gated on already-granted
    # permission) and the settings render.
    init_body = _function_body(source, "initDueNotifications")
    assert "enableDueNotifications" not in init_body
    assert "requestPermission" not in init_body
    # The only call site for requesting permission is inside
    # `enableDueNotifications` itself, and it must be wired to a button
    # click, never invoked from bootstrap/showApp.
    request_permission_body = _function_body(source, "enableDueNotifications")
    assert "requestPermission" in request_permission_body
    bootstrap_body = _function_body(source, "bootstrap")
    show_app_body = _function_body(source, "showApp")
    assert "enableDueNotifications" not in bootstrap_body
    assert "enableDueNotifications" not in show_app_body
    assert (
        'document.querySelector("#due-notifications-enable").addEventListener("click", enableDueNotifications)'
        in source
    )


def test_fire_due_notification_degrades_safely_instead_of_throwing() -> None:
    source = _app_js()
    body = _function_body(source, "fireDueNotification")
    assert "try {" in body and "catch" in body
    assert "new Notification(" in body


def test_check_due_notifications_guards_every_denied_or_unsupported_state() -> None:
    source = _app_js()
    body = _function_body(source, "checkDueNotifications")
    assert "dueNotificationsSupported()" in body
    assert 'Notification.permission !== "granted"' in body
    assert "dueNotificationsEnabled()" in body
    # A failed background fetch (e.g. session expired) must not propagate as
    # an uncaught error -- the periodic poll is best-effort.
    assert "try { items = await api" in body


def test_notification_settings_ui_covers_unsupported_and_denied_states() -> None:
    source = _app_js()
    body = _function_body(source, "renderDueNotificationsSettings")
    assert "dueNotificationsSupported()" in body
    assert '"denied"' in body


def test_disable_never_touches_a_financial_entity() -> None:
    source = _app_js()
    body = _function_body(source, "disableDueNotifications")
    # Turning the experience off is purely a `localStorage` write plus a UI
    # re-render -- never an API call, so it can never alter a financial
    # fact (Work Order acceptance criterion: enable/disable without
    # altering financial facts).
    assert "api(" not in body
    assert "setDueNotificationsEnabled" in body


def test_index_html_wires_the_bell_and_the_settings_toggle() -> None:
    html = _index_html()
    for element_id in (
        "due-notifications-toggle",
        "due-notifications-panel",
        "due-notifications-list",
        "due-notifications-count",
        "due-notifications-status",
        "due-notifications-enable",
        "due-notifications-disable",
    ):
        assert f'id="{element_id}"' in html, f"#{element_id} missing from index.html"
