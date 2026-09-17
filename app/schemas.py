from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class SetupRequest(BaseModel):
    household_name: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=2, max_length=120)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=10, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=10, max_length=200)
    is_admin: bool = False


class AccountRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    institution: str = Field(default="", max_length=80)
    account_type: str = Field(default="checking", pattern="^(checking|credit_card|investment|cash)$")
    owner_label: str = Field(default="Família", max_length=80)
    last_four: str | None = Field(default=None, pattern=r"^\d{4}$")
    card_closing_day: int | None = Field(default=None, ge=1, le=31)
    card_due_day: int | None = Field(default=None, ge=1, le=31)


class TransactionUpdate(BaseModel):
    category_id: str | None = None
    excluded: bool | None = None
    possible_duplicate: bool | None = None
    reviewed: bool | None = None
    owner_label: str | None = Field(default=None, max_length=80)


class ManualTransactionRequest(BaseModel):
    booked_at: date
    description: str = Field(min_length=2, max_length=500)
    amount: Decimal = Field(gt=0)
    movement_type: str = Field(
        pattern="^(expense|income|investment|redemption|refund)$"
    )
    account_id: str
    category_id: str | None = None
    category_name: str | None = Field(default=None, min_length=2, max_length=100)
    installment_current: int | None = Field(default=None, ge=1, le=999)
    installment_total: int | None = Field(default=None, ge=1, le=999)
    # Explicit month (`YYYY-MM`) the user confirms this expense belongs to.
    # Only meaningful for `expense`: INV-017 requires a card purchase to keep
    # the canonical invoice competence, which this app has no invoice entity
    # to derive automatically yet (that lands with the "Contas a pagar"
    # slice), so it must be a human-confirmed fact instead of one silently
    # fabricated from `booked_at` -- see `create_manual_transaction`.
    competence: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    # FAMILY_FINANCE_PRIVILEGE_FUNDING_V15
    funding_source: str = Field(default="account", pattern="^(account|privilege)$")
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_movement_fields(self) -> "ManualTransactionRequest":
        if self.installment_current is not None or self.installment_total is not None:
            if self.movement_type != "expense":
                raise ValueError("Parcelamento só se aplica a despesas")
            if self.installment_current is None or self.installment_total is None:
                raise ValueError("Informe a parcela atual e o total de parcelas juntos")
            if self.installment_current > self.installment_total:
                raise ValueError("A parcela atual não pode ser maior que o total de parcelas")
        if self.competence is not None and self.movement_type != "expense":
            raise ValueError("Competência explícita só se aplica a despesas")
        if self.funding_source == "privilege" and self.movement_type != "expense":
            raise ValueError("Privilège DI como origem só se aplica a despesas")
        return self


class TransferRequest(BaseModel):
    """Structured transfer between two accounts of the same household.

    `docs/GO_LIVE_MANUAL_UX_PLAN.md` -- "Transferências" -- and
    `docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md`: origin and
    destination are both explicit, human-confirmed account ids; nothing
    here infers either account. `POST /transfers` (`app/api.py`) re-checks
    household ownership and account status before calling the canonical
    `create_internal_transfer` (`app/services/transfers.py`), which also
    re-validates distinct accounts as a structural INV-001 guard.
    """

    booked_at: date
    description: str = Field(min_length=2, max_length=500)
    amount: Decimal = Field(gt=0)
    from_account_id: str
    to_account_id: str
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_distinct_accounts(self) -> "TransferRequest":
        if self.from_account_id == self.to_account_id:
            raise ValueError("Conta de origem e destino devem ser diferentes")
        return self


