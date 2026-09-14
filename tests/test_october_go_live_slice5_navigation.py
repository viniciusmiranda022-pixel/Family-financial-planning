"""Static-source regression for October Go-Live Slice 5 (navegação e UX
final -- `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_5.md`).

This project's CI has no JS test runner (`frontend-syntax` only runs
`node --check`), so -- matching the precedent set by
`tests/test_purchase_scenario_comparison_frontend.py` and
`tests/test_browser_due_notifications_frontend.py` -- this is the
deterministic, auditable way to prove:

1. `#main-nav` is exactly the target navigation from the Work Order, in
   order, and every legacy surface it absorbs (Lançar agora, Transferências,
   Importações, Lançamentos, Revisar, Rendas, Planejamento, Integridade) has
   no permanent button there anymore.
2. Every nav button and every `data-go` contextual link still resolves to a
   real `#view-*` section -- no orphaned capability, no broken route.
3. Entradas/Saídas never classify an internal transfer as income/expense
   (INV-001) and point the user at Transferências instead.
4. The Assistente Financeiro frontend only ever drives the Slice 4 typed-
   action contract through a server-issued `proposal_id` -- it can never
   send a `typed_action`/`payload` of its own, the confirm button only
   exists when the server already said `can_execute`, the possible-
   duplicate flow only ever offers the three human choices and never writes
   before one is chosen, and Undo is gated by the server's own
   `undoable`/`non_reversible_reason`.
5. "Outra entrada"/"Outra saída" template chips (`app.services.
   entry_type_templates`, migração 0017) only ever prefill the form -- they
   never submit on their own -- and an observation is only ever recorded
   after the transaction it references already exists (never before, never
   blocking/rolling back the entry itself).
"""

import html
import re
from pathlib import Path

TARGET_NAV = [
    ("dashboard", "Dashboard Financeiro"),
    ("entradas", "Entradas"),
    ("saidas", "Saídas"),
    ("payables", "Contas a pagar"),
    ("planning", "Gastos & Economia"),
    ("reports", "Relatórios"),
    ("advisor", "Assistente Financeiro"),
    ("users", "Acessos"),
    ("settings", "Configurações"),
]

# Legacy surfaces the Work Order's absorption table explicitly says must not
# keep a permanent #main-nav button (they stay routable via navigate()/
# data-go from their new home instead).
ABSORBED_VIEWS = ("capture", "transferencias", "imports", "transactions", "reviews", "integrity")


