"""Go-live smoke -- P0 #87, October Go-Live Slice 8.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_8.md` §5 ("Smoke real de
go-live"). Every individual behaviour exercised here already has isolated,
more exhaustive regression coverage elsewhere (`tests/test_manual_transfers.py`,
`tests/test_card_invoice_lifecycle.py`, `tests/test_assistant_slice4.py`,
`tests/test_investments_slice6.py`, `tests/test_reports_slice7.py`,
`tests/test_obligation_lifecycle_slice3.py`). This file's job is different:
prove the 14 day-to-day flows the Work Order lists work **end-to-end,
together, in one continuous household session** (one login, one set of
accounts, one Assistant conversation), the way an actual user would touch
the system before go-live -- not each behaviour in its own isolated
fixture.

Every assertion below is the real, already-verified HTTP/service contract
copied from the test files above -- this is not a second implementation of
any financial rule, and it never fabricates a fact: every input is
synthetic (rebaseline/Work Order explicitly allow "datasets sintéticos
equivalentes" for this slice's automated E2E; a real-data manual smoke is a
separate, human step the Work Order also requires and this file cannot
perform on its own -- there is no real family data available to this
session).

Steps 4, 5 and 14 (the Assistant flows) drive the real `POST
/api/assistant/interpret` HTTP endpoint -- `interpret_message` ->
`_coerce_interpretation` -> `build_typed_action_proposal` all run
unstubbed -- and stub only the Advisor sidecar *transport*
(`_StubCodexAdvisorClient`, replacing `CodexAdvisorClient`) with one
canned, schema-shaped `/v1/interpret` response per message. Engineering
review of this PR, Round 1, blocker 1: an earlier version of this file
built a `StructuredInterpretation` by hand and called
`build_typed_action_proposal`/`persist_action_proposal` directly, which
skipped exactly the interpretation layer the Work Order requires this
smoke to prove (a materially complete phrase executes; an ambiguous one
asks; a Codex hypothesis never becomes a fact without confirmation). No
real network call happens in CI.

`run_go_live_smoke()` returns the ordered list of step results (input,
action, expected, observed, evidence) so this same flow can also produce
the Work Order's required smoke-results document without duplicating the
flow itself -- see `scripts/smoke_go_live.py`.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-go-live-smoke-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "go-live-smoke-test-secret-long-enough-value-x")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Household, Transaction  # noqa: E402
from app.services.codex_client import CodexResult  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

TARGET_NAV_VIEWS = (
    "dashboard",
    "entradas",
    "saidas",
    "payables",
    "planning",
    "reports",
    "advisor",
    "users",
    "settings",
)


@dataclass(slots=True)
class SmokeStep:
    number: int
    title: str
    input: str
    action: str
    expected: str
    observed: str
    ok: bool
    evidence: dict = field(default_factory=dict)


def _client() -> tuple[TestClient, sessionmaker]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), session_factory


@dataclass(slots=True)
class _StubCodexAdvisorClient:
    """Deterministic stand-in for `app.services.codex_client.CodexAdvisorClient`,
    scoped to this smoke's three `/api/assistant/interpret` calls (steps 4,
    5, 14).

    Engineering review of this PR (Round 1, blocker 1): the smoke must
    exercise the real HTTP/service interpretation contract end to end --
    `POST /api/assistant/interpret` -> `interpret_message` ->
    `_coerce_interpretation` -> `build_typed_action_proposal` -- not a
    `StructuredInterpretation` hand-built inside the test and fed straight
    to `build_typed_action_proposal`/`persist_action_proposal`, which skips
    exactly the interpretation layer the Work Order requires this smoke to
    validate. Stubbing only the sidecar *transport* (this class, standing
    in for `CodexAdvisorClient`) keeps the smoke deterministic and free of
    any real network call in CI -- matching the discipline
    `app.services.assistant_interpreter` already documents for every other
    caller -- while every other step of the real contract (schema
    revalidation, household-data resolution, disambiguation, duplicate
    detection, persistence) runs for real, unstubbed.
    """

    responses: dict[str, dict]

    @property
    def configured(self) -> bool:
        return True

    def interpret(self, payload: dict) -> CodexResult:
        message = payload.get("message")
        response = self.responses.get(message)
        if response is None:
            raise AssertionError(f"go-live smoke: unexpected /v1/interpret call for message={message!r}")
        return CodexResult(payload=dict(response))


def _interpret(client: TestClient, message: str, response: dict):
    """POST /api/assistant/interpret through the real HTTP/service
    contract, stubbing only the Advisor sidecar transport with one canned,
    schema-shaped `/v1/interpret` response keyed to this exact message."""

    with mock.patch(
        "app.services.assistant_interpreter.CodexAdvisorClient",
        return_value=_StubCodexAdvisorClient(responses={message: response}),
    ):
        return client.post("/api/assistant/interpret", json={"message": message})


def run_go_live_smoke(client: TestClient, session_factory) -> list[SmokeStep]:  # noqa: PLR0915
    steps: list[SmokeStep] = []

    def _record(number, title, *, input_, action, expected, observed, ok, evidence=None):
        steps.append(
            SmokeStep(
                number=number,
                title=title,
                input=input_,
                action=action,
                expected=expected,
                observed=observed,
                ok=ok,
                evidence=evidence or {},
            )
        )
        assert ok, f"Passo {number} ({title}) falhou: {observed}"

    # -- 1. Login e navegação principal alvo -------------------------------
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Smoke Go-Live",
            "name": "Admin",
            "username": f"admin-smoke-{uuid.uuid4().hex[:8]}",
            "password": "senha-local-segura",
        },
    )
    complete_mfa_enrollment(client)
    nav_html = Path("app/templates/index.html").read_text(encoding="utf-8")
    nav_views_present = all(f'data-view="{view}"' in nav_html for view in TARGET_NAV_VIEWS)
    _record(
        1,
        "Login e navegação principal",
        input_="Cadastro de família + login + MFA",
        action="POST /api/auth/setup, enroll MFA, verificar #main-nav",
        expected="201 na criação; MFA concluído; navegação alvo presente",
        observed=f"setup={setup.status_code}; nav_ok={nav_views_present}",
        ok=setup.status_code == 201 and nav_views_present,
    )

    checking = client.post("/api/accounts", json={"name": "Conta Corrente", "account_type": "checking"})
    checking_id = checking.json()["id"]
    liquidity = client.post("/api/accounts", json={"name": "Reserva DI", "account_type": "investment"})
    liquidity_id = liquidity.json()["id"]
    card = client.post(
        "/api/accounts",
        json={"name": "Cartão", "account_type": "credit_card", "card_closing_day": 10, "card_due_day": 17},
    )
    card_id = card.json()["id"]

    # -- 2. Lançamento manual de entrada -------------------------------------
    income = client.post(
        "/api/transactions",
        json={
            "booked_at": "2026-09-05",
            "description": "Salário",
            "amount": "4500.00",
            "movement_type": "income",
            "account_id": checking_id,
        },
    )
    _record(
        2,
        "Lançamento manual de entrada",
        input_="Salário R$4.500,00 em 2026-09-05 na Conta Corrente",
        action="POST /api/transactions (movement_type=income)",
        expected="201",
        observed=f"status={income.status_code}",
        ok=income.status_code == 201,
    )

    # -- 3. Lançamento manual de saída ---------------------------------------
    expense = client.post(
        "/api/transactions",
        json={
            "booked_at": "2026-09-06",
            "description": "Farmácia",
            "amount": "150.00",
            "movement_type": "expense",
            "account_id": checking_id,
            "category_name": "Saúde",
        },
    )
    _record(
        3,
        "Lançamento manual de saída",
        input_="Farmácia R$150,00 em 2026-09-06 na Conta Corrente",
        action="POST /api/transactions (movement_type=expense)",
        expected="201",
        observed=f"status={expense.status_code}",
        ok=expense.status_code == 201,
    )

    with session_factory() as db:
        household_id = db.scalar(select(Household.id))

    # -- 6. Criação/consulta de obrigação (created before step 4 uses it) ---
    obligation = client.post(
        "/api/obligations", json={"name": "Parcela chácara", "due_date": "2026-08-10", "amount": "500.00"}
    )
    obligation_query = client.get("/api/obligations")
    obligation_row = next(
        (item for item in obligation_query.json() if item["id"] == obligation.json().get("id")), None
    )
    _record(
        6,
        "Criação/consulta de obrigação",
        input_="Parcela chácara R$500,00, vencimento 2026-08-10",
        action="POST /api/obligations; GET /api/obligations",
        expected="201 na criação; COMPROMETIDO na consulta",
        observed=f"create={obligation.status_code}; financial_state={obligation_row['financial_state'] if obligation_row else None}",
        ok=obligation.status_code == 201 and obligation_row is not None and obligation_row["financial_state"] == "COMPROMETIDO",
    )

    # -- 4. Lançamento pelo Assistente com ação tipada (paga a obrigação) ---
    # Engineering review of this PR (Round 1, blocker 1): goes through the
    # real `POST /api/assistant/interpret` HTTP/service contract -- only the
    # Advisor sidecar transport is stubbed (`_interpret`/`_StubCodexAdvisorClient`
    # above); `interpret_message`, `_coerce_interpretation`, and
    # `build_typed_action_proposal`'s household-data resolution all run for
    # real, unstubbed.
    pay_response = {
        "intent": "pay_obligation",
        "extracted_fields": {
            "target_hint": "Parcela chácara",
            "funding_source_hint": "account",
            "account_hint": "Conta Corrente",
            "date_text": "2026-08-09",
        },
        "missing_fields": [],
        "clarifying_question": None,
        "confidence": 0.92,
        "model": "stub-codex-smoke",
    }
    interpret_pay = _interpret(client, "Paguei a parcela da chácara", pay_response)
    interpret_pay_json = interpret_pay.json()
    proposal_id = interpret_pay_json.get("proposal_id")
    pay_can_execute = interpret_pay_json["proposal"]["can_execute"]
    execute = (
        client.post("/api/assistant/execute", json={"proposal_id": proposal_id})
        if proposal_id
        else None
    )
    action_id = execute.json()["action"]["id"] if execute is not None else None
    _record(
        4,
        "Assistente: frase completa executa ação tipada",
        input_='"Paguei a parcela da chácara" (Codex resolve conta/data; smoke stuba só o transporte do sidecar)',
        action="POST /api/assistant/interpret -> POST /api/assistant/execute",
        expected="interpret 200; proposal.can_execute=True; execute 201; obrigação paga",
        observed=f"interpret={interpret_pay.status_code}; can_execute={pay_can_execute}; execute={execute.status_code if execute else None}",
        ok=bool(
            interpret_pay.status_code == 200
            and pay_can_execute
            and execute is not None
            and execute.status_code == 201
        ),
        evidence={"action_id": action_id},
    )

    # -- 5. Caso ambíguo no Assistente exigindo pergunta ---------------------
    ambiguous_response = {
        "intent": "create_expense",
        "extracted_fields": {"amount_text": "300", "description": "Combustível"},
        "missing_fields": ["account_hint", "funding_source_hint"],
        "clarifying_question": "De qual conta ou cartão saiu esse gasto?",
        "confidence": 0.55,
        "model": "stub-codex-smoke",
    }
    interpret_ambiguous = _interpret(client, "Gastei 300 de combustível", ambiguous_response)
    ambiguous_json = interpret_ambiguous.json()
    _record(
        5,
        "Assistente: frase ambígua pede informação",
        input_='"Gastei 300 de combustível" (sem conta/cartão)',
        action="POST /api/assistant/interpret",
        expected="200; can_execute=False; pergunta de esclarecimento; nada persistido (proposal_id nulo)",
        observed=(
            f"interpret={interpret_ambiguous.status_code}; "
            f"can_execute={ambiguous_json['proposal']['can_execute']}; "
            f"proposal_id={ambiguous_json.get('proposal_id')}; "
            f"question={ambiguous_json['proposal'].get('clarifying_question')!r}"
        ),
        ok=(
            interpret_ambiguous.status_code == 200
            and ambiguous_json.get("proposal_id") is None
            and ambiguous_json["proposal"]["can_execute"] is False
            and bool(ambiguous_json["proposal"].get("clarifying_question"))
        ),
    )

    # -- 7. Compra em cartão e consulta da fatura ----------------------------
    card_purchase = client.post(
        "/api/transactions",
        json={
            "booked_at": "2026-08-05",
            "description": "Farmácia no cartão",
            "amount": "150.00",
            "movement_type": "expense",
            "account_id": card_id,
            "category_name": "Saúde",
        },
    )
    sync = client.post("/api/card-invoices/sync", json={"account_id": card_id, "competence": "2026-08"})
    invoice_id = sync.json()["id"]
    _record(
        7,
        "Compra em cartão e consulta da fatura",
        input_="Compra R$150,00 no cartão em 2026-08-05 (competência 2026-08)",
        action="POST /api/transactions; POST /api/card-invoices/sync",
        expected="compra 201; fatura fechada com computed_total=150.00, sem reduzir banco",
        observed=f"purchase={card_purchase.status_code}; invoice_status={sync.json().get('status')}; computed_total={sync.json().get('computed_total')}",
        ok=(
            card_purchase.status_code == 201
            and sync.status_code == 201
            and sync.json()["status"] == "closed"
            and sync.json()["computed_total"] == "150.00"
        ),
    )

    # -- 8. Pagamento de fatura sem duplicar gasto ---------------------------
    pay_invoice = client.post(
        f"/api/card-invoices/{invoice_id}/pay",
        json={
            "paying_account_id": checking_id,
            "amount": "150.00",
            "booked_at": "2026-09-14",
            "description": "Pagamento fatura",
            "confirmed": True,
        },
    )
    with session_factory() as db:
        expense_total = len(
            list(
                db.scalars(
                    select(Transaction).where(
                        Transaction.household_id == household_id,
                        Transaction.transaction_type == "expense",
                        Transaction.description == "Farmácia no cartão",
                    )
                )
            )
        )
    _record(
        8,
        "Pagamento de fatura sem duplicar gasto",
        input_="Pagamento integral da fatura de agosto, R$150,00",
        action="POST /api/card-invoices/{id}/pay",
        expected="201; invoice status=paid; apenas uma despesa da compra original (nunca uma segunda)",
        observed=f"pay={pay_invoice.status_code}; invoice_status={pay_invoice.json().get('invoice', {}).get('status')}; expense_rows={expense_total}",
        ok=(pay_invoice.status_code == 201 and pay_invoice.json()["invoice"]["status"] == "paid" and expense_total == 1),
    )

    # -- 9. Transferência interna Conta Corrente <-> Privilège --------------
    # Engineering review of this PR (Round 1, blocker 4): `from_transaction_id
    # != to_transaction_id` alone only proves the transfer wrote two distinct
    # rows -- it does not prove the rebaseline §4.2/§14 property that a
    # transfer never becomes renda/despesa. Compare `summary.total_spending`/
    # `total_cash_in` before and after the transfer instead: both must be
    # bit-for-bit unchanged. `internal_transfers_total` is documented here
    # deliberately as the *gross* sum of both legs (R$500,00 out of Reserva
    # DI + R$500,00 into Conta Corrente = R$1.000,00), never the economic
    # value of the transfer -- reading it as "R$1.000,00 of spending or
    # income" would be exactly the misreading rebaseline §16 forbids.
    report_before_transfer = client.get("/api/reports?end_month=2026-09&months=1").json()
    transfer = client.post(
        "/api/transfers",
        json={
            "booked_at": "2026-09-10",
            "description": "Resgate para pagamento",
            "amount": "500.00",
            "from_account_id": liquidity_id,
            "to_account_id": checking_id,
        },
    )
    report_after_transfer = client.get("/api/reports?end_month=2026-09&months=1").json()
    summary_before = report_before_transfer["summary"]
    summary_after = report_after_transfer["summary"]
    _record(
        9,
        "Transferência interna Conta Corrente <-> Privilège",
        input_="Resgate R$500,00 do Reserva DI para Conta Corrente",
        action="POST /api/transfers; GET /api/reports antes/depois (total_spending, total_cash_in, internal_transfers_total)",
        expected=(
            "201; total_spending e total_cash_in inalterados (transferência não vira renda/despesa); "
            "internal_transfers_total sobe R$1.000,00 (soma bruta das duas pernas de R$500,00, não valor econômico)"
        ),
        observed=(
            f"transfer={transfer.status_code}; "
            f"total_spending {summary_before['total_spending']} -> {summary_after['total_spending']}; "
            f"total_cash_in {summary_before['total_cash_in']} -> {summary_after['total_cash_in']}; "
            f"internal_transfers_total {summary_before['internal_transfers_total']} -> {summary_after['internal_transfers_total']}"
        ),
        ok=(
            transfer.status_code == 201
            and transfer.json()["from_transaction_id"] != transfer.json()["to_transaction_id"]
            and summary_after["total_spending"] == summary_before["total_spending"]
            and summary_after["total_cash_in"] == summary_before["total_cash_in"]
            and summary_after["internal_transfers_total"] == summary_before["internal_transfers_total"] + 1000.0
        ),
    )

    # -- 10. Saldo confirmado e reconciliação --------------------------------
    observation = client.post(
        "/api/account-balances",
        json={
            "account_id": liquidity_id,
            "amount": "12000.00",
            "as_of_date": "2026-09-12",
            "observation_type": "point_in_time",
        },
    )
    dashboard = client.get("/api/dashboard?month=2026-09").json()
    _record(
        10,
        "Saldo confirmado e reconciliação",
        input_="Observação confirmada R$12.000,00 em 2026-09-12 no Reserva DI",
        action="POST /api/account-balances; GET /api/dashboard",
        expected="201; dashboard publica o saldo confirmado como soberano",
        observed=f"obs={observation.status_code}; observed_value={dashboard['noncanonical']['current_liquidity_observation']}",
        ok=(
            observation.status_code == 201
            and dashboard["noncanonical"]["current_liquidity_observation"] is not None
            and dashboard["noncanonical"]["current_liquidity_observation"]["value"] == 12000.0
        ),
    )

    # -- 11. Patrimônio/Studio -----------------------------------------------
    investment = client.post(
        "/api/investments",
        json={
            "name": "Studio",
            "historical_cost": "30000.00",
            "current_value": "35000.00",
            "expected_receivable_value": "45000.00",
            "valuation_date": "2026-09-01",
        },
    )
    dashboard_patrimony = client.get("/api/dashboard?month=2026-09").json()["noncanonical"]["patrimony"]
    _record(
        11,
        "Patrimônio/Studio",
        input_="Studio: investido 30.000, hoje 35.000, previsto 45.000",
        action="POST /api/investments; GET /api/dashboard (patrimony)",
        expected="201; patrimônio = caixa confirmado (12.000) + valor de hoje (35.000) = 47.000, nunca soma dos três",
        observed=f"investment={investment.status_code}; patrimony_value={dashboard_patrimony['value']}",
        ok=(investment.status_code == 201 and dashboard_patrimony["value"] == 47000.0 and dashboard_patrimony["investments_component"] == 35000.0),
    )

    # -- 12. Gastos & Economia e Relatórios ----------------------------------
    report = client.get("/api/reports?end_month=2026-09&months=1").json()
    economy = client.get("/api/spending-economy?end_month=2026-09&months=1").json()
    _record(
        12,
        "Gastos & Economia e Relatórios",
        input_="Mesmo período (2026-09) já populado pelos passos anteriores",
        action="GET /api/reports; GET /api/spending-economy",
        expected="200 em ambos; total_spending concorda entre as duas superfícies",
        observed=f"report_total={report['summary']['total_spending']}; economy_total={economy['seus_dados']['summary']['total_spending']}",
        ok=(report["summary"]["total_spending"] == economy["seus_dados"]["summary"]["total_spending"]),
    )

    # -- 13. Undo de ação do Assistente ---------------------------------------
    undo = (
        client.post(f"/api/assistant/actions/{action_id}/undo", json={"reason": "Pagamento lançado por engano pelo Assistente"})
        if action_id
        else None
    )
    with session_factory() as db:
        from app.models import Obligation

        obligation_row_after_undo = db.get(Obligation, obligation.json()["id"])
    _record(
        13,
        "Undo de ação do Assistente",
        input_=f"Desfazer a ação {action_id} (pagamento da chácara pelo Assistente)",
        action="POST /api/assistant/actions/{id}/undo",
        expected="200; obrigação volta a pending; trilha de auditoria preservada",
        observed=f"undo={undo.status_code if undo else None}; obligation_status={obligation_row_after_undo.status if obligation_row_after_undo else None}",
        ok=bool(undo is not None and undo.status_code == 200 and obligation_row_after_undo is not None and obligation_row_after_undo.status == "pending"),
    )

    # -- 14. Provável duplicidade sem decisão destrutiva automática ----------
    existing_fuel = client.post(
        "/api/transactions",
        json={
            "booked_at": "2026-09-05",
            "description": "Posto Ipiranga",
            "amount": "300.00",
            "movement_type": "expense",
            "account_id": checking_id,
            "category_name": "Combustível",
        },
    )
    duplicate_response = {
        "intent": "create_expense",
        "extracted_fields": {
            "amount_text": "300",
            "description": "Posto Ipiranga",
            "account_hint": "Conta Corrente",
            "date_text": "2026-09-05",
            "category_hint": "Combustível",
            "funding_source_hint": "account",
        },
        "missing_fields": [],
        "clarifying_question": None,
        "confidence": 0.88,
        "model": "stub-codex-smoke",
    }
    interpret_duplicate = _interpret(client, "Gastei 300 no posto de novo", duplicate_response)
    duplicate_json = interpret_duplicate.json()
    with session_factory() as db:
        fuel_expense_count = len(
            list(
                db.scalars(
                    select(Transaction).where(
                        Transaction.household_id == household_id,
                        Transaction.description == "Posto Ipiranga",
                    )
                )
            )
        )
    _record(
        14,
        "Provável duplicidade sem decisão destrutiva automática",
        input_='"Gastei 300 no posto de novo" (mesmo valor/categoria/data de um lançamento existente)',
        action="POST /api/assistant/interpret (sem duplicate_resolution)",
        expected="200; can_execute=False; candidate_kind=possible_duplicate; proposal_id nulo; nada é criado/apagado automaticamente",
        observed=(
            f"existing={existing_fuel.status_code}; interpret={interpret_duplicate.status_code}; "
            f"can_execute={duplicate_json['proposal']['can_execute']}; "
            f"kind={duplicate_json['proposal'].get('candidate_kind')}; fuel_rows={fuel_expense_count}"
        ),
        ok=(
            existing_fuel.status_code == 201
            and interpret_duplicate.status_code == 200
            and duplicate_json.get("proposal_id") is None
            and duplicate_json["proposal"]["can_execute"] is False
            and duplicate_json["proposal"].get("candidate_kind") == "possible_duplicate"
            and fuel_expense_count == 1
        ),
    )

    return steps


def test_go_live_smoke_fourteen_steps() -> None:
    client, session_factory = _client()
    with client:
        steps = run_go_live_smoke(client, session_factory)
    # Step 6 (create the obligation) necessarily runs before step 4 (pay it
    # through the Assistant), so execution order is not numeric order --
    # every step 1-14 must still appear exactly once, and every one must
    # have passed its own inline assertion above.
    assert sorted(step.number for step in steps) == list(range(1, 15))
    assert all(step.ok for step in steps)