class AdvisorHistoryItem(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class AdvisorRequest(BaseModel):
    message: str = Field(min_length=2, max_length=1000)
    history: list[AdvisorHistoryItem] = Field(default_factory=list, max_length=8)


class AssistantDuplicateResolution(BaseModel):
    """An explicit, discrete answer to a possible-duplicate warning a prior
    `POST /assistant/interpret` call already returned
    (`proposal.candidate_kind == "possible_duplicate"`) -- Work Order
    "deduplicação provável com as três escolhas humanas", engineering
    review of PR #92 (blocker 4). `transaction_id` must name the exact
    candidate that warning showed; a decision that no longer matches the
    current duplicate scan is never honored silently."""

    decision: str = Field(pattern="^(skip|import_anyway|view_existing)$")
    transaction_id: str = Field(min_length=1, max_length=64)


class AssistantInterpretRequest(BaseModel):
    """`POST /assistant/interpret` -- P0 #87, October Go-Live Slice 4.

    Calls the Codex sidecar for natural-language understanding and
    deterministically resolves the result against real household data.
    Never creates/edits/deletes a financial-fact row -- but, when the
    resolution is already materially complete and unambiguous
    (`proposal.can_execute = true`), it does persist one single-use
    `AssistantActionProposal` row (engineering review of PR #92, blocker 1)
    so a later `POST /assistant/execute` can be bound to exactly this
    resolution instead of trusting anything the HTTP caller resupplies --
    see `app.services.assistant_actions.persist_action_proposal`.
    `trace_id` is optional and, when supplied, is only threaded through for
    correlation with other audit entries; when omitted the backend
    generates one. `duplicate_resolution` answers a possible-duplicate
    warning a prior call in the same conversational turn already returned.
    """

    message: str = Field(min_length=1, max_length=1000)
    history: list[AdvisorHistoryItem] = Field(default_factory=list, max_length=8)
    trace_id: str | None = Field(default=None, max_length=64)
    duplicate_resolution: AssistantDuplicateResolution | None = None


class AssistantExecuteRequest(BaseModel):
    """`POST /assistant/execute` -- accepts only a `proposal_id` returned by
    a prior `POST /assistant/interpret` call whose response had
    `proposal.can_execute = true` (engineering review of PR #92, blocker
    1). The client can never supply `typed_action`/`payload`/
    `path_params`/`structured_interpretation` directly: every one of those
    fields is read back, unmodified, from the persisted
    `AssistantActionProposal` the interpret step already resolved
    deterministically against the closed vocabulary in
    `app.services.assistant_actions.TYPED_ACTIONS` and that action's own
    existing request schema (`ManualTransactionRequest`, `TransferRequest`,
    `ObligationPaymentRequest`, `CardInvoicePayRequest`,
    `RefundLinkRequest`) -- never a free-form write. `proposal_id` is
    itself the idempotency key: a retry with the same, already-consumed
    `proposal_id` returns the original result instead of executing again.
    """

    proposal_id: str = Field(min_length=1, max_length=64)
    disambiguation_qa: list[dict] = Field(default_factory=list, max_length=20)


class AssistantUndoRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class EntryTypeTemplateDeactivateRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class EntryTypeTemplateCreateRequest(BaseModel):
    """Records one confirmed use of a custom "Outra entrada"/"Outra saída"
    label against an already-existing transaction -- never creates or
    edits any `Transaction` itself (`transaction_id` must already exist and
    belong to this household). See
    `app.services.entry_type_templates.record_observed_entry`.
    """

    movement_type: str = Field(pattern="^(income|expense)$")
    label: str = Field(min_length=2, max_length=100)
    category_id: str | None = Field(default=None, max_length=36)
    transaction_id: str = Field(min_length=1, max_length=36)


class CaptureItemRequest(BaseModel):
    kind: str = Field(pattern="^(transaction|obligation|payroll)$")
    selected: bool = True
    booked_at: date | None = None
    due_date: date | None = None
    competence: date | None = None
    payment_date: date | None = None
    description: str = Field(min_length=2, max_length=500)
    amount: Decimal = Field(gt=0)
    movement_type: str | None = Field(
        default=None,
        pattern="^(expense|income|investment|redemption|refund|transfer|reconciliation)$",
    )
    signed_amount: Decimal | None = None
    account_id: str | None = None
    category_id: str | None = None
    category_name: str | None = Field(default=None, min_length=2, max_length=100)
    recurrence_months: int = Field(default=0, ge=0, le=120)
    occurrence_count: int = Field(default=1, ge=1, le=240)
    payroll_kind: str = Field(
        default="regular", pattern="^(regular|13_first|13_second|vacation_extra|other)$"
    )
    source_line: int | None = Field(default=None, ge=1)
    installment_current: int | None = Field(default=None, ge=1, le=999)
    installment_total: int | None = Field(default=None, ge=1, le=999)
    card_last_four: str | None = Field(default=None, pattern=r"^\d{4}$")
    funding_source: str = Field(default="account", pattern="^(account|privilege)$")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "CaptureItemRequest":
        if self.kind == "transaction" and (not self.booked_at or not self.movement_type):
            raise ValueError("Lançamentos precisam de data e tipo de movimentação")
        if (
            self.kind == "transaction"
            and self.movement_type in {"transfer", "reconciliation"}
            and self.signed_amount is not None
            and abs(abs(self.signed_amount) - self.amount) > Decimal("0.01")
        ):
            raise ValueError("O valor assinado precisa corresponder ao valor do lançamento")
        if (
            self.funding_source == "privilege"
            and (self.kind != "transaction" or self.movement_type != "expense")
        ):
            raise ValueError("Privilège DI como origem só se aplica a uma despesa")
        if self.kind == "obligation" and not self.due_date:
            raise ValueError("Boletos e obrigações precisam de vencimento")
        if self.kind == "payroll" and (not self.competence or not self.payment_date):
            raise ValueError("Holerites precisam de competência e data de pagamento")
        return self


class CaptureConfirmRequest(BaseModel):
    items: list[CaptureItemRequest] = Field(min_length=1, max_length=1000)
    confirmed_large_amount: bool = False


class CommissionRequest(BaseModel):
    description: str = Field(min_length=2, max_length=200)
    expected_date: date
    gross_amount: Decimal = Field(gt=0)
    tax_rate: Decimal = Field(default=Decimal("0.06"), ge=0, le=1)
    delay_days: int = Field(default=60, ge=0, le=365)


class CommissionReceiveRequest(BaseModel):
    """October Go-Live Slice 3: explicit, human-confirmed act of marking a
    `Commission` receivable as actually received (rebaseline §8.3 -- a
    commission only enters the financial picture "quando efetivamente
    registrada/recebida"). Never creates or edits any `Transaction`; the
    real credit is expected to already exist as one, registered through the
    normal income flow. `received_date` defaults to today when omitted."""

    received_date: date | None = None


class PayrollRequest(BaseModel):
    person_name: str = Field(min_length=2, max_length=120)
    competence: date
    payment_date: date
    payroll_kind: str = Field(
        default="regular", pattern="^(regular|13_first|13_second|vacation_extra|other)$"
    )
    gross_amount: Decimal = Field(default=0, ge=0)
    deductions: Decimal = Field(default=0, ge=0)
    net_amount: Decimal
    payroll_loan: Decimal = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=1000)


class ObligationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    due_date: date
    amount: Decimal = Field(gt=0)
    recurrence_months: int = Field(default=0, ge=0, le=120)
    occurrence_count: int = Field(default=1, ge=1, le=240)
    category: str = Field(default="general", max_length=60)


class ObligationPaymentRequest(BaseModel):
    transaction_id: str | None = Field(default=None, max_length=36)
    account_id: str | None = Field(default=None, max_length=36)
    paid_at: date | None = None
    description: str | None = Field(default=None, min_length=2, max_length=500)
    funding_source: str = Field(default="account", pattern="^(account|privilege)$")
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_payment_source(self) -> "ObligationPaymentRequest":
        has_transaction = bool(self.transaction_id)
        has_account = bool(self.account_id)
        if has_transaction == has_account:
            raise ValueError(
                "Escolha exatamente uma origem: lançamento existente ou conta para criar o pagamento"
            )
        if has_account and self.paid_at is None:
            raise ValueError("Informe a data do pagamento")
        return self


class ObligationPaymentUndoRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ProfileRequest(BaseModel):
    monthly_salary_net: Decimal = Field(ge=0)
    monthly_cash_cap: Decimal = Field(ge=0)
    emergency_floor: Decimal = Field(ge=0)
    food_allowance: Decimal = Field(ge=0)
    meal_allowance_daily: Decimal = Field(ge=0)
    workdays_month: int = Field(ge=0, le=31)
    investment_name: str = Field(min_length=2, max_length=120)
    investment_balance: Decimal = Field(ge=0)
    investment_gross_annual_rate: Decimal = Field(ge=0, le=1)
    investment_income_tax_rate: Decimal = Field(ge=0, le=1)
    projection_end: date


