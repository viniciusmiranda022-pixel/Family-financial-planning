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
        pattern="^(expense|income|investment|redemption|refund|transfer|reconciliation)$"
    )
    account_id: str
    destination_account_id: str | None = None
    category_id: str | None = None
    category_name: str | None = Field(default=None, min_length=2, max_length=100)
    installment_current: int | None = Field(default=None, ge=1, le=999)
    installment_total: int | None = Field(default=None, ge=1, le=999)
    confirmed_large_amount: bool = False

    @model_validator(mode="after")
    def validate_movement_fields(self) -> "ManualTransactionRequest":
        if self.movement_type == "transfer":
            if not self.destination_account_id:
                raise ValueError("Transferência exige a conta de destino")
            if self.destination_account_id == self.account_id:
                raise ValueError("A conta de destino precisa ser diferente da conta de origem")
        elif self.destination_account_id is not None:
            raise ValueError("Conta de destino só se aplica a transferências entre contas")
        if self.installment_current is not None or self.installment_total is not None:
            if self.movement_type != "expense":
                raise ValueError("Parcelamento só se aplica a despesas")
            if self.installment_current is None or self.installment_total is None:
                raise ValueError("Informe a parcela atual e o total de parcelas juntos")
            if self.installment_current > self.installment_total:
                raise ValueError("A parcela atual não pode ser maior que o total de parcelas")
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
