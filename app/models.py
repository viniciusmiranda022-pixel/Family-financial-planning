import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Household(Base, TimestampMixin):
    __tablename__ = "households"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    household: Mapped[Household] = relationship()


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("household_id", "name", name="uq_account_household_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    institution: Mapped[str] = mapped_column(String(80), default="")
    account_type: Mapped[str] = mapped_column(String(30), default="checking")
    owner_label: Mapped[str] = mapped_column(String(80), default="Família")
    last_four: Mapped[str | None] = mapped_column(String(4), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("household_id", "name", name="uq_category_household_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(7), default="#64748B")
    cash_cap: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    essential: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("household_id", "sha256", name="uq_document_household_sha"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="processing")
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transaction_household_date", "household_id", "booked_at"),
        Index("ix_transaction_fingerprint", "household_id", "fingerprint"),
        Index("ix_transaction_household_competence", "household_id", "competence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    booked_at: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    normalized_description: Mapped[str] = mapped_column(String(500), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    transaction_type: Mapped[str] = mapped_column(String(30), default="expense")
    owner_label: Mapped[str] = mapped_column(String(80), default="Família")
    card_last_four: Mapped[str | None] = mapped_column(String(4), nullable=True)
    installment_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    installment_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    competence: Mapped[str | None] = mapped_column(String(7), nullable=True)
    classification_source: Mapped[str] = mapped_column(String(30), default="legacy")
    classification_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    canonical_status: Mapped[str] = mapped_column(String(24), default="unassigned")
    duplicate_group_id: Mapped[str | None] = mapped_column(
        ForeignKey("duplicate_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    linked_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    transfer_group_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_priority: Mapped[int] = mapped_column(Integer, default=50)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    possible_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped[Category | None] = relationship()
    account: Mapped[Account | None] = relationship()


class ReviewItem(Base, TimestampMixin):
    __tablename__ = "review_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transaction: Mapped[Transaction | None] = relationship()


class CaptureDraft(Base, TimestampMixin):
    __tablename__ = "capture_drafts"
    __table_args__ = (
        Index("ix_capture_household_created", "household_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(30))
    detected_type: Mapped[str] = mapped_column(String(40), default="text")
    status: Mapped[str] = mapped_column(String(30), default="preview")
    processor: Mapped[str] = mapped_column(String(60), default="local_rules")
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document | None] = relationship()


class Commission(Base, TimestampMixin):
    __tablename__ = "commissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(String(200))
    expected_date: Mapped[date] = mapped_column(Date)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(7, 6), default=Decimal("0.06"))
    delay_days: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(20), default="expected")
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class PayrollRecord(Base, TimestampMixin):
    __tablename__ = "payroll_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    person_name: Mapped[str] = mapped_column(String(120))
    competence: Mapped[date] = mapped_column(Date)
    payment_date: Mapped[date] = mapped_column(Date)
    payroll_kind: Mapped[str] = mapped_column(String(30), default="regular")
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    deductions: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payroll_loan: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Obligation(Base, TimestampMixin):
    __tablename__ = "obligations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    recurrence_months: Mapped[int] = mapped_column(Integer, default=0)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(60), default="general")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FinancialProfile(Base, TimestampMixin):
    __tablename__ = "financial_profiles"
    __table_args__ = (UniqueConstraint("household_id", name="uq_financial_profile_household"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    monthly_salary_net: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    monthly_cash_cap: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    emergency_floor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    food_allowance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    meal_allowance_daily: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    workdays_month: Mapped[int] = mapped_column(Integer, default=20)
    investment_name: Mapped[str] = mapped_column(String(120), default="Reserva DI")
    investment_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    investment_gross_annual_rate: Mapped[Decimal] = mapped_column(Numeric(9, 8), default=0)
    investment_income_tax_rate: Mapped[Decimal] = mapped_column(Numeric(9, 8), default=0)
    projection_end: Mapped[date | None] = mapped_column(Date, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class IntegrityRun(Base):
    __tablename__ = "integrity_runs"
    __table_args__ = (
        Index("ix_integrity_runs_household_status", "household_id", "status"),
        Index("ix_integrity_runs_household_period", "household_id", "period"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(20))
    scope_entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    scope_entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    trigger: Mapped[str] = mapped_column(String(20))
    financial_rules_version: Mapped[str] = mapped_column(String(20))
    calculation_version: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class IntegrityFinding(Base, TimestampMixin):
    __tablename__ = "integrity_findings"
    __table_args__ = (
        UniqueConstraint(
            "household_id",
            "fingerprint",
            name="uq_integrity_finding_household_fingerprint",
        ),
        Index("ix_integrity_findings_household_status", "household_id", "status"),
        Index("ix_integrity_findings_household_severity", "household_id", "severity"),
        Index("ix_integrity_findings_household_period", "household_id", "period"),
        Index("ix_integrity_findings_household_invariant", "household_id", "invariant_id"),
        Index("ix_integrity_findings_household_fingerprint", "household_id", "fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("integrity_runs.id", ondelete="CASCADE"), index=True
    )
    invariant_id: Mapped[str] = mapped_column(String(12))
    fingerprint: Mapped[str] = mapped_column(String(64))
    financial_rules_version: Mapped[str] = mapped_column(String(20))
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(String(24), default="deterministic")
    status: Mapped[str] = mapped_column(String(24), default="open")
    check_status: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(16))
    scope: Mapped[str] = mapped_column(String(20))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    actual_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    difference_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON_DOCUMENT, nullable=True
    )
    actual: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON_DOCUMENT, nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_DOCUMENT, default=dict)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentReconciliation(Base):
    __tablename__ = "document_reconciliations"
    __table_args__ = (
        Index(
            "ix_document_reconciliations_household_document",
            "household_id",
            "document_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    parser_name: Mapped[str] = mapped_column(String(80))
    parser_version: Mapped[str] = mapped_column(String(40))
    formula_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    declared_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reconstructed_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    closing_balance_declared: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    closing_balance_calculated: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    credits_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    debits_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    purchases_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    fees_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    refunds_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    payments_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    tolerance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.01"))
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reconciled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AccountBalanceObservation(Base):
    __tablename__ = "account_balance_observations"
    __table_args__ = (
        Index(
            "ix_account_balance_observations_account_date",
            "household_id",
            "account_id",
            "as_of_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    as_of_date: Mapped[date] = mapped_column(Date)
    observation_type: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(30))
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_balance_observations.id", ondelete="SET NULL"), nullable=True
    )
    superseded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_balance_observations.id", ondelete="SET NULL"), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DuplicateGroup(Base, TimestampMixin):
    __tablename__ = "duplicate_groups"
    __table_args__ = (
        UniqueConstraint(
            "household_id", "group_key", name="uq_duplicate_group_household_key"
        ),
        Index("ix_duplicate_groups_household_status", "household_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    group_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="open")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    resolution: Mapped[str | None] = mapped_column(String(40), nullable=True)
    canonical_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )
    signals: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    rule_version: Mapped[str] = mapped_column(String(40))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DuplicateGroupMember(Base):
    __tablename__ = "duplicate_group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "transaction_id",
            name="uq_duplicate_group_member_transaction",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("duplicate_groups.id", ondelete="CASCADE"), index=True
    )
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    signals: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    source_priority: Mapped[int] = mapped_column(Integer)
    excluded_by_policy: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClassificationRule(Base, TimestampMixin):
    __tablename__ = "classification_rules"
    __table_args__ = (
        UniqueConstraint(
            "household_id",
            "normalized_merchant",
            "category_id",
            "movement_type",
            name="uq_classification_rule_consistent_choice",
        ),
        Index(
            "ix_classification_rules_household_merchant",
            "household_id",
            "normalized_merchant",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    household_id: Mapped[str] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"), index=True
    )
    normalized_merchant: Mapped[str] = mapped_column(String(500))
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    movement_type: Mapped[str] = mapped_column(String(30))
    confirmation_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="observed")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_from_correction: Mapped[bool] = mapped_column(Boolean, default=True)
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