class NotificationSettingsUpdateRequest(BaseModel):
    """MAIL-00 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`). Format/range
    validation for `send_time_local`/`timezone` happens in
    `app.services.notification_settings` (needs household-independent
    constants, not per-field pydantic validators)."""

    enabled: bool
    send_time_local: str = Field(min_length=5, max_length=5)
    timezone: str = Field(min_length=2, max_length=64)


class NotificationRecipientCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    active: bool = True
    notify_d1: bool = True
    notify_d0: bool = True


class NotificationRecipientUpdateRequest(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=254)
    active: bool | None = None
    notify_d1: bool | None = None
    notify_d0: bool | None = None


class NotificationTestEmailRequest(BaseModel):
    """MAIL-01 (`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`). Only a
    reference to an already-cadastrado recipient -- never a host/username/
    password, which the Work Order explicitly forbids accepting from the
    browser for this endpoint."""

    recipient_id: str = Field(min_length=1, max_length=36)


class IntegrityRunRequest(BaseModel):
    scope: str = Field(pattern="^(entity|period|global)$")
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    entity_type: str | None = Field(default=None, max_length=80)
    entity_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_scope_fields(self) -> "IntegrityRunRequest":
        if self.scope == "period" and not self.period:
            raise ValueError("Auditoria por período exige mês no formato AAAA-MM")
        if self.scope == "entity" and (
            self.entity_type != "transaction" or not self.entity_id
        ):
            raise ValueError("Auditoria por entidade exige uma transação")
        if self.scope == "global" and any(
            value is not None for value in (self.period, self.entity_type, self.entity_id)
        ):
            raise ValueError("Auditoria global não aceita período ou entidade")
        return self


class SemanticAuditRequest(BaseModel):
    """Request body for POST /api/integrity/semantic-audit.

    This is a request for a *consultative* Codex annotation of the
    deterministic integrity status the Financial Integrity Engine already
    computed -- see app/services/codex_audit.py. It never accepts a status,
    score, or any financial figure: the endpoint always recomputes those
    itself from the database and echoes them back unchanged.
    """

    audit_type: str = Field(
        default="period_review",
        pattern="^(period_review|projection_review|reconciliation_review|report_review)$",
    )
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class AccountBalanceObservationRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=36)
    amount: Decimal
    as_of_date: date
    observation_type: str = Field(pattern="^(opening|closing|point_in_time)$")
    document_id: str | None = Field(default=None, max_length=36)
    supersedes_id: str | None = Field(default=None, max_length=36)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_supersession_reason(self) -> "AccountBalanceObservationRequest":
        if self.supersedes_id and not self.reason:
            raise ValueError("Correção de saldo exige justificativa")
        return self


class DuplicateResolutionRequest(BaseModel):
    resolution: str = Field(pattern="^(duplicate|distinct)$")
    canonical_transaction_id: str | None = Field(default=None, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def validate_canonical_choice(self) -> "DuplicateResolutionRequest":
        if self.resolution == "duplicate" and not self.canonical_transaction_id:
            raise ValueError("Resolução como duplicidade exige a fonte canônica")
        return self


class FindingLifecycleRequest(BaseModel):
    """Body shared by acknowledge/resolve/ignore/false-positive finding actions.

    `reason` is mandatory for every one of these human lifecycle actions (PR 7
    Work Order item 4: "com motivo obrigatório e audit trail").
    """

    reason: str = Field(min_length=3, max_length=1000)


class MonthlyCloseReopenRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class CardPaymentLinkRequest(BaseModel):
    """Human-confirmed link between a bank debit and a card-invoice payment.

    Order of the two ids does not matter -- the service resolves which one
    is the checking-side row and which is the credit-card-side row. `reason`
    is mandatory, matching every other human reconciliation/lifecycle action
    in this project (`FindingLifecycleRequest`, `DuplicateResolutionRequest`,
    `MonthlyCloseReopenRequest`): even confirming a deterministic suggestion
    is still the human, not the system, asserting the pair is correct.
    """

    checking_transaction_id: str = Field(min_length=1, max_length=36)
    card_transaction_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)


class CardPaymentUnlinkRequest(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)


