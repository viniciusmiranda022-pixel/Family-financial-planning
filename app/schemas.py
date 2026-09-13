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