def _app_js() -> str:
    return Path("app/static/app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return Path("app/templates/index.html").read_text(encoding="utf-8")


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


def _main_nav_block() -> str:
    html = _index_html()
    match = re.search(r'<nav id="main-nav">(.*?)</nav>', html, re.S)
    assert match, "#main-nav not found in app/templates/index.html"
    return match.group(1)


def _section_block(view: str) -> str:
    html = _index_html()
    match = re.search(rf'<section id="view-{re.escape(view)}"[^>]*>(.*?)\n {{6}}</section>', html, re.S)
    assert match, f"#view-{view} section not found in app/templates/index.html"
    return match.group(1)


def test_main_nav_is_exactly_the_target_list_in_order() -> None:
    nav = _main_nav_block()
    buttons = re.findall(r'<button data-view="([a-zA-Z0-9_-]+)"[^>]*>(.*?)</button>', nav, re.S)
    views_in_order = [view for view, _label in buttons]
    assert views_in_order == [view for view, _label in TARGET_NAV], (
        "October Go-Live Slice 5's target navigation must be exactly "
        f"{[v for v, _ in TARGET_NAV]!r}, got {views_in_order!r}"
    )
    for view, expected_label in TARGET_NAV:
        _view, label_html = next(item for item in buttons if item[0] == view)
        # Strip the leading icon <span>...</span> (its content is the emoji
        # glyph, not part of the label) before comparing the visible text.
        label_text = re.sub(r"^<span>.*?</span>", "", label_html).strip()
        label_text = html.unescape(re.sub(r"<[^>]+>", "", label_text).strip())
        assert label_text == expected_label, (
            f"nav button for {view!r} must read {expected_label!r}, got {label_text!r}"
        )
    # A single visual separator between Assistente Financeiro and Acessos,
    # not another #main-nav button (docs: "separador").
    assert '<div class="nav-separator"' in nav


def test_absorbed_legacy_views_have_no_permanent_nav_button() -> None:
    nav = _main_nav_block()
    for view in ABSORBED_VIEWS:
        assert f'data-view="{view}"' not in nav, (
            f"{view!r} must not have a permanent #main-nav button anymore "
            "(October Go-Live Slice 5 absorption table)"
        )
    # "Rendas" is not merely un-navved -- it is fully merged into Entradas,
    # so its old standalone view must not exist in the DOM at all.
    assert 'id="view-income"' not in _index_html()


def test_every_nav_button_and_data_go_target_resolves_to_a_real_view() -> None:
    html = _index_html()
    nav_targets = set(re.findall(r'<button data-view="([a-zA-Z0-9_-]+)"', _main_nav_block()))
    go_targets = set(re.findall(r'data-go="([a-zA-Z0-9_-]+)"', html))
    existing_views = set(re.findall(r'<section id="view-([a-zA-Z0-9_-]+)"', html))
    missing = (nav_targets | go_targets) - existing_views
    assert not missing, f"navigate()/data-go targets with no matching #view-* section: {missing}"


def test_entradas_and_saidas_never_offer_transfer_and_point_to_transferencias() -> None:
    entradas = _section_block("entradas")
    saidas = _section_block("saidas")
    for view_name, block in (("entradas", entradas), ("saidas", saidas)):
        assert 'id="income-entry-form"' in block or 'id="expense-entry-form"' in block
        # INV-001: an internal transfer is never a movement_type option on
        # the income/expense structured forms themselves.
        assert "movement_type" not in block, (
            f"{view_name} must not offer a transfer/investment movement type directly "
            "(INV-001) -- it must point to Transferências instead"
        )
        assert 'data-go="transferencias"' in block, (
            f"{view_name} must keep a contextual link to Transferências (October Go-Live "
            "Slice 5 absorption table)"
        )


def test_payables_still_exposes_obligations_invoices_and_conferir_e_pagar() -> None:
    payables = _section_block("payables")
    assert 'id="payables-obligations-table"' in payables
    assert 'id="card-invoices-table"' in payables
    assert 'id="pay-invoice-form"' in payables
    assert "Conferir e pagar" in _app_js()


def test_payables_owns_obligation_cadastro_conferencia_e_pagamento() -> None:
    """docs/OCTOBER_GO_LIVE_REBASELINE.md §2: '**Contas a pagar** contém cadastro,
    conferência e pagamento de compromissos/faturas.' Obligation creation
    (`#obligation-form`) and deletion must live in #view-payables, not only
    a link out to Gastos & Economia -- and Gastos & Economia must not keep a
    second, redundant obligations table now that Payables owns the full
    lifecycle."""
    payables = _section_block("payables")
    planning = _section_block("planning")
    assert 'id="obligation-form"' in payables, "obligation cadastro (creation) must live in Contas a pagar"
    assert 'id="obligation-form"' not in planning, "obligation cadastro must not be duplicated in Gastos & Economia"
    assert "delete-obligation" in _app_js()
    assert '"#obligations-table"' not in _app_js(), (
        "the old, separate obligations table must be removed once Contas a pagar owns the "
        "full obligation lifecycle -- not kept as a second, divergent read surface"
    )


def test_imports_reachable_from_settings_and_ledger_from_reports() -> None:
    settings = _section_block("settings")
    reports = _section_block("reports")
    assert 'data-go="imports"' in settings, "Importações must be reachable from Configurações"
    assert 'data-go="transactions"' in reports, "Lançamentos/livro-razão must be reachable from Relatórios"


def test_reviews_has_no_permanent_nav_but_dashboard_kpi_links_to_it() -> None:
    dashboard = _section_block("dashboard")
    assert 'id="kpi-reviews"' in dashboard
    assert 'data-go="reviews"' in dashboard, (
        "Revisar must stay reachable from a Dashboard alert/KPI (October Go-Live "
        "Slice 5: 'sem navegação permanente')"
    )


def test_assistant_execute_never_sends_typed_action_or_payload() -> None:
    """`execute_typed_action` (`app/services/assistant_actions.py`) accepts
    only a server-issued `proposal_id` -- the client must never be able to
    resupply `typed_action`/`payload`/`path_params`/an account or category
    id directly (engineering review of PR #92/#93)."""
    source = _app_js()
    body = _function_body(source, "executeAssistantProposal")
    assert '"/assistant/execute"' in body
    assert "proposal_id" in body
    assert "disambiguation_qa" in body
    for forbidden in ("typed_action", "payload:", "path_params", "account_id:", "category_id:"):
        assert forbidden not in body, (
            f"executeAssistantProposal() must never serialize {forbidden!r} -- only "
            "proposal_id/disambiguation_qa (Slice 4 contract)"
        )


def test_assistant_interpret_request_only_ever_sends_the_known_contract_fields() -> None:
    source = _app_js()
    send_body = _function_body(source, "sendAssistantMessage")
    resolve_body = _function_body(source, "resolveAssistantDuplicate")
    for body in (send_body, resolve_body):
        assert '"/assistant/interpret"' in body
        for forbidden in ("typed_action", "payload:", "amount:", "account_id:"):
            assert forbidden not in body


def test_assistant_confirm_button_only_created_when_proposal_can_execute() -> None:
    source = _app_js()
    handler_body = _function_body(source, "handleAssistantInterpretResult")
    # renderAssistantProposal() is the only place a "Confirmar e registrar"
    # button is wired to executeAssistantProposal() -- assert it is reached
    # exclusively through the `proposal.can_execute` branch.
    match = re.search(r"if\s*\(proposal\.can_execute\)\s*\{\s*renderAssistantProposal\(", handler_body)
    assert match, (
        "renderAssistantProposal() (which creates the Confirmar/execute button) must "
        "only be called when proposal.can_execute is true"
    )
    proposal_render_body = _function_body(source, "renderAssistantProposal")
    assert "executeAssistantProposal(proposalId" in proposal_render_body


def test_assistant_possible_duplicate_offers_exactly_three_choices_and_never_writes() -> None:
    source = _app_js()
    duplicate_body = _function_body(source, "renderAssistantDuplicateChoice")
    for decision in ('"skip"', '"import_anyway"', '"view_existing"'):
        assert decision in duplicate_body, f"missing duplicate-resolution choice {decision}"
    # Clicking any of the three choices only ever calls interpret again
    # (through resolveAssistantDuplicate), never execute -- the possible
    # duplicate is not resolved into a write until the server returns a
    # fresh, explicit can_execute proposal.
    assert "resolveAssistantDuplicate(" in duplicate_body
    assert "executeAssistantProposal(" not in duplicate_body
    resolve_body = _function_body(source, "resolveAssistantDuplicate")
    assert '"/assistant/interpret"' in resolve_body
    assert '"/assistant/execute"' not in resolve_body


def test_assistant_undo_is_gated_by_the_servers_own_undoable_flag() -> None:
    source = _app_js()
    result_body = _function_body(source, "renderAssistantExecutionResult")
    assert "action.undoable" in result_body
    assert "action.non_reversible_reason" in result_body
    # The Undo button must only be constructed inside the undoable branch,
    # never unconditionally.
    assert re.search(r"if\s*\(action\.undoable\)\s*\{[^}]*Desfazer", result_body, re.S), (
        "Undo control must be created only when action.undoable is true"
    )


def test_assistant_intent_fallback_uses_existing_advisor_chat_not_a_new_engine() -> None:
    """A message that is not a recognized typed-action intent (a plain
    question) must fall back to the pre-existing read-only `/advisor/chat`
    consultant, never a second, ad-hoc answer engine in the frontend."""
    source = _app_js()
    handler_body = _function_body(source, "handleAssistantInterpretResult")
    assert "answerAdvisorQuestion(originalMessage, bubble)" in handler_body
    assert '"/advisor/chat"' not in handler_body  # delegated, not duplicated inline


def test_entry_type_template_chips_only_prefill_never_auto_submit() -> None:
    source = _app_js()
    body = _function_body(source, "renderTypeTemplateChips")
    assert "/entry-type-templates?movement_type=" in body
    assert "active_only=true" in body
    click_handler = re.search(r"addEventListener\(\"click\", \(\) => \{(.*?)\}\)\);", body, re.S)
    assert click_handler, "type-template-chip click handler not found"
    handler_source = click_handler.group(1)
    # Prefilling only -- never a submit()/requestSubmit() call and never a
    # POST of its own from inside the click handler.
    assert "submit(" not in handler_source
    assert 'api(' not in handler_source
    assert "form.elements.description.value" in handler_source or "form.elements.description" in handler_source


def test_privilege_settings_copy_never_teaches_synthetic_deficit_surplus_rule() -> None:
    """Engineering review of PR #93 (2026-09-14): `#view-settings` explained
    Privilège as "recebe a sobra do mês e cobre o déficit quando salário e
    outras receitas não bastam" -- teaching the exact forbidden rule from
    `docs/OCTOBER_GO_LIVE_REBASELINE.md` §4.3
    (`resultado_operacional < 0 -> fabricar liquidity_withdrawal`, and its
    surplus mirror). Slice 5 is the final UX pass; visible copy must match
    the normative semantics (§4.2/§4.3), not the legacy one."""
    card = re.search(
        r"<strong>Como o Privilège DI funciona no sistema</strong>(.*?)</div>",
        _index_html(),
        re.S,
    )
    assert card, "Privilège explanation card not found in #view-settings"
    copy = card.group(1)
    forbidden_phrases = (
        "recebe a sobra do mês",
        "cobre o déficit",
        "déficit superar o saldo",
    )
    for phrase in forbidden_phrases:
        assert phrase not in copy, (
            f"Privilège settings copy must not teach the forbidden rebaseline §4.3 rule "
            f"(resultado operacional -> aplicação/resgate sintético): found {phrase!r}"
        )
    for required_phrase in ("movimento bancário observado", "não é prova de resgate"):
        assert required_phrase in copy, (
            f"Privilège settings copy must state the normative §4.2/§4.3 rule explicitly: "
            f"missing {required_phrase!r}"
        )


def test_payables_shows_realizado_comprometido_previsto_without_mixing() -> None:
    """Engineering review of PR #93 (2026-09-14), blocker #2: Contas a pagar
    must surface the already-canonical REALIZADO/COMPROMETIDO/PREVISTO facts
    the backend already computes (`app.services.financial_state`, reused by
    `GET /obligations`'s `financial_state` and
    `card_invoice_lifecycle.serialize_card_invoice`'s `financial_state`) --
    not just due-date urgency (`alert_label`) or cycle status (`status`) --
    and never invent a second, frontend-derived state or merge the three
    labels together (Work Order #5 test 5 / rebaseline §23)."""
    source = _app_js()
    chip_body = _function_body(source, "financialStateChip")
    assert "FINANCIAL_STATE_LABELS" in chip_body
    labels_match = re.search(r"const FINANCIAL_STATE_LABELS\s*=\s*\{([^}]*)\}", source)
    assert labels_match, "FINANCIAL_STATE_LABELS constant not found in app/static/app.js"
    for state in ("REALIZADO", "COMPROMETIDO", "PREVISTO"):
        assert state in labels_match.group(1), f"FINANCIAL_STATE_LABELS must map the canonical {state!r} label"

    obligation_row = _function_body(source, "payablesObligationRow")
    assert "financialStateChip(item.financial_state)" in obligation_row, (
        "payablesObligationRow() must render the backend's own financial_state, not just "
        "alert_label/status"
    )

    invoices_body = _function_body(source, "loadCardInvoices")
    assert "financialStateChip(item.financial_state)" in invoices_body, (
        "the card-invoices table must render the backend's own financial_state, not just cycle "
        "status"
    )

    # Distinct CSS -- proof the three states are styled (and therefore
    # rendered) as three different things, never collapsed into one.
    css = Path("app/static/styles.css").read_text(encoding="utf-8")
    for suffix in ("realizado", "comprometido", "previsto"):
        assert f"financial-state-{suffix}" in css, f"missing distinct styling for financial-state-{suffix}"


def test_entry_type_observation_is_gated_by_explicit_outra_entrada_saida_opt_in() -> None:
    """Engineering review of PR #93 (2026-09-14), blocker #3:
    `recordEntryTypeObservation()` must not fire for every regular entry
    (e.g. "Supermercado", "Combustível posto X") -- only when the user
    explicitly opted into the "Outra entrada"/"Outra saída" reusable-type
    flow via an unchecked-by-default checkbox that carries no `name` (so it
    can never be serialized into the `POST /transactions` payload). Tipo,
    categoria e descrição continuam conceitos diferentes (rebaseline §8.2/§9)
    -- toda descrição comum não pode virar candidato a tipo."""
    html = _index_html()
    for checkbox_id in ("income-save-as-type", "expense-save-as-type"):
        tag_match = re.search(rf'<input type="checkbox" id="{checkbox_id}"[^>]*>', html)
        assert tag_match, f"checkbox #{checkbox_id} not found in app/templates/index.html"
        assert "name=" not in tag_match.group(0), (
            f"#{checkbox_id} must have no name attribute -- it is UI-only and must never be "
            "serialized into POST /transactions by formJson()/FormData"
        )

    source = _app_js()
    income_handler = re.search(
        r'document\.querySelector\("#income-entry-form"\)\.addEventListener\("submit".*?\n\}\);',
        source,
        re.S,
    )
    expense_handler = re.search(
        r'document\.querySelector\("#expense-entry-form"\)\.addEventListener\("submit".*?\n\}\);',
        source,
        re.S,
    )
    assert income_handler and expense_handler
    for name, handler, checkbox_id in (
        ("income", income_handler.group(0), "income-save-as-type"),
        ("expense", expense_handler.group(0), "expense-save-as-type"),
    ):
        guard = re.search(
            rf'if\s*\(document\.querySelector\("#{re.escape(checkbox_id)}"\)\?\.checked\)\s*\{{\s*'
            r"(?:await )?recordEntryTypeObservation\(",
            handler,
        )
        assert guard, (
            f"{name}-entry-form must only call recordEntryTypeObservation() when "
            f"#{checkbox_id} is explicitly checked -- a regular entry must never become "
            "template-learning evidence on its own"
        )


def test_type_template_chip_click_checks_the_explicit_save_as_type_box() -> None:
    """Reusing an already-learned type via a chip is itself the user's
    explicit confirmation that this label applies again -- clicking one
    checks the same opt-in box the free-text flow uses, so the
    reinforcement observation still fires without a second, parallel
    learning path."""
    chips_body = _function_body(_app_js(), "renderTypeTemplateChips")
    assert "saveAsTypeSelector" in chips_body
    assert "saveAsType.checked = true" in chips_body


def test_entry_type_observation_is_recorded_only_after_the_transaction_exists() -> None:
    source = _app_js()
    # Non-greedy up to the handler's own top-level closing `});` (column 0)
    # -- not the first inner `});` the handler body happens to contain
    # (e.g. `JSON.stringify(payload) });`).
    income_handler = re.search(
        r'document\.querySelector\("#income-entry-form"\)\.addEventListener\("submit".*?\n\}\);',
        source,
        re.S,
    )
    expense_handler = re.search(
        r'document\.querySelector\("#expense-entry-form"\)\.addEventListener\("submit".*?\n\}\);',
        source,
        re.S,
    )
    assert income_handler and expense_handler
    for name, handler in (("income", income_handler), ("expense", expense_handler)):
        handler_source = handler.group(0)
        create_index = handler_source.index('api("/transactions"')
        observe_index = handler_source.index("recordEntryTypeObservation(")
        assert observe_index > create_index, (
            f"{name}-entry-form must call recordEntryTypeObservation() only after "
            "POST /transactions already returned (the entry is a fact first, the "
            "learning signal second, and best-effort)"
        )
        # The transaction id used is the one the backend just returned, not
        # a client-invented id.
        assert "created.id" in handler_source

    record_body = _function_body(source, "recordEntryTypeObservation")
    assert '"/entry-type-templates"' in record_body
    assert "transaction_id" in record_body