class CardInvoicePaymentRequest(BaseModel):
    """Manual payment of a card invoice -- go-live manual slice 3.

    `docs/WORK_ORDER_MANUAL_PAYABLES_CARD_PAYMENT.md`, "Fluxo especializado
    — pagamento de fatura de cartão": the five minimum inputs are the
    invoice (`card_transaction_id`), amount paid, paying account, date and
    an explicit human confirmation. `confirmed` is that explicit assent,
    distinct from `confirmed_large_amount` (`_large_entry_threshold`), which
    only guards unusually large values the same way every other manual
    command already does. Reuses `link_card_payment`'s exact amount
    tolerance/direction contract in `app/services/card_payment_reconciliation
    .py::pay_card_invoice` -- no second reconciliation policy.
    """

    card_transaction_id: str = Field(min_length=1, max_length=36)
    paying_account_id: str
    amount: Decimal = Field(gt=0)
    booked_at: date
    description: str = Field(min_length=2, max_length=500)
    confirmed: bool
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_confirmation(self) -> "CardInvoicePaymentRequest":
        if not self.confirmed:
            raise ValueError("Confirme explicitamente que este pagamento aconteceu")
        return self


class CardInvoiceSyncRequest(BaseModel):
    """`POST /card-invoices/sync` -- October Go-Live Slice 2.

    The project's "leitura preguiçosa no primeiro acesso" trigger
    (`docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §4): idempotent get-or-create
    plus deterministic resync of the `CardInvoice` for
    `(account_id, competence)`. `competence` defaults to the account's
    current billing cycle when omitted.
    """

    account_id: str = Field(min_length=1, max_length=36)
    competence: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class CardInvoiceCloseRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class CardInvoicePayRequest(BaseModel):
    """Manual payment of a `CardInvoice` -- superset of
    `CardInvoicePaymentRequest`/`pay_card_invoice`: `amount` may be any
    value up to the invoice's outstanding balance, including a partial
    amount (rebaseline §6.5). Mirrors the same five minimum inputs and
    explicit `confirmed` assent every other manual command in this project
    requires (`CardInvoicePaymentRequest`, `MonthlyCloseReopenRequest`).
    """

    paying_account_id: str = Field(min_length=1, max_length=36)
    amount: Decimal = Field(gt=0)
    booked_at: date
    description: str = Field(min_length=2, max_length=500)
    confirmed: bool
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_confirmation(self) -> "CardInvoicePayRequest":
        if not self.confirmed:
            raise ValueError("Confirme explicitamente que este pagamento aconteceu")
        return self


class RefundLinkRequest(BaseModel):
    """Explicit, human-confirmed link from a `refund` transaction to the
    original `expense` it neutralizes -- rebaseline §6.6; never inferred."""

    refund_transaction_id: str = Field(min_length=1, max_length=36)
    original_transaction_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)


class RefundUnlinkRequest(BaseModel):
    refund_transaction_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)


class InvestmentCreateRequest(BaseModel):
    """October Go-Live Slice 6 (P0 #87), rebaseline §16.1 minimum model.

    `historical_cost` is the initial cost basis (e.g. what the Studio has
    already cost so far); `current_value` is required so patrimony
    (`app.services.investments.investments_summary`/
    `household_patrimony_summary`) always has a real "Valor de hoje" from
    the moment an asset is created, never an implicit zero standing in for
    "unknown". Creating an investment also appends its
    first `InvestmentValuation` snapshot (`app.api._create_investment_impl`)
    so history is complete from day one.
    """

    name: str = Field(min_length=2, max_length=120)
    historical_cost: Decimal = Field(ge=0)
    current_value: Decimal = Field(ge=0)
    expected_receivable_value: Decimal | None = Field(default=None, ge=0)
    expected_receipt_date: date | None = None
    valuation_date: date
    notes: str | None = Field(default=None, max_length=2000)


