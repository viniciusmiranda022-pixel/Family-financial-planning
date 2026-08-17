from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


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