class InvestmentValuationRequest(BaseModel):
    """Records a new valuation event for an existing `Investment`
    (`POST /investments/{investment_id}/valuations`) -- rebaseline §16.3
    examples ("Hoje acho que o Studio vale 35 mil"/"A previsão agora é
    receber 45 mil"). At least one of `current_value`/
    `expected_receivable_value` must be given; the other keeps its current
    value in the new snapshot. Never touches `historical_cost` -- that is
    exclusively `InvestmentContributionRequest`'s job (rebaseline §16.2:
    cost basis and current value are distinct facts, one event never moves
    both)."""

    current_value: Decimal | None = Field(default=None, ge=0)
    expected_receivable_value: Decimal | None = Field(default=None, ge=0)
    valuation_date: date
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_at_least_one_value(self) -> "InvestmentValuationRequest":
        if self.current_value is None and self.expected_receivable_value is None:
            raise ValueError(
                "Informe o novo valor de hoje e/ou o novo valor previsto a receber"
            )
        return self


class InvestmentContributionRequest(BaseModel):
    """Records a contribution (aporte) into an existing `Investment` --
    increases `historical_cost` only (rebaseline §16.2/§16.3: "Coloquei
    mais 5 mil no Studio" never changes `current_value`, which is only ever
    confirmed by a separate valuation event).

    `funding_transaction_id` is **required** (engineering review on PR #94,
    blocking item 2): it must name an already-existing `Transaction`
    representing the real cash movement out of an account (the same
    `movement_type="investment"`/"Transferência patrimonial" manual entry
    already supported by `ManualTransactionRequest`, or an imported
    equivalent) -- this endpoint never creates that transaction itself, it
    only links to one that already exists, exactly like `RefundLinkRequest`
    always requires (never optionally links) an already-existing refund
    (Work Order: "sem fabricar gasto econômico ou origem de caixa"). A new
    aporte event must never silently increase cost basis with no
    corresponding real funding fact -- the opening/historical cost basis
    captured at asset creation, import or backfill time
    (`InvestmentCreateRequest.historical_cost`) is the only place a cost
    figure is ever accepted without a linked cash movement, because that is
    a point-in-time fact about the past, not a new event. A human who has
    not yet recorded the outgoing transfer must record it first (the
    existing manual-entry form), then link it here -- the Assistant's own
    typed-action proposal (`register_asset_contribution`) already follows
    the same rule, asking for the origin when it cannot resolve exactly one
    matching transaction."""

    contribution_amount: Decimal = Field(gt=0)
    valuation_date: date
    funding_transaction_id: str = Field(min_length=1, max_length=36)
    note: str | None = Field(default=None, max_length=2000)


class InvestmentValuationUndoRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


# Issue #85 (docs/WORK_ORDER_PRIVILEGE_DI_CVM.md): CVM daily fund valuation
# position evidence. `fund_cnpj` accepts either the formatted
# ("26.199.519/0001-34") or digits-only form -- `app.services.privilege_valuation`
# normalizes either way before persisting/matching (see
# `app.services.cvm_client.normalize_fund_cnpj`).
class FundPositionSnapshotRequest(BaseModel):
    fund_cnpj: str = Field(min_length=11, max_length=20)
    units_held: Decimal = Field(ge=0)
    effective_date: date
    evidence_type: str = Field(pattern="^(statement_position|confirmed_balance_reconciliation)$")
    evidence_document_id: str | None = Field(default=None, min_length=1, max_length=36)
    evidence_balance_observation_id: str | None = Field(default=None, min_length=1, max_length=36)
    note: str | None = Field(default=None, max_length=2000)


class FundPositionMovementRequest(BaseModel):
    fund_cnpj: str = Field(min_length=11, max_length=20)
    delta_units: Decimal
    effective_date: date
    evidence_type: str = Field(pattern="^(application|redemption)$")
    note: str | None = Field(default=None, max_length=2000)


class ClassificationRuleEditRequest(BaseModel):
    """Admin-only edit of an eligible local merchant rule's category.

    `movement_type` is deliberately not a field here: it is deterministic
    evidence inherited from the confirming transaction, never a free choice
    (see `app/services/classification_learning.py::edit_classification_rule`).
    """

    category_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=3, max_length=1000)


class ClassificationRuleDeactivateRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class PurchaseScenarioAlternativeRequest(BaseModel):
    """One hypothetical purchase alternative to run through the canonical
    Projection Engine/Validator for `POST /purchases/scenario-comparison`.

    Deliberately mirrors the same structured inputs the rest of the app
    already supports for a financed purchase (price, down payment,
    installment count, interest rate) instead of accepting free text -- see
    `docs/WORK_ORDER_PURCHASE_SCENARIO_COMPARISON.md`. No new financial
    semantics: `price`/`down_payment`/`installment_count`/
    `monthly_interest_rate` feed the same amortization formula the Advisor
    chat already uses (`app.services.finance.amortized_installment_payment`),
    and the resulting schedule is merged into the exact `ForecastInput`
    contract the canonical Projection Engine/Validator already consume.
    """

    label: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(gt=0, le=Decimal("100000000"))
    down_payment: Decimal = Field(default=Decimal("0"), ge=0)
    installment_count: int = Field(default=1, ge=1, le=120)
    monthly_interest_rate: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.30"))
    # First month this purchase would affect the projection (`YYYY-MM`).
    # Defaults to the projection's own start month (the next calendar
    # month) when omitted -- the current month is already closed by the
    # snapshot the projection starts from, so an earlier month could never
    # be reflected anyway (see `forecast`/`compare_purchase_scenarios`).
    purchase_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @model_validator(mode="after")
    def validate_down_payment(self) -> "PurchaseScenarioAlternativeRequest":
        if self.down_payment > self.price:
            raise ValueError("A entrada não pode ser maior que o preço da compra")
        return self


class PurchaseScenarioComparisonRequest(BaseModel):
    alternatives: list[PurchaseScenarioAlternativeRequest] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def validate_unique_labels(self) -> "PurchaseScenarioComparisonRequest":
        labels = [alternative.label for alternative in self.alternatives]
        if len(labels) != len(set(labels)):
            raise ValueError("Cada alternativa precisa de um rótulo único")
        return self


class CardCompetenceRepairApplyRequest(BaseModel):
    """`docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md`, "Aplicação explícita,
    seletiva e race-safe": the client selects which preview candidates to
    apply and proves it read a specific financial revision -- it never sends
    a `target_competence` of its own; the backend always recomputes it
    through `app.services.card_competence.card_invoice_competence`.
    """

    transaction_ids: list[str] = Field(min_length=1, max_length=500)
    expected_financial_revision: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)


class CardCompetenceRepairRollbackRequest(BaseModel):
    """Mirrors `CardCompetenceRepairApplyRequest` for the logical-rollback
    endpoint -- same explicit selection and revision-guard contract."""

    transaction_ids: list[str] = Field(min_length=1, max_length=500)
    expected_financial_revision: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)


class MfaEnrollConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class MfaChallengeRequest(BaseModel):
    """Body shared by the login MFA challenge and reconfiguration/recovery
    re-authentication: exactly one of a 6-digit TOTP code or a recovery
    code, never both -- accepting both and treating either as sufficient
    would silently let a caller skip whichever check it omits."""

    code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    recovery_code: str | None = Field(default=None, min_length=8, max_length=40)

    @model_validator(mode="after")
    def validate_single_factor(self) -> "MfaChallengeRequest":
        if bool(self.code) == bool(self.recovery_code):
            raise ValueError("Informe exatamente um: código TOTP ou código de recuperação")
        return self


class WhatsAppAuthorizedNumberCreateRequest(BaseModel):
    """WA-01 (`docs/WORK_ORDER_WA_01.md`): admin request to link a phone
    number to a household member. `phone_number` accepts any human-typed
    format (`"+55 11 99999-8888"`, `"5511999998888"`, ...) --
    `app.services.whatsapp_gateway.normalize_phone` reduces it to the same
    digits-only canonical form an inbound webhook's `wa_id` already is."""

    user_id: str = Field(min_length=1, max_length=36)
    phone_number: str = Field(min_length=8, max_length=32)


class MfaReauthRequest(BaseModel):
    """Strong re-authentication for reconfiguration/recovery-code
    regeneration: current password plus current second factor, per the
    Work Order's "Reconfiguração do autenticador" (never just the already
    valid session cookie)."""

    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    recovery_code: str | None = Field(default=None, min_length=8, max_length=40)

    @model_validator(mode="after")
    def validate_single_factor(self) -> "MfaReauthRequest":
        if bool(self.code) == bool(self.recovery_code):
            raise ValueError("Informe exatamente um: código TOTP ou código de recuperação")
        return self
