import json
import logging
import re
import time
import uuid
from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import (
    Account,
    AccountBalanceObservation,
    AuditEvent,
    CaptureDraft,
    CaptureProcessingJob,
    Category,
    ClassificationRule,
    Commission,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    DuplicateGroupMember,
    FinancialProfile,
    FinancialSnapshot,
    FinancialSnapshotLineage,
    Household,
    IntegrityFinding,
    IntegrityRun,
    MfaFactor,
    MfaRecoveryCode,
    Obligation,
    PayrollRecord,
    ReviewItem,
    Transaction,
    User,
)
from app.schemas import (
    AccountBalanceObservationRequest,
    AccountRequest,
    AdvisorRequest,
    CaptureConfirmRequest,
    CardCompetenceRepairApplyRequest,
    CardCompetenceRepairRollbackRequest,
    CardInvoicePaymentRequest,
    CardPaymentLinkRequest,
    CardPaymentUnlinkRequest,
    ClassificationRuleDeactivateRequest,
    ClassificationRuleEditRequest,
    CommissionRequest,
    DuplicateResolutionRequest,
    FindingLifecycleRequest,
    IntegrityRunRequest,
    LoginRequest,
    ManualTransactionRequest,
    MfaChallengeRequest,
    MfaEnrollConfirmRequest,
    MfaReauthRequest,
    MonthlyCloseReopenRequest,
    ObligationPaymentRequest,
    ObligationPaymentUndoRequest,
    ObligationRequest,
    PayrollRequest,
    ProfileRequest,
    PurchaseScenarioAlternativeRequest,
    PurchaseScenarioComparisonRequest,
    SemanticAuditRequest,
    SetupRequest,
    TransactionUpdate,
    TransferRequest,
    UserCreateRequest,
)
from app.security import (
    PENDING_PURPOSE_ENROLL,
    PENDING_PURPOSE_VERIFY,
    bump_session_version,
    clear_pending_cookie,
    clear_session_cookie,
    get_current_user,
    get_enroll_pending_user,
    get_verify_pending_user,
    hash_password,
    mfa_pending_expires_at,
    set_pending_cookie,
    set_session_cookie,
    verify_password,
)
from app.services import capture_worker
from app.services import mfa as mfa_service
from app.services.card_competence import (
    card_invoice_competence,
    resolve_expense_competence,
)
from app.services.card_competence_repair import (
    BlockedCandidateError,
    CardCompetenceRepairError,
    NoLongerCandidateError,
    StaleRevisionError,
    apply_card_competence_repair,
    preview_card_competence_repair,
    rollback_card_competence_repair,
    serialize_candidate,
)
from app.services.card_payment_reconciliation import (
    CardPaymentLinkError,
    link_card_payment,
    list_card_invoice_obligations,
    list_card_payment_reconciliations,
    pay_card_invoice,
    serialize_card_invoice_obligation,
    serialize_card_payment_match,
    unlink_card_payment,
)
from app.services.classification_learning import (
    accept_classification_rule,
    classify_with_local_rules,
    deactivate_classification_rule,
    edit_classification_rule,
    record_confirmed_correction,
    serialize_classification_rule,
)
from app.services.classifier import normalize_description
from app.services.codex_audit import run_semantic_audit
from app.services.codex_client import CodexAdvisorClient
from app.services.crypto import EncryptedDocumentStore
from app.services.duplicates import (
    register_transaction_duplicates,
    resolve_duplicate_group,
    serialize_duplicate_group,
    source_priority,
)
from app.services.finance import (
    ForecastCommission,
    ForecastInput,
    add_months,
    amortized_installment_payment,
    build_forecast,
    commission_net,
    money,
    month_key,
    monthly_net_rate,
)
from app.services.financial_integrity import (
    ACTIVE_FINDING_STATUSES,
    FindingLifecycleError,
    IntegrityCheck,
    IntegrityRunScope,
    IntegrityRunTrigger,
    acknowledge_finding,
    assess_integrity,
    build_baseline_checks,
    consolidated_integrity_status,
    execute_integrity_run,
    ignore_finding,
    mark_finding_false_positive,
    record_isolated_run_failure,
    resolve_finding,
    serialize_finding,
    serialize_run,
)
from app.services.financial_invariants import InvariantContext, InvariantScope
from app.services.financial_revision import current_household_financial_revision
from app.services.financial_snapshots import (
    account_cash_flow_rows,
    build_snapshot,
    category_monetary_publication,
    category_spending_rows,
    dashboard_and_report_consistency_facts,
    dashboard_monetary_publication,
    liquidity_transition_facts,
    report_month_monetary_publication,
    report_summary_monetary_publication,
    serialize_snapshot,
    snapshot_lineage_facts,
)
from app.services.importer import (
    PARSER_CONTRACT_VERSION,
    ParsedTransaction,
    file_sha256,
    parse_document_contract,
    parse_payroll_document,
    transaction_fingerprint,
)
from app.services.invariant_registry import evaluate_invariant
from app.services.monthly_close import (
    MonthlyCloseGateError,
    MonthlyCloseStateError,
    assert_close_runnable,
    get_monthly_close,
    reopen_monthly_close,
    serialize_monthly_close,
    trust_monthly_close,
    upsert_monthly_close_after_run,
)
from app.services.projection_engine import PROJECTION_CALCULATION_VERSION
from app.services.projection_validator import PROJECTION_TOLERANCE, validate_projection
from app.services.reconciliation import (
    persist_reconciliation,
    reconcile_parsed_document,
    serialize_reconciliation,
    unknown_reconciliation,
)
from app.services.report_export import (
    REPORT_EXPORT_MEDIA_TYPES,
    build_report_pdf,
    build_report_workbook,
    report_export_filename,
)
from app.services.smart_capture import CaptureParseError, preview_capture
from app.services.transfers import TransferError, create_internal_transfer

router = APIRouter(prefix="/api")
settings = get_settings()
logger = logging.getLogger(__name__)

DEFAULT_CATEGORIES = (
    ("Conciliação", "#64748B", None, False),
    ("Transferência patrimonial", "#64748B", None, False),
    ("Transferência interna", "#64748B", None, False),
    ("Repasses a confirmar", "#F59E0B", None, False),
    ("Reembolsos e estornos", "#22C55E", None, False),
    ("Recebimentos e devoluções", "#0EA5E9", None, False),
    ("Receitas", "#16A34A", None, False),
    ("Restaurantes e delivery", "#F97316", Decimal("0"), False),
    ("Mercado e itens domésticos", "#10B981", Decimal("0"), True),
    ("Água e energia da residência", "#0EA5E9", Decimal("650"), True),
    ("Transporte", "#3B82F6", Decimal("700"), True),
    ("Saúde e farmácia", "#EF4444", Decimal("350"), True),
    ("Academia", "#8B5CF6", Decimal("200"), False),
    ("Assinaturas digitais", "#6366F1", Decimal("100"), False),
    ("Estética e beleza", "#EC4899", Decimal("0"), False),
    ("Viagens e lazer", "#F43F5E", Decimal("0"), False),
    ("Seguros", "#14B8A6", None, True),
    ("Juros, IOF e tarifas", "#DC2626", Decimal("0"), False),
    ("Compras, casa e vestuário", "#A855F7", Decimal("300"), False),
    ("Pix, ajudas e outros", "#D97706", Decimal("300"), False),
    ("Operação da chácara", "#15803D", Decimal("500"), True),
    ("Margem para imprevistos", "#475569", Decimal("900"), True),
    ("Revisar", "#F59E0B", None, False),
)

# `IntegrityFinding.severity` is a plain string column, so
# `.order_by(IntegrityFinding.severity.desc())` sorts lexicographically
# ("warning" > "review" > "info" > "critical" > "block"), not by real
# severity rank. A query that also `.limit()`s (see the semantic-audit
# endpoint below) could then silently keep WARNING/REVIEW findings over
# BLOCK/CRITICAL ones. This expression orders by explicit rank instead;
# shared by every ORDER BY on IntegrityFinding.severity in this module so
# the ranking can't drift between call sites.
_FINDING_SEVERITY_RANK = case(
    {"block": 5, "critical": 4, "review": 3, "warning": 2, "info": 1},
    value=IntegrityFinding.severity,
    else_=0,
)


def decimal_value(value: Decimal | None) -> float:
    return float(value or 0)


def _month_start(value: str | None) -> date:
    if not value:
        return date.today().replace(day=1)
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Mês deve usar o formato AAAA-MM") from exc


def _expense_signature(transaction: Transaction, account_type: str | None) -> tuple[object, ...]:
    # FAMILY_FINANCE_CARD_COMPETENCE_V11
    period: object = transaction.booked_at
    if account_type == "credit_card":
        # A persisted `competence` is always the canonical "YYYY-MM" string
        # `month_key` produces (see `_card_invoice_competence`). The
        # fallback for a transaction with no persisted competence must match
        # that same string shape -- a bare `(year, month)` tuple never
        # equals the string form, so two same-period card expenses would
        # silently never be recognized as duplicates of each other purely
        # because of this type mismatch, not because they actually differ.
        period = transaction.competence or month_key(transaction.booked_at)
    return (
        period,
        normalize_description(transaction.description),
        money(transaction.amount),
        transaction.installment_current,
        transaction.installment_total,
    )


def _consolidated_transactions(
    db: Session,
    household_id: str,
    start: date,
    end: date,
) -> tuple[list[tuple[Transaction, str]], list[Transaction]]:
    """Return one authoritative copy of every movement in the period."""
    raw_rows = db.execute(
        select(Transaction, Category.name, Account.account_type, Document.document_type)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Account, Account.id == Transaction.account_id)
        .outerjoin(Document, Document.id == Transaction.document_id)
        .where(
            Transaction.household_id == household_id,
            or_(
                and_(
                    Account.account_type == "credit_card",
                    or_(
                        # `competence` ("YYYY-MM") sorts lexicographically
                        # the same as chronological order, so a half-open
                        # string range mirrors the `booked_at` range below --
                        # this must hold for the multi-month windows this
                        # function is actually called with (`cut_plan`'s
                        # 6-month lookback, `_build_report_payload`'s 1-12
                        # month range). Comparing only against `start`'s own
                        # month here previously dropped every card
                        # transaction whose invoice competence fell in any
                        # later month of the window.
                        and_(
                            Transaction.competence >= start.strftime("%Y-%m"),
                            Transaction.competence < end.strftime("%Y-%m"),
                        ),
                        and_(
                            Transaction.competence.is_(None),
                            Transaction.booked_at >= start,
                            Transaction.booked_at < end,
                        ),
                    ),
                ),
                and_(
                    or_(
                        Account.account_type != "credit_card",
                        Account.account_type.is_(None),
                    ),
                    Transaction.booked_at >= start,
                    Transaction.booked_at < end,
                ),
            ),
        )
        .order_by(Transaction.booked_at, Transaction.created_at, Transaction.id)
    ).all()
    workbook_signatures = {
        _expense_signature(transaction, account_type)
        for transaction, _category, account_type, document_type in raw_rows
        if document_type == "financial_plan_workbook"
        and transaction.canonical_status != "supporting"
    }
    result: list[tuple[Transaction, str]] = []
    ignored: list[Transaction] = []
    for transaction, category_name, account_type, document_type in raw_rows:
        if transaction.canonical_status == "supporting":
            ignored.append(transaction)
            continue
        if transaction.canonical_status in {"canonical", "distinct"}:
            result.append((transaction, str(category_name or "Revisar")))
            continue
        signature = _expense_signature(transaction, account_type)
        if document_type != "financial_plan_workbook" and signature in workbook_signatures:
            ignored.append(transaction)
            continue
        result.append((transaction, str(category_name or "Revisar")))
    return result, ignored


def _consolidated_expenses(
    db: Session,
    household_id: str,
    start: date,
    end: date,
) -> tuple[list[tuple[Transaction, str]], int]:
    """Prefer every matching workbook row over copies from individual imports.

    The workbook is the authoritative ledger even when its copy is excluded from
    spending or classified as a transfer/reconciliation.  Otherwise a later PDF
    import can reintroduce that same cash movement as an ordinary expense.
    """
    rows, ignored = _consolidated_transactions(db, household_id, start, end)
    result = [
        (transaction, category_name)
        for transaction, category_name in rows
        if transaction.transaction_type in {"expense", "refund"} and not transaction.excluded
    ]
    duplicates_ignored = sum(
        transaction.transaction_type in {"expense", "refund"} and not transaction.excluded
        for transaction in ignored
    )
    return result, duplicates_ignored


def _account_display(account: Account | None) -> str:
    if not account:
        return "Sem conta identificada"
    parts = [account.institution.strip(), account.name.strip()]
    label = " • ".join(dict.fromkeys(part for part in parts if part)) or "Conta sem nome"
    if account.last_four:
        label += f" • final {account.last_four}"
    return label


def _operational_cash_flow(rows: list[tuple[Transaction, str]]) -> dict:
    """Separate real income, bank outgoings and card commitments.

    Card purchases are commitments, not immediate withdrawals from a bank account.
    Income marked as excluded or outside the Receitas category is not real household
    income.  Excluded expenses may still represent actual cash outgoings (for example,
    a patrimonial payment kept outside the monthly consumption cap).
    """
    accounts: dict[str, dict[str, object]] = {}
    for transaction, category_name in rows:
        if transaction.possible_duplicate:
            continue
        if transaction.transaction_type in {"transfer", "reconciliation"}:
            continue
        if category_name in {"Transferência interna", "Transferência patrimonial", "Conciliação"}:
            continue
        account = transaction.account
        key = account.id if account else "unidentified"
        item = accounts.setdefault(
            key,
            {
                "account_id": account.id if account else None,
                "account": _account_display(account),
                "institution": account.institution if account else "",
                "account_type": account.account_type if account else "other",
                "cash_in": Decimal("0"),
                "gross_out": Decimal("0"),
                "refunds": Decimal("0"),
            },
        )
        amount = Decimal(transaction.amount)
        if transaction.transaction_type == "income" and amount > 0 and not transaction.excluded:
            # FAMILY_FINANCE_DASHBOARD_CASH_IN_V3
            # Cash flow follows the bank fact, independently from whether the inflow is
            # operating income (salary) or a non-operating receipt (e.g. debt repayment).
            item["cash_in"] += amount
        elif transaction.transaction_type == "refund" and amount > 0 and not transaction.excluded:
            item["refunds"] += amount
            if item["account_type"] != "credit_card":
                # Bank reimbursement is a real entry. Card-side refund instead reduces
                # card spending and does not create bank cash.
                item["cash_in"] += amount
        elif transaction.transaction_type in {"expense", "refund"} and amount < 0:
            item["gross_out"] += abs(amount)

    serialized = []
    for item in accounts.values():
        cash_in = money(item["cash_in"])
        refunds = money(item["refunds"])
        cash_out = money(
            max(Decimal("0"), item["gross_out"] - refunds)
            if item["account_type"] == "credit_card"
            else item["gross_out"]
        )
        if cash_in == 0 and cash_out == 0 and refunds == 0:
            continue
        serialized.append(
            {
                "account_id": item["account_id"],
                "account": item["account"],
                "institution": item["institution"],
                "account_type": item["account_type"],
                "cash_in": decimal_value(cash_in),
                "cash_out": decimal_value(cash_out),
                "bank_cash_out": decimal_value(
                    Decimal("0") if item["account_type"] == "credit_card" else cash_out
                ),
                "card_spending": decimal_value(
                    cash_out if item["account_type"] == "credit_card" else Decimal("0")
                ),
                "refunds": decimal_value(refunds),
                "net": decimal_value(money(cash_in - cash_out)),
            }
        )
    serialized.sort(key=lambda item: (item["account_type"], item["account"]))
    cash_in = money(sum((Decimal(str(item["cash_in"])) for item in serialized), Decimal("0")))
    bank_cash_out = money(
        sum((Decimal(str(item["bank_cash_out"])) for item in serialized), Decimal("0"))
    )
    card_spending = money(
        sum((Decimal(str(item["card_spending"])) for item in serialized), Decimal("0"))
    )
    return {
        "cash_in": decimal_value(cash_in),
        "cash_out": decimal_value(money(bank_cash_out + card_spending)),
        "bank_cash_out": decimal_value(bank_cash_out),
        "card_spending": decimal_value(card_spending),
        "accounts": serialized,
    }


def _liquidity_settlement(
    starting_balance: Decimal,
    operational_result: Decimal,
    emergency_floor: Decimal,
) -> dict[str, Decimal]:
    """Apply the household result to the central liquidity account.

    The Privilège balance is operational cash, not a protected pocket. A negative
    result consumes the whole balance when necessary; the emergency floor is a
    warning target and never makes unavailable money appear to remain invested.
    """
    starting = money(max(Decimal("0"), starting_balance))
    result = money(operational_result)
    floor = money(max(Decimal("0"), emergency_floor))
    deposit = money(max(Decimal("0"), result))
    deficit = money(max(Decimal("0"), -result))
    withdrawal = money(min(starting, deficit))
    closing = money(starting + deposit - withdrawal)
    uncovered = money(max(Decimal("0"), deficit - withdrawal))
    return {
        "starting_balance": starting,
        "closing_balance": closing,
        "deposit": deposit,
        "withdrawal": withdrawal,
        "uncovered_deficit": uncovered,
        "floor_gap": money(closing - floor),
    }


def _large_entry_threshold(profile: FinancialProfile) -> Decimal:
    """Require a second acknowledgement for values likely to be tests or simulations."""
    return max(Decimal("5000"), money(profile.monthly_cash_cap * Decimal("2")))
def _add_months_preserving_day(value: date, months: int) -> date:
    target = add_months(value.replace(day=1), months)
    return target.replace(day=min(value.day, monthrange(target.year, target.month)[1]))


def _obligation_dates(item: Obligation) -> list[date]:
    dates = []
    for occurrence in range(max(1, item.occurrence_count)):
        dates.append(_add_months_preserving_day(item.due_date, occurrence * item.recurrence_months))
        if item.recurrence_months == 0:
            break
    return dates


def _obligation_timing(item: Obligation, reference: date | None = None) -> dict:
    reference = reference or date.today()
    dates = _obligation_dates(item)
    next_due = next((due for due in dates if due >= reference), dates[-1])
    days = (next_due - reference).days
    if days < 0:
        level = "overdue"
        label = f"Cronograma venceu há {abs(days)} dia(s)"
    elif days == 0:
        level = "urgent"
        label = "Vence hoje"
    elif days <= 7:
        level = "urgent"
        label = f"Vence em {days} dia(s)"
    elif days <= 30:
        level = "soon"
        label = f"Vence em {days} dias"
    else:
        level = "scheduled"
        label = f"Vence em {days} dias"
    return {
        "next_due_date": next_due,
        "days_until_due": days,
        "alert_level": level,
        "alert_label": label,
    }


def _obligation_rows(
    db: Session,
    household_id: str,
    reference: date | None = None,
    *,
    include_paid: bool = False,
) -> list[dict]:
    # FAMILY_FINANCE_OBLIGATION_PAYMENT_V8
    query = select(Obligation).where(
        Obligation.household_id == household_id,
        Obligation.active.is_(True),
    )
    if not include_paid:
        query = query.where(Obligation.status == "pending")
    rows = db.scalars(query.order_by(Obligation.due_date)).all()

    result = []
    for item in rows:
        if item.status == "paid":
            timing = {
                "next_due_date": item.due_date,
                "days_until_due": 0,
                "alert_level": "paid",
                "alert_label": (
                    f"Pago em {item.paid_at.strftime('%d/%m/%Y')}"
                    if item.paid_at
                    else "Pago"
                ),
            }
        else:
            timing = _obligation_timing(item, reference)
        result.append(
            {
                "id": item.id,
                "name": item.name,
                "due_date": item.due_date,
                "amount": decimal_value(item.amount),
                "recurrence_months": item.recurrence_months,
                "occurrence_count": item.occurrence_count,
                "category": item.category,
                "status": item.status,
                "paid_at": item.paid_at,
                "paid_transaction_id": item.paid_transaction_id,
                "payment_transaction_created": item.payment_transaction_created,
                **timing,
            }
        )
    result.sort(key=lambda row: (row["status"] == "paid", row["next_due_date"]))
    return result


def _brl(value: Decimal | float | int) -> str:
    formatted = f"{money(value):,.2f}"
    return "R$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")


MONTHS_PT = {
    "JANEIRO": 1,
    "FEVEREIRO": 2,
    "MARCO": 3,
    "ABRIL": 4,
    "MAIO": 5,
    "JUNHO": 6,
    "JULHO": 7,
    "AGOSTO": 8,
    "SETEMBRO": 9,
    "OUTUBRO": 10,
    "NOVEMBRO": 11,
    "DEZEMBRO": 12,
}

MONTH_LABELS_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Março",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}


def _advisor_month(message: str) -> date:
    normalized = normalize_description(message)
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    year = int(year_match.group(1)) if year_match else date.today().year
    for name, number in MONTHS_PT.items():
        if re.search(rf"\b{name}\b", normalized):
            return date(year, number, 1)
    return date.today().replace(day=1)


def _advisor_amount(message: str) -> Decimal | None:
    lowered = message.lower()
    number = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?!\d)"
    patterns = (
        rf"(?:custa|custando|valor(?:\s+de)?|compra(?:\s+de)?|pagar)\s*(?:r\$\s*)?{number}\s*(mil|k)?",
        rf"(?:comprar|compra|adquirir).{{0,80}}?\b(?:por|de|custa|custando|valor(?:\s+de)?)\s*(?:r\$\s*)?{number}\s*(mil|k)?",
        rf"r\$\s*{number}\s*(mil|k)?",
        rf"{number}\s*(mil|k)\b",
    )
    matches = [candidate for pattern in patterns if (candidate := re.search(pattern, lowered))]
    if not matches:
        return None
    match = min(matches, key=lambda candidate: candidate.start())
    raw = match.group(1)
    suffix = match.group(2)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    value = Decimal(raw)
    if suffix:
        value *= Decimal("1000")
    return money(value)


PURCHASE_KIND_RULES = (
    (
        "personal_care",
        (
            "ESMALTE",
            "BATOM",
            "MAQUIAGEM",
            "COSMETICO",
            "PERFUME",
            "SHAMPOO",
            "CONDICIONADOR",
            "MANICURE",
        ),
    ),
    (
        "equipment",
        (
            "ENXADA",
            "ROCADEIRA",
            "FURADEIRA",
            "FERRAMENTA",
            "EQUIPAMENTO",
            "MAQUINA",
            "MOTOSSERRA",
            "SOPRADOR",
            "TRATOR",
            "BETONEIRA",
        ),
    ),
    ("vehicle", ("CARRO", "MOTO", "CAMINHONETE", "VEICULO", "AUTOMOVEL")),
    ("property", ("IMOVEL", "TERRENO", "CHALE", "CONSTRUCAO", "REFORMA")),
    (
        "electronics",
        (
            "VIDEOGAME",
            "VIDEO GAME",
            "CONSOLE",
            "CELULAR",
            "NOTEBOOK",
            "COMPUTADOR",
            "TELEVISOR",
            "TABLET",
        ),
    ),
    ("clothing", ("ROUPA", "TENIS", "SAPATO", "BOLSA", "VESTIDO", "CAMISA")),
    (
        "consumable",
        ("MERCADO", "ALIMENTO", "COMIDA", "BEBIDA", "LIMPEZA", "DESCARTAVEL"),
    ),
)

PURCHASE_KIND_DETAILS = {
    "personal_care": ("item de cuidados pessoais", "Estética e beleza"),
    "equipment": ("ferramenta ou equipamento", "Operação da chácara"),
    "vehicle": ("veículo", "Transporte"),
    "property": ("imóvel ou melhoria estrutural", "Compras, casa e vestuário"),
    "electronics": ("eletrônico ou bem durável", "Compras, casa e vestuário"),
    "clothing": ("item pessoal ou de vestuário", "Compras, casa e vestuário"),
    "consumable": ("item de consumo recorrente", "Mercado e itens domésticos"),
    "general": ("compra não classificada", "Pix, ajudas e outros"),
}


def _advisor_purchase_kind(message: str) -> str:
    normalized = normalize_description(message)
    for kind, keywords in PURCHASE_KIND_RULES:
        if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords):
            return kind
    return "general"


def _advisor_purchase_context(
    message: str,
    purchase_amount: Decimal,
    cash_cap: Decimal,
    *,
    total_cost: Decimal | None = None,
    installments: int = 1,
) -> dict[str, object]:
    kind = _advisor_purchase_kind(message)
    label, suggested_category = PURCHASE_KIND_DETAILS[kind]
    analyzed_cost = money(total_cost if total_cost is not None else purchase_amount)
    cap = max(Decimal("1"), cash_cap)
    quick_limit = max(Decimal("50"), money(cap * Decimal("0.02")))
    ratio = analyzed_cost / cap
    if installments == 1 and analyzed_cost <= quick_limit:
        depth = "quick"
    elif ratio >= Decimal("0.20") or installments >= 4:
        depth = "full"
    else:
        depth = "standard"
    return {
        "kind": kind,
        "label": label,
        "suggested_category": suggested_category,
        "analysis_depth": depth,
        "budget_ratio": decimal_value(ratio),
        "quick_limit": decimal_value(quick_limit),
        "show_commitment_schedule": depth == "full" or installments > 1,
    }


def _advisor_payment(
    message: str,
    purchase_amount: Decimal,
    *,
    assume_cash: bool = False,
) -> dict[str, object]:
    normalized = normalize_description(message)
    installment_match = re.search(r"\b(\d{1,3})\s*(?:X|VEZ(?:ES)?|PARCELAS?)\b", normalized)
    is_cash = bool(re.search(r"\bA\s+VISTA\b", normalized))
    if not installment_match and not is_cash:
        if assume_cash:
            is_cash = True
        else:
            return {
                "complete": False,
                "question": (
                    "Essa compra será à vista ou parcelada? Se for parcelada, informe a "
                    "quantidade de parcelas e se há juros."
                ),
            }
    installments = int(installment_match.group(1)) if installment_match else 1
    if installments < 1 or installments > 120:
        return {
            "complete": False,
            "question": "Informe uma quantidade de parcelas entre 1 e 120.",
        }
    interest_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", message)
    without_interest = "SEM JUROS" in normalized
    with_interest = "COM JUROS" in normalized or interest_match is not None
    if installments > 1 and not without_interest and not with_interest:
        return {
            "complete": False,
            "question": (
                "O parcelamento tem juros? Se tiver, informe a taxa mensal; por exemplo, 2% ao mês."
            ),
        }
    if installments > 1 and "COM JUROS" in normalized and interest_match is None:
        return {
            "complete": False,
            "question": "Qual é a taxa de juros mensal do parcelamento?",
        }
    monthly_rate = Decimal("0")
    if interest_match:
        monthly_rate = Decimal(interest_match.group(1).replace(",", ".")) / Decimal("100")
        if monthly_rate > Decimal("0.30"):
            return {
                "complete": False,
                "question": "A taxa parece muito alta. Confirme a taxa mensal do parcelamento.",
            }
    monthly_payment, total_cost = amortized_installment_payment(
        purchase_amount, installments, monthly_rate
    )
    return {
        "complete": True,
        "mode": "cash" if installments == 1 else "installments",
        "assumed_cash": bool(assume_cash and not re.search(r"\bA\s+VISTA\b", normalized)),
        "installments": installments,
        "monthly_interest_rate": decimal_value(monthly_rate),
        "monthly_payment": decimal_value(monthly_payment),
        "total_cost": decimal_value(total_cost),
    }


def _advisor_reflection_context(payload: AdvisorRequest) -> str | None:
    if not any(
        item.role == "assistant" and "COMPRA CONSCIENTE" in normalize_description(item.content)
        for item in payload.history
    ):
        return None
    normalized = normalize_description(payload.message)
    reflection_signals = (
        "PRECISO",
        "USAR",
        "USO",
        "VEZ",
        "ALUGAR",
        "ALUGUEL",
        "SERVICO",
        "TRABALHO",
        "CHACARA",
        "MANUTENCAO",
        "GUARDAR",
        "URGENTE",
        "VONTADE",
        "ANO",
        "MESES",
    )
    return payload.message.strip() if any(word in normalized for word in reflection_signals) else None


def _advisor_conversation_message(payload: AdvisorRequest) -> str:
    current = payload.message
    normalized = normalize_description(current)
    has_payment_detail = bool(
        re.search(r"\b(\d{1,3})\s*(?:X|VEZ(?:ES)?|PARCELAS?)\b", normalized)
        or "A VISTA" in normalized
        or "JUROS" in normalized
    )
    reflection_context = _advisor_reflection_context(payload)
    starts_new_purchase = any(word in normalized.split() for word in {"COMPRA", "COMPRAR", "ADQUIRIR"})
    if reflection_context and not starts_new_purchase:
        prior_context = []
        for item in payload.history:
            candidate = normalize_description(item.content)
            if item.role != "user":
                continue
            if (
                any(word in candidate.split() for word in {"COMPRA", "COMPRAR", "CUSTA", "ADQUIRIR"})
                or re.search(r"\b(\d{1,3})\s*(?:X|VEZ(?:ES)?|PARCELAS?)\b", candidate)
                or ("A VISTA" in candidate or "JUROS" in candidate)
            ):
                prior_context.append(item.content)
        if prior_context:
            return ". ".join((*prior_context, f"Contexto de necessidade: {reflection_context}"))
    if _advisor_amount(current) is not None:
        return current
    if not has_payment_detail:
        return current
    for item in reversed(payload.history):
        candidate = normalize_description(item.content)
        if item.role == "user" and any(
            word in candidate.split() for word in {"COMPRA", "COMPRAR", "CUSTA", "ADQUIRIR"}
        ):
            return f"{item.content}. {current}"
    return current


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Apenas administradores gerenciam acessos")


def audit(
    db: Session,
    user: User,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict | None = None,
    *,
    before_state: dict | None = None,
    after_state: dict | None = None,
    reason: str | None = None,
    trace_id: str | None = None,
    source: str = "application",
    request_id: str | None = None,
) -> None:
    db.add(
        AuditEvent(
            household_id=user.household_id,
            user_id=user.id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            before_state=_audit_state(before_state),
            after_state=_audit_state(after_state),
            reason=reason,
            trace_id=trace_id,
            source=source,
            request_id=request_id,
        )
    )


def _audit_state(value: dict | None) -> dict | None:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def category_for(db: Session, household_id: str, name: str) -> Category:
    clean_name = " ".join(name.split())[:100] or "Revisar"
    category = db.scalar(
        select(Category).where(
            Category.household_id == household_id,
            func.lower(Category.name) == clean_name.lower(),
        )
    )
    if category:
        return category
    category = Category(household_id=household_id, name=clean_name)
    db.add(category)
    db.flush()
    return category


def profile_for(db: Session, household_id: str) -> FinancialProfile:
    profile = db.scalar(select(FinancialProfile).where(FinancialProfile.household_id == household_id))
    if not profile:
        profile = FinancialProfile(household_id=household_id)
        db.add(profile)
        db.flush()
    return profile


def _capture_response(item: CaptureDraft) -> dict:
    try:
        proposals = json.loads(item.proposal_json or "[]")
    except json.JSONDecodeError:
        proposals = []
    try:
        result = json.loads(item.result_json) if item.result_json else None
    except json.JSONDecodeError:
        result = None
    job = item.processing_job
    job_info = (
        {
            "job_type": job.job_type,
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "error_code": job.error_code,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        }
        if job
        else None
    )
    return {
        "id": item.id,
        "source_type": item.source_type,
        "detected_type": item.detected_type,
        "status": item.status,
        "processor": item.processor,
        "confidence": decimal_value(item.confidence),
        "notes": item.notes,
        "items": proposals,
        "result": result,
        "file_name": item.document.original_name if item.document else None,
        "created_at": item.created_at,
        "confirmed_at": item.confirmed_at,
        "job": job_info,
    }


def _capture_signed_amount(movement_type: str, amount: Decimal) -> Decimal:
    if movement_type in {"income", "redemption", "refund", "reconciliation"}:
        return abs(amount)
    return -abs(amount)


def _enrich_capture_items(
    db: Session,
    household_id: str,
    items: list[dict],
    requested_account_id: str | None,
) -> list[dict]:
    accounts = db.scalars(
        select(Account).where(Account.household_id == household_id, Account.active.is_(True))
    ).all()
    accounts_by_id = {item.id: item for item in accounts}
    default_account = accounts_by_id.get(requested_account_id or "")
    if requested_account_id and not default_account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    if not default_account and len(accounts) == 1:
        default_account = accounts[0]
    categories = db.scalars(select(Category).where(Category.household_id == household_id)).all()
    categories_by_name = {item.name.casefold(): item for item in categories}

    enriched: list[dict] = []
    for original in items:
        proposal = dict(original)
        proposal["warnings"] = list(proposal.get("warnings") or [])
        if proposal.get("kind") != "transaction":
            enriched.append(proposal)
            continue
        account = default_account
        if not account and proposal.get("card_last_four"):
            card_matches = [item for item in accounts if item.last_four == proposal.get("card_last_four")]
            if len(card_matches) == 1:
                account = card_matches[0]
        if not account:
            description = normalize_description(str(proposal.get("description") or ""))
            mentioned = [
                item
                for item in accounts
                if any(
                    len(alias) >= 3 and alias in description
                    for alias in (
                        normalize_description(item.name),
                        normalize_description(item.institution),
                    )
                )
            ]
            if "CARTAO" in description:
                mentioned = [item for item in mentioned if item.account_type == "credit_card"]
            elif "CONTA" in description:
                mentioned = [item for item in mentioned if item.account_type == "checking"]
            if len(mentioned) == 1:
                account = mentioned[0]
        proposal["account_id"] = account.id if account else None
        proposal["account_name"] = _account_display(account) if account else None
        proposal["requires_account"] = account is None
        category_name = str(proposal.get("category_name") or "Revisar")
        category = categories_by_name.get(category_name.casefold())
        proposal["category_id"] = category.id if category else None
        proposal["new_category"] = category is None
        movement_type = str(proposal.get("movement_type") or "expense")
        if account and proposal.get("booked_at") and proposal.get("amount"):
            try:
                signed_amount = _capture_signed_amount(movement_type, Decimal(str(proposal["amount"])))
                parsed = ParsedTransaction(
                    booked_at=date.fromisoformat(str(proposal["booked_at"])),
                    description=str(proposal.get("description") or "Lançamento inteligente"),
                    amount=signed_amount,
                    source_line=int(proposal.get("source_line") or 1),
                    card_last_four=proposal.get("card_last_four"),
                    installment_current=proposal.get("installment_current"),
                    installment_total=proposal.get("installment_total"),
                )
                fingerprint = transaction_fingerprint(account.id, parsed, account.owner_label)
                duplicate = bool(
                    db.scalar(
                        select(Transaction.id).where(
                            Transaction.household_id == household_id,
                            Transaction.fingerprint == fingerprint,
                        )
                    )
                )
            except (TypeError, ValueError):
                duplicate = False
            proposal["possible_duplicate"] = duplicate
            if duplicate:
                proposal["selected"] = False
                proposal["warnings"].append(
                    "Possível duplicidade: já existe um lançamento idêntico nessa conta"
                )
        enriched.append(proposal)
    return enriched


@router.get("/public/status")
def public_status(db: Session = Depends(get_db)) -> dict:
    return {"configured": bool(db.scalar(select(func.count(User.id))))}


# ---------------------------------------------------------------------------
# Fase 4 -- MFA local TOTP (docs/WORK_ORDER_LOCAL_MFA_TOTP.md)
#
# Auth state machine:
#
#   ANONYMOUS --username+senha válida--> PASSWORD_VERIFIED
#     --sem fator confirmado----> MFA_ENROLL_PENDING (ffp_mfa_pending, purpose=enroll)
#     --fator já confirmado----> MFA_VERIFY_PENDING  (ffp_mfa_pending, purpose=verify)
#   MFA_VERIFY_PENDING/ENROLL_PENDING --TOTP/recovery válido--> FULLY_AUTHENTICATED (ffp_session)
#
# `ffp_mfa_pending` never authenticates any endpoint that depends on
# `get_current_user`: it is a distinctly named cookie read by a distinct
# FastAPI dependency (`get_enroll_pending_user`/`get_verify_pending_user`),
# so no route protected by the normal dependency can ever be reached with
# only a pending token, independent of any bug in this module's own logic.
# ---------------------------------------------------------------------------


def _mfa_factor_for(db: Session, user: User) -> MfaFactor | None:
    return db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id))


def _aware_utc(value: datetime | None) -> datetime | None:
    """Normalize a `DateTime(timezone=True)` MFA column value read back
    from the database for a Python-level comparison against
    `datetime.now(UTC)`.

    PostgreSQL always returns a timezone-aware value for this column type;
    SQLite (used in tests) always returns a naive one regardless -- every
    value this app ever writes to such a column is already UTC
    (`datetime.now(UTC)`), so a naive value is safely assumed to already be
    UTC. Same idiom as `app.services.capture_worker._aware_utc`.
    """

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _mfa_locked(factor: MfaFactor, now: datetime) -> bool:
    locked_until = _aware_utc(factor.locked_until)
    return bool(locked_until and locked_until > now)


def _mfa_pending_expired(factor: MfaFactor, now: datetime) -> bool:
    expires_at = _aware_utc(factor.pending_setup_expires_at)
    return bool(expires_at and expires_at <= now)


def _raise_already_enrolled() -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="MFA já configurado para este usuário. Use a reconfiguração para trocar o autenticador.",
    )


def _stage_pending_secret(
    db: Session, user: User, factor: MfaFactor | None, *, require_unconfirmed: bool = False
) -> tuple[MfaFactor, str, bool]:
    """Return `(factor, plaintext_secret, generated_new)` for an enrollment
    or reconfiguration in progress.

    Reuses the existing pending secret while it has not expired, so a user
    who reloads the QR screen keeps scanning the same code instead of
    invalidating their authenticator app's in-progress setup; mints a fresh
    one otherwise ("Setup expirado não ativa MFA e deve exigir novo
    segredo. Não reutilizar indefinidamente segredo de enrollment
    abandonado.").

    `require_unconfirmed=True` (the fresh-enrollment callers) fails closed
    with 409 instead of staging anything when `factor.confirmed_at` is
    already set -- only `/auth/mfa/reconfigure/*` (which requires a full
    session plus current password and current second factor) may replace a
    confirmed factor. Without this, a stale `ffp_mfa_pending(enroll)`
    cookie issued *before* the user's first enrollment (e.g. a second
    device/tab that validated the password first but never finished
    enrollment) would still pass `get_enroll_pending_user` after a
    different client completed enrollment in the meantime, and could stage
    -- then confirm -- a brand-new secret over the now-confirmed factor,
    bypassing the "Reconfigurar autenticador" contract entirely (no current
    password/TOTP re-check). The guard itself is an atomic
    `UPDATE ... WHERE confirmed_at IS NULL`, not a Python-level check
    followed by a separate write, so a factor confirmed by a concurrent
    request between this function's read and its write still loses the
    race instead of silently overwriting the just-confirmed secret.
    """

    now = datetime.now(UTC)
    if factor is None:
        factor = MfaFactor(user_id=user.id)
        db.add(factor)
        db.flush()
    elif require_unconfirmed and factor.confirmed_at is not None:
        _raise_already_enrolled()
    if (
        factor.pending_secret_encrypted
        and factor.pending_setup_expires_at
        and not _mfa_pending_expired(factor, now)
    ):
        return factor, mfa_service.decrypt_secret(factor.pending_secret_encrypted), False
    secret = mfa_service.generate_totp_secret()
    stmt = update(MfaFactor).where(MfaFactor.id == factor.id)
    if require_unconfirmed:
        stmt = stmt.where(MfaFactor.confirmed_at.is_(None))
    result = db.execute(
        stmt.values(
            pending_secret_encrypted=mfa_service.encrypt_secret(secret),
            pending_setup_started_at=now,
            # A brand-new secret has no history of its own -- any prior
            # pending secret's accepted timestep is meaningless against it.
            pending_setup_expires_at=mfa_pending_expires_at(),
            pending_last_accepted_timestep=None,
        )
    )
    if result.rowcount != 1:
        if require_unconfirmed:
            # Lost the race against a concurrent confirm that confirmed
            # this exact factor between the read above and this UPDATE.
            _raise_already_enrolled()
        # `require_unconfirmed=False` only reaches here if the factor row
        # itself vanished mid-request (the local break-glass CLI deletes
        # it outright) -- unrelated to "already enrolled", so raise a
        # distinct, accurate error instead of the misleading one above.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Fator MFA foi removido por um reset. Faça login novamente.",
        )
    db.refresh(factor)
    return factor, secret, True


def _raise_mfa_locked() -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Muitas tentativas inválidas. Tente novamente em alguns minutos.",
    )


def _register_mfa_failure(db: Session, factor: MfaFactor) -> MfaFactor:
    """Atomically increment `failed_attempts` and, once it reaches the
    configured threshold, set `locked_until` -- both computed inside the
    `UPDATE` itself (SQL-side arithmetic and `CASE`, not a Python
    read-modify-write), so two concurrent invalid attempts against the
    same factor cannot both read the same pre-image and lose an increment
    (same compare-free counter idiom as `app.services.capture_worker`).

    Flushes and refreshes `factor` from the database *without* committing:
    every MFA route commits exactly once, at the very end, so a failed
    attempt's rate-limit bookkeeping and its `AuditEvent` land in the same
    transaction instead of two separate commits that could diverge on a
    crash between them.
    """

    settings = get_settings()
    now = datetime.now(UTC)
    lock_at = now + timedelta(seconds=settings.mfa_rate_limit_lockout_seconds)
    db.execute(
        update(MfaFactor)
        .where(MfaFactor.id == factor.id)
        .values(
            failed_attempts=MfaFactor.failed_attempts + 1,
            locked_until=case(
                (MfaFactor.failed_attempts + 1 >= settings.mfa_rate_limit_max_attempts, lock_at),
                else_=MfaFactor.locked_until,
            ),
        )
    )
    db.flush()
    db.refresh(factor)
    return factor


def _reset_mfa_lockout(factor: MfaFactor) -> None:
    factor.failed_attempts = 0
    factor.locked_until = None


def _accept_timestep(db: Session, factor: MfaFactor, timestep: int, *, pending: bool) -> bool:
    """Atomic anti-replay accept.

    Succeeds only if `timestep` is strictly newer than whatever the target
    column currently is (`NULL` counts as older than anything). Two
    concurrent requests validating the same code race this single `UPDATE
    ... WHERE`; the loser's predicate stops matching the instant the
    winner's write lands, so `rowcount == 0` unambiguously means "this
    exact timestep is already spent" -- by this request or a concurrent
    one (Work Order: "duas validações concorrentes do mesmo código
    resultam em no máximo uma aceitação"). Portable across SQLite and
    PostgreSQL: it relies only on one `UPDATE` statement's WHERE being
    evaluated atomically with its write, not on dialect-specific locking.

    `pending` selects which of the two independent counters this check
    guards -- see `MfaFactor.pending_last_accepted_timestep`'s docstring
    for why the active and pending secrets must never share one.
    """

    column = MfaFactor.pending_last_accepted_timestep if pending else MfaFactor.last_accepted_timestep
    result = db.execute(
        update(MfaFactor)
        .where(MfaFactor.id == factor.id, or_(column.is_(None), column < timestep))
        .values({column: timestep})
    )
    return result.rowcount == 1


def _verify_totp_challenge(
    db: Session, factor: MfaFactor, secret_encrypted: str, code: str, *, pending: bool
) -> bool:
    """Verify `code` against `secret_encrypted`, enforcing anti-replay
    (against the active or pending counter, per `pending`) and updating
    the shared rate-limit counters in-memory (the caller commits). Never
    raises on a wrong code -- each route phrases its own HTTP response so
    enrollment-confirm, login-verify and reconfiguration-confirm can each
    audit/respond appropriately."""

    secret = mfa_service.decrypt_secret(secret_encrypted)
    timestep = mfa_service.verify_totp(secret, code)
    if timestep is None or not _accept_timestep(db, factor, timestep, pending=pending):
        _register_mfa_failure(db, factor)
        return False
    _reset_mfa_lockout(factor)
    return True


def _consume_recovery_code(db: Session, factor: MfaFactor, candidate: str) -> bool:
    """Atomically consume one unused recovery code matching `candidate`.
    The `UPDATE ... WHERE used_at IS NULL` predicate means a code raced by
    two concurrent requests is consumed by at most one of them, and an
    already-used code can never authenticate again."""

    code_hash = mfa_service.hash_recovery_code(candidate)
    result = db.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.factor_id == factor.id,
            MfaRecoveryCode.code_hash == code_hash,
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    if result.rowcount != 1:
        _register_mfa_failure(db, factor)
        return False
    _reset_mfa_lockout(factor)
    return True


def _verify_mfa_challenge(db: Session, factor: MfaFactor, payload) -> bool:
    """Shared TOTP-or-recovery-code dispatch for `payload` objects shaped
    like `MfaChallengeRequest`/`MfaReauthRequest` (both carry mutually
    exclusive `code`/`recovery_code` fields, enforced by their own
    validators)."""

    if payload.code:
        if not factor.secret_encrypted:
            return False
        return _verify_totp_challenge(db, factor, factor.secret_encrypted, payload.code, pending=False)
    return _consume_recovery_code(db, factor, payload.recovery_code or "")


def _issue_recovery_codes(db: Session, factor: MfaFactor) -> list[str]:
    """Invalidate every existing recovery code for `factor` (marking it
    used, never deleting it -- keeps a row for count/audit purposes) and
    persist a fresh set of 10, returning the plaintext once. Per Work
    Order: "regenerar conjunto invalida todos os códigos anteriores.\""""

    now = datetime.now(UTC)
    db.execute(
        update(MfaRecoveryCode)
        .where(MfaRecoveryCode.factor_id == factor.id, MfaRecoveryCode.used_at.is_(None))
        .values(used_at=now)
    )
    plaintext_codes = mfa_service.generate_recovery_codes()
    for code in plaintext_codes:
        db.add(MfaRecoveryCode(factor_id=factor.id, code_hash=mfa_service.hash_recovery_code(code)))
    db.flush()
    return plaintext_codes


def _activate_pending_secret(db: Session, factor: MfaFactor) -> bool:
    """Copy the staged `pending_secret_encrypted` over the active
    `secret_encrypted`, clear the staging columns, and reset the replay/
    rate-limit counters for the newly active factor.

    Returns whether this was a *reconfiguration* (an active secret already
    existed) as opposed to the very first enrollment -- callers use this to
    choose between the `mfa.enrolled` and `mfa.reconfigured` audit events.
    The previous secret is never referenced again after this call, but
    nothing here revokes sessions; only the reconfiguration route (which
    already required a full session plus re-authentication before staging
    a new secret) does that, matching "nunca invalidar o fator antigo
    antes da confirmação do novo."
    """

    was_reconfigure = factor.secret_encrypted is not None
    factor.secret_encrypted = factor.pending_secret_encrypted
    factor.confirmed_at = datetime.now(UTC)
    factor.pending_secret_encrypted = None
    factor.pending_setup_started_at = None
    factor.pending_setup_expires_at = None
    factor.last_accepted_timestep = None
    factor.pending_last_accepted_timestep = None
    factor.failed_attempts = 0
    factor.locked_until = None
    return was_reconfigure


def _recovery_codes_remaining(db: Session, factor: MfaFactor) -> int:
    return int(
        db.scalar(
            select(func.count(MfaRecoveryCode.id)).where(
                MfaRecoveryCode.factor_id == factor.id, MfaRecoveryCode.used_at.is_(None)
            )
        )
        or 0
    )


@router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
def setup(payload: SetupRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(status_code=409, detail="O sistema já foi configurado")
    household = Household(name=payload.household_name)
    db.add(household)
    db.flush()
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user = User(
        household_id=household.id,
        name=payload.name,
        username=payload.username.lower(),
        password_hash=password_hash,
        is_admin=True,
    )
    db.add(user)
    for name, color, cash_cap, essential in DEFAULT_CATEGORIES:
        db.add(
            Category(
                household_id=household.id,
                name=name,
                color=color,
                cash_cap=cash_cap,
                essential=essential,
            )
        )
    db.add(FinancialProfile(household_id=household.id, projection_end=date(date.today().year + 1, 12, 1)))
    db.flush()
    audit(db, user, "system.setup", "household", household.id)
    db.commit()
    # Fase 4: mesmo o primeiro usuário (administrador) precisa concluir o
    # segundo fator antes de qualquer sessão completa -- "MFA obrigatório
    # para todo usuário ativo" não abre exceção para quem faz o setup
    # inicial. `/auth/setup` só cria a conta; o restante do fluxo é
    # idêntico ao enrollment obrigatório de `/auth/login`.
    set_pending_cookie(response, user.id, PENDING_PURPOSE_ENROLL)
    return {"configured": True, "mfa_required": True, "mode": "enroll"}


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username.lower(), User.active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    # Work Order decision 4: "Nenhuma sessão completa depois da senha."
    # `ffp_session` is never issued here -- only a short-TTL pending
    # cookie. `auth.login`/`mfa.authenticated` are audited once the second
    # factor actually succeeds (see `mfa_verify`/`mfa_enroll_confirm`),
    # since that is the point authentication is actually complete now.
    factor = _mfa_factor_for(db, user)
    if factor is None or factor.confirmed_at is None:
        set_pending_cookie(response, user.id, PENDING_PURPOSE_ENROLL)
        return {"mfa_required": True, "mode": "enroll"}
    set_pending_cookie(response, user.id, PENDING_PURPOSE_VERIFY)
    return {"mfa_required": True, "mode": "verify"}


@router.post("/auth/mfa/enroll/start")
def mfa_enroll_start(user: User = Depends(get_enroll_pending_user), db: Session = Depends(get_db)) -> dict:
    factor = _mfa_factor_for(db, user)
    factor, secret, generated_new = _stage_pending_secret(db, user, factor, require_unconfirmed=True)
    if generated_new:
        audit(db, user, "mfa.enrollment_started")
    db.commit()
    uri = mfa_service.provisioning_uri(secret, username=user.username)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_code": mfa_service.qr_code_data_uri(uri),
        "expires_at": factor.pending_setup_expires_at,
    }


@router.post("/auth/mfa/enroll/confirm")
def mfa_enroll_confirm(
    payload: MfaEnrollConfirmRequest,
    response: Response,
    user: User = Depends(get_enroll_pending_user),
    db: Session = Depends(get_db),
) -> dict:
    factor = _mfa_factor_for(db, user)
    if factor is not None and factor.confirmed_at is not None:
        # Defense in depth alongside `_stage_pending_secret`'s atomic guard:
        # a stale `ffp_mfa_pending(enroll)` cookie from before this user's
        # first enrollment must never be able to (re)activate a factor that
        # another client already confirmed in the meantime. Fails closed
        # without touching pending state or issuing a session -- only
        # `/auth/mfa/reconfigure/*` may replace a confirmed factor.
        _raise_already_enrolled()
    if factor is None or not factor.pending_secret_encrypted:
        raise HTTPException(
            status_code=400, detail="Nenhuma configuração de MFA pendente. Inicie o enrollment novamente."
        )
    now = datetime.now(UTC)
    if _mfa_locked(factor, now):
        audit(db, user, "mfa.rate_limited")
        db.commit()
        _raise_mfa_locked()
    if _mfa_pending_expired(factor, now):
        factor.pending_secret_encrypted = None
        factor.pending_setup_started_at = None
        factor.pending_setup_expires_at = None
        db.commit()
        raise HTTPException(status_code=400, detail="Configuração expirada. Gere um novo QR Code.")
    if not _verify_totp_challenge(db, factor, factor.pending_secret_encrypted, payload.code, pending=True):
        audit(db, user, "mfa.challenge_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Código inválido")
    was_reconfigure = _activate_pending_secret(db, factor)
    recovery_codes = _issue_recovery_codes(db, factor)
    audit(db, user, "mfa.reconfigured" if was_reconfigure else "mfa.enrolled")
    clear_pending_cookie(response)
    set_session_cookie(response, user)
    audit(db, user, "auth.login")
    db.commit()
    return {
        "user": {"id": user.id, "name": user.name, "username": user.username, "is_admin": user.is_admin},
        "recovery_codes": recovery_codes,
    }


@router.post("/auth/mfa/verify")
def mfa_verify(
    payload: MfaChallengeRequest,
    response: Response,
    user: User = Depends(get_verify_pending_user),
    db: Session = Depends(get_db),
) -> dict:
    factor = _mfa_factor_for(db, user)
    if factor is None or not factor.confirmed_at:
        # Unreachable through the normal flow: `login()` only ever issues a
        # "verify" pending cookie when a confirmed factor already exists.
        # Handled as a hard failure rather than silently falling back to
        # enrollment, so a forged/stale pending token can never grant any
        # state on its own.
        raise HTTPException(status_code=401, detail="MFA não configurado")
    now = datetime.now(UTC)
    if _mfa_locked(factor, now):
        audit(db, user, "mfa.rate_limited")
        db.commit()
        _raise_mfa_locked()
    if not _verify_mfa_challenge(db, factor, payload):
        audit(db, user, "mfa.challenge_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Código inválido")
    clear_pending_cookie(response)
    set_session_cookie(response, user)
    audit(db, user, "mfa.recovery_code_used" if payload.recovery_code else "mfa.authenticated")
    audit(db, user, "auth.login")
    db.commit()
    return {"id": user.id, "name": user.name, "username": user.username, "is_admin": user.is_admin}


@router.post("/auth/mfa/reconfigure/start")
def mfa_reconfigure_start(
    payload: MfaReauthRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Senha atual incorreta")
    factor = _mfa_factor_for(db, user)
    if factor is None or not factor.confirmed_at:
        raise HTTPException(status_code=400, detail="MFA ainda não foi configurado")
    now = datetime.now(UTC)
    if _mfa_locked(factor, now):
        audit(db, user, "mfa.rate_limited")
        db.commit()
        _raise_mfa_locked()
    if not _verify_mfa_challenge(db, factor, payload):
        audit(db, user, "mfa.challenge_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Segundo fator inválido")
    factor, secret, _generated_new = _stage_pending_secret(db, user, factor)
    db.commit()
    uri = mfa_service.provisioning_uri(secret, username=user.username)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_code": mfa_service.qr_code_data_uri(uri),
        "expires_at": factor.pending_setup_expires_at,
    }


@router.post("/auth/mfa/reconfigure/confirm")
def mfa_reconfigure_confirm(
    payload: MfaEnrollConfirmRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    factor = _mfa_factor_for(db, user)
    if factor is None or not factor.pending_secret_encrypted:
        raise HTTPException(status_code=400, detail="Nenhuma reconfiguração pendente. Inicie novamente.")
    now = datetime.now(UTC)
    if _mfa_locked(factor, now):
        audit(db, user, "mfa.rate_limited")
        db.commit()
        _raise_mfa_locked()
    if _mfa_pending_expired(factor, now):
        factor.pending_secret_encrypted = None
        factor.pending_setup_started_at = None
        factor.pending_setup_expires_at = None
        db.commit()
        raise HTTPException(status_code=400, detail="Configuração expirada. Gere um novo QR Code.")
    if not _verify_totp_challenge(db, factor, factor.pending_secret_encrypted, payload.code, pending=True):
        audit(db, user, "mfa.challenge_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Código inválido")
    _activate_pending_secret(db, factor)  # always a reconfiguration on this route
    recovery_codes = _issue_recovery_codes(db, factor)
    # "Incrementar session_version/revogar sessões" then "criar uma nova
    # sessão completa para a operação atual somente depois do fluxo
    # concluído": `bump_session_version` refreshes `user` in-memory too, so
    # the cookie issued right after embeds the *new* version instead of
    # the one this very request authenticated with.
    bump_session_version(db, user)
    audit(db, user, "mfa.reconfigured")
    set_session_cookie(response, user)
    db.commit()
    return {"recovery_codes": recovery_codes}


@router.post("/auth/mfa/recovery-codes/regenerate")
def mfa_recovery_codes_regenerate(
    payload: MfaReauthRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Senha atual incorreta")
    factor = _mfa_factor_for(db, user)
    if factor is None or not factor.confirmed_at:
        raise HTTPException(status_code=400, detail="MFA ainda não foi configurado")
    now = datetime.now(UTC)
    if _mfa_locked(factor, now):
        audit(db, user, "mfa.rate_limited")
        db.commit()
        _raise_mfa_locked()
    if not _verify_mfa_challenge(db, factor, payload):
        audit(db, user, "mfa.challenge_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Segundo fator inválido")
    recovery_codes = _issue_recovery_codes(db, factor)
    audit(db, user, "mfa.recovery_codes_regenerated")
    db.commit()
    return {"recovery_codes": recovery_codes}


@router.post("/auth/logout")
def logout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    clear_session_cookie(response)
    clear_pending_cookie(response)
    audit(db, user, "auth.logout")
    db.commit()
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    factor = _mfa_factor_for(db, user)
    return {
        "id": user.id,
        "name": user.name,
        "username": user.username,
        "is_admin": user.is_admin,
        "mfa_enabled": bool(factor and factor.confirmed_at),
        "mfa_recovery_codes_remaining": _recovery_codes_remaining(db, factor) if factor else 0,
    }


@router.post("/integrity/runs", status_code=status.HTTP_201_CREATED)
def create_integrity_run(
    payload: IntegrityRunRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    run_scope = IntegrityRunScope(payload.scope)
    try:
        checks = build_baseline_checks(
            db,
            household_id=user.household_id,
            scope=run_scope,
            period=payload.period,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Entidade não encontrada") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        run, _results = execute_integrity_run(
            db,
            household_id=user.household_id,
            scope=run_scope,
            trigger=IntegrityRunTrigger.MANUAL,
            checks=checks,
            created_by=user.id,
            period=payload.period,
            scope_entity_type=payload.entity_type,
            scope_entity_id=payload.entity_id,
        )
    except Exception as exc:
        db.commit()
        raise HTTPException(
            status_code=500,
            detail="A auditoria falhou sem alterar os dados financeiros",
        ) from exc

    audit(
        db,
        user,
        "integrity.run",
        "integrity_run",
        run.id,
        {"scope": run.scope, "period": run.period},
        after_state={"status": run.status, "summary": run.summary},
        reason="Execução manual solicitada por administrador",
        trace_id=run.trace_id,
        source="financial_integrity_engine",
    )
    db.commit()
    return serialize_run(run) or {}


@router.get("/integrity/runs/{run_id}")
def integrity_run_detail(
    run_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    run = db.scalar(
        select(IntegrityRun).where(
            IntegrityRun.id == run_id,
            IntegrityRun.household_id == user.household_id,
        )
    )
    if not run:
        raise HTTPException(status_code=404, detail="Execução de integridade não encontrada")
    finding_ids = tuple((run.summary or {}).get("finding_ids", ()))
    findings = (
        db.scalars(
            select(IntegrityFinding)
            .where(
                IntegrityFinding.household_id == user.household_id,
                IntegrityFinding.id.in_(finding_ids),
            )
            .order_by(_FINDING_SEVERITY_RANK.desc(), IntegrityFinding.created_at)
        ).all()
        if finding_ids
        else []
    )
    return {**(serialize_run(run) or {}), "findings": [serialize_finding(item) for item in findings]}


@router.get("/integrity/status")
def integrity_status(
    period: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return consolidated_integrity_status(
            db,
            household_id=user.household_id,
            period=period,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/integrity/findings")
def integrity_findings(
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    period: str | None = None,
    invariant_id: str | None = None,
    page: int = 1,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if page < 1 or limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="Paginação inválida")
    if status_filter and status_filter not in {
        "open",
        "acknowledged",
        "resolved",
        "ignored",
        "false_positive",
        # "active" = open OR acknowledged (`ACTIVE_FINDING_STATUSES`), not a
        # real `IntegrityFinding.status` value. Lets a caller ask for every
        # still-live finding in one request instead of one call per status --
        # the UI's global BLOCK/CRITICAL panel needs exactly this so a
        # material finding is never pushed out of view by the default
        # pagination cap regardless of which terminal statuses also exist
        # for the household. See the engineering review on PR 7 ("UI pode
        # esconder BLOCK/CRITICAL ativo pelo cap de 100").
        "active",
    }:
        raise HTTPException(status_code=422, detail="Status de finding inválido")
    if severity and severity not in {"info", "warning", "review", "critical", "block"}:
        raise HTTPException(status_code=422, detail="Severidade inválida")
    if period:
        try:
            datetime.strptime(period, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Mês deve usar o formato AAAA-MM") from exc

    filters = [IntegrityFinding.household_id == user.household_id]
    if status_filter == "active":
        filters.append(IntegrityFinding.status.in_(ACTIVE_FINDING_STATUSES))
    elif status_filter:
        filters.append(IntegrityFinding.status == status_filter)
    if severity:
        filters.append(IntegrityFinding.severity == severity)
    if period:
        filters.append(IntegrityFinding.period == period)
    if invariant_id:
        filters.append(IntegrityFinding.invariant_id == invariant_id.upper())

    total = db.scalar(select(func.count(IntegrityFinding.id)).where(*filters)) or 0
    rows = db.scalars(
        select(IntegrityFinding)
        .where(*filters)
        .order_by(IntegrityFinding.last_seen_at.desc(), IntegrityFinding.id)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    return {
        "items": [serialize_finding(item) for item in rows],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/integrity/findings/{finding_id}")
def integrity_finding_detail(
    finding_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    finding = db.scalar(
        select(IntegrityFinding).where(
            IntegrityFinding.id == finding_id,
            IntegrityFinding.household_id == user.household_id,
        )
    )
    if not finding:
        raise HTTPException(status_code=404, detail="Finding não encontrado")
    return serialize_finding(finding)


@router.post("/integrity/semantic-audit")
def integrity_semantic_audit(
    payload: SemanticAuditRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Consultative Codex Semantic Audit over the already-computed verdict.

    Authority boundary (see docs/ARCHITECTURE.md): `integrity_status` below
    is produced exactly the same way as `GET /integrity/status` and is never
    read from, or influenced by, the Codex response. `semantic_audit` is a
    strictly separate, structurally verdict-less block (see
    `app.services.codex_audit.AuditOutcome`) that can only explain,
    hypothesize about, or recommend review of that verdict -- it cannot
    resolve findings, correct data, or change `status`/`score`/`trusted_for_*`.
    An unavailable/timed-out/invalid Codex response degrades to
    `semantic_audit.available = False` and never to a false "pass".

    Role boundary (`docs/WORK_ORDER_ADMIN_READONLY_PROFILES.md`): never
    changing a financial fact does not make this route read-only -- it is a
    `POST` that runs an integrity audit and persists an `AuditEvent` of its
    own invocation, both of which the Work Order's acceptance criteria treat
    as operational state, not as a data read. `_require_admin` therefore
    runs first, exactly like every other mutating route.
    """

    _require_admin(user)
    try:
        integrity_status = consolidated_integrity_status(
            db,
            household_id=user.household_id,
            period=payload.period,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    finding_filters = [
        IntegrityFinding.household_id == user.household_id,
        IntegrityFinding.status.in_(("open", "acknowledged")),
    ]
    if payload.period:
        finding_filters.append(
            or_(IntegrityFinding.period == payload.period, IntegrityFinding.period.is_(None))
        )
    findings = db.scalars(
        select(IntegrityFinding)
        .where(*finding_filters)
        .order_by(_FINDING_SEVERITY_RANK.desc(), IntegrityFinding.last_seen_at.desc(), IntegrityFinding.id)
        .limit(30)
    ).all()

    outcome = run_semantic_audit(
        audit_type=payload.audit_type,
        integrity_status=integrity_status,
        findings=[serialize_finding(item) for item in findings],
        period=payload.period,
    )

    audit(
        db,
        user,
        "integrity.semantic_audit",
        "financial_profile",
        user.household_id,
        {
            "audit_type": payload.audit_type,
            "period": payload.period,
            "available": outcome.available,
            "reason": outcome.reason,
        },
        source="codex_semantic_audit",
    )
    db.commit()

    return {
        "audit_type": payload.audit_type,
        "period": payload.period,
        "integrity_status": integrity_status,
        "semantic_audit": outcome.to_dict(),
    }


def _finding_or_404(db: Session, user: User, finding_id: str) -> IntegrityFinding:
    finding = db.scalar(
        select(IntegrityFinding).where(
            IntegrityFinding.id == finding_id,
            IntegrityFinding.household_id == user.household_id,
        )
    )
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding não encontrado")
    return finding


def _finding_lifecycle_action(
    db: Session,
    user: User,
    finding_id: str,
    payload: FindingLifecycleRequest,
    *,
    action: str,
    apply,
) -> dict:
    _require_admin(user)
    finding = _finding_or_404(db, user, finding_id)
    before_state = {"status": finding.status}
    try:
        apply(finding)
    except FindingLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(
        db,
        user,
        f"integrity.finding.{action}",
        "integrity_finding",
        finding.id,
        before_state=before_state,
        after_state={"status": finding.status},
        reason=payload.reason,
        trace_id=finding.trace_id,
        source="financial_integrity_engine",
    )
    db.commit()
    db.refresh(finding)
    return serialize_finding(finding)


@router.post("/integrity/findings/{finding_id}/acknowledge")
def acknowledge_integrity_finding(
    finding_id: str,
    payload: FindingLifecycleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _finding_lifecycle_action(
        db,
        user,
        finding_id,
        payload,
        action="acknowledge",
        apply=lambda finding: acknowledge_finding(finding, user_id=user.id, reason=payload.reason),
    )


@router.post("/integrity/findings/{finding_id}/resolve")
def resolve_integrity_finding(
    finding_id: str,
    payload: FindingLifecycleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _finding_lifecycle_action(
        db,
        user,
        finding_id,
        payload,
        action="resolve",
        apply=lambda finding: resolve_finding(finding, user_id=user.id, reason=payload.reason),
    )


@router.post("/integrity/findings/{finding_id}/ignore")
def ignore_integrity_finding(
    finding_id: str,
    payload: FindingLifecycleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _finding_lifecycle_action(
        db,
        user,
        finding_id,
        payload,
        action="ignore",
        apply=lambda finding: ignore_finding(finding, user_id=user.id, reason=payload.reason),
    )


@router.post("/integrity/findings/{finding_id}/false-positive")
def mark_integrity_finding_false_positive(
    finding_id: str,
    payload: FindingLifecycleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _finding_lifecycle_action(
        db,
        user,
        finding_id,
        payload,
        action="false-positive",
        apply=lambda finding: mark_finding_false_positive(
            finding, user_id=user.id, reason=payload.reason
        ),
    )


_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _validate_period(period: str) -> None:
    # Stricter than `integrity_findings`' `datetime.strptime(period, "%Y-%m")`
    # (which accepts non-zero-padded months like "2026-8"): this value is fed
    # straight into `consolidated_integrity_status` -> `_period_bounds`,
    # which uses `date.fromisoformat` and would otherwise raise an unhandled
    # 500 instead of a clean 422.
    if not _PERIOD_PATTERN.match(period):
        raise HTTPException(status_code=422, detail="Mês deve usar o formato AAAA-MM") from None


@router.get("/monthly-closes/{period}")
def monthly_close_status(
    period: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _validate_period(period)
    close = get_monthly_close(db, household_id=user.household_id, period=period)
    return serialize_monthly_close(close, db=db, household_id=user.household_id, period=period)


@router.post("/monthly-closes/{period}/run")
def run_monthly_close(
    period: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Execute a fresh period-scoped integrity run and refresh the canonical snapshot.

    Deliberately does not decide `trusted` -- see `POST .../trust`. Unlike
    `POST /integrity/runs`, this endpoint rebuilds the period's canonical
    `FinancialSnapshot` *before* calling `execute_integrity_run` (the gate
    checks below need it), so a failed run cannot simply commit its own
    `failed` row the way `create_integrity_run` does -- that would also
    commit the already-flushed snapshot rebuild (and any predecessor
    supersession), despite the close having failed. On failure this instead
    rolls back the whole transaction and records the failure audit trail in
    isolation via `record_isolated_run_failure` -- see that function's
    docstring and the engineering review on PR 7. If anything after a
    *successful* run fails unexpectedly (e.g. the monthly close upsert), no
    explicit commit happens here, so the session close implicitly rolls back
    the whole request -- a close run is all-or-nothing either way.
    """

    _require_admin(user)
    _validate_period(period)
    # `assert_close_runnable` takes the household-wide revision barrier
    # (`lock_household_financial_revision`) as its very first action, before
    # reading `close.status` -- not the other way around. See its docstring
    # for the run→trust/trust→run interleaving that ordering closes (PR 7,
    # Round 11: a `trust`/`reopen` that used to be able to commit invisibly
    # between a stale status read here and a barrier acquired only
    # afterward). The same barrier stays held for the rest of this request:
    # on PostgreSQL it blocks a concurrent mutation from committing (and
    # thus from being partially reflected across `period_snapshot`/the
    # projection/consistency checks below), and blocks a concurrent
    # `trust`/`reopen` from even reading `close.status`, until this run's
    # transaction ends. The settled revision (captured further below, after
    # this run's own `IntegrityRun`/`IntegrityFinding` writes) is persisted
    # onto the close so `trust_monthly_close` can require its own freshly
    # locked revision to still match it -- see
    # `MonthlyFinancialClose.financial_revision`'s docstring and the
    # engineering review on PR 7, Round 8.
    try:
        assert_close_runnable(db, household_id=user.household_id, period=period)
    except MonthlyCloseStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    duplicate_checks = build_baseline_checks(
        db, household_id=user.household_id, scope=IntegrityRunScope.PERIOD, period=period
    )
    # A monthly close's `run` must be able to reach the deterministic
    # coverage `_trust_gate()` requires for `trusted_for_projection`
    # (INV-005, INV-006, INV-018, INV-022), not just INV-014. Reusing
    # `_build_projection_gate_checks` -- the exact same builder GET /forecast
    # uses -- proves the just-closed period's snapshot is a trustworthy seed
    # for the next month's projection, over one real month instead of the
    # full display horizon a forecast view needs. Without this, `run ->
    # trust` could never reach `trusted` through the real endpoint: see the
    # engineering review that blocked this PR on exactly that gap.
    profile = profile_for(db, user.household_id)
    period_start = date.fromisoformat(f"{period}-01")
    period_snapshot = build_snapshot(
        db, household_id=user.household_id, period=period, generated_by=user.id
    )
    close_entity_id = f"monthly-close:{user.household_id}:{period}"
    projection_checks, _rows, _validation = _build_projection_gate_checks(
        db,
        household_id=user.household_id,
        profile=profile,
        snapshot=period_snapshot,
        start_month=add_months(period_start, 1),
        end_month=add_months(period_start, 1),
        check_period=period,
        entity_type="monthly_close",
        entity_id=close_entity_id,
    )
    # `trusted_for_reports` needs its own required coverage (INV-019, INV-020;
    # INV-022 is already evaluated above and satisfies both gates at once --
    # see `_trust_gate`). Each invariant gets its own fact set (`"dashboard"`
    # for INV-019, `"report"` for INV-020) because `/dashboard` and
    # `/reports` do not publish the exact same field set -- see
    # `dashboard_and_report_consistency_facts`' docstring for why comparing
    # the snapshot to itself is the true state of the current architecture
    # rather than invented evidence.
    consistency_facts = dashboard_and_report_consistency_facts(period_snapshot, profile=profile)
    report_checks = (
        IntegrityCheck(
            "INV-019",
            InvariantContext(
                facts=consistency_facts["dashboard"],
                scope=InvariantScope.REPORT,
                entity_type="monthly_close",
                entity_id=close_entity_id,
                period=period,
            ),
        ),
        IntegrityCheck(
            "INV-020",
            InvariantContext(
                facts=consistency_facts["report"],
                scope=InvariantScope.REPORT,
                entity_type="monthly_close",
                entity_id=close_entity_id,
                period=period,
            ),
        ),
    )
    checks = duplicate_checks + projection_checks + report_checks
    try:
        run, _results = execute_integrity_run(
            db,
            household_id=user.household_id,
            scope=IntegrityRunScope.PERIOD,
            trigger=IntegrityRunTrigger.CLOSE,
            checks=checks,
            created_by=user.id,
            period=period,
        )
    except Exception as exc:
        # `period_snapshot` above was built (and may have superseded its
        # predecessor) *before* this try block, in the same uncommitted
        # transaction `execute_integrity_run` itself flushed a `failed` run
        # row into. Committing here (as before the engineering review caught
        # it) would persist that snapshot rebuild too, despite the close
        # having failed -- see the review comment ("Falha em
        # execute_integrity_run pode persistir snapshot pré-run"). Roll back
        # everything from this request first, then record the failure audit
        # trail in isolation, so a failed close never leaves any canonical
        # fact (snapshot, finding) behind.
        db.rollback()
        record_isolated_run_failure(
            db,
            household_id=user.household_id,
            scope=IntegrityRunScope.PERIOD,
            trigger=IntegrityRunTrigger.CLOSE,
            period=period,
            created_by=user.id,
            error=exc,
        )
        db.commit()
        raise HTTPException(
            status_code=500,
            detail="A execução do fechamento falhou sem alterar snapshot ou estado anterior",
        ) from exc

    snapshot = build_snapshot(
        db, household_id=user.household_id, period=period, generated_by=user.id, force=True
    )
    # The settled revision, still under the barrier locked above: it already
    # includes this run's own `IntegrityRun`/`IntegrityFinding` writes (both
    # are `FINANCIAL_REVISION_MODELS`), so a `trust` attempt right after this
    # commits sees the same value here and passes; any further financial
    # mutation before `trust` moves it and `trust_monthly_close` rejects the
    # stale run. See `MonthlyFinancialClose.financial_revision`'s docstring.
    run_financial_revision = current_household_financial_revision(db, household_id=user.household_id)
    close = upsert_monthly_close_after_run(
        db,
        household_id=user.household_id,
        period=period,
        snapshot_id=snapshot.id,
        integrity_run_id=run.id,
        financial_revision=run_financial_revision,
    )
    audit(
        db,
        user,
        "monthly_close.run",
        "monthly_financial_close",
        close.id,
        {"period": period},
        after_state={
            "status": close.status,
            "snapshot_id": close.snapshot_id,
            "integrity_run_id": close.integrity_run_id,
        },
        trace_id=run.trace_id,
        source="financial_integrity_engine",
    )
    db.commit()
    db.refresh(close)
    return serialize_monthly_close(close, db=db, household_id=user.household_id, period=period)


@router.post("/monthly-closes/{period}/trust")
def trust_monthly_close_endpoint(
    period: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    _validate_period(period)
    before = get_monthly_close(db, household_id=user.household_id, period=period)
    before_state = {"status": before.status} if before else {"status": "open"}
    try:
        close = trust_monthly_close(db, household_id=user.household_id, period=period, user_id=user.id)
    except MonthlyCloseGateError as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "reasons": list(exc.reasons)}
        ) from exc
    except MonthlyCloseStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit(
        db,
        user,
        "monthly_close.trust",
        "monthly_financial_close",
        close.id,
        before_state=before_state,
        after_state={
            "status": close.status,
            "closed_at": close.closed_at.isoformat() if close.closed_at else None,
        },
        source="financial_integrity_engine",
    )
    db.commit()
    db.refresh(close)
    return serialize_monthly_close(close, db=db, household_id=user.household_id, period=period)


@router.post("/monthly-closes/{period}/reopen")
def reopen_monthly_close_endpoint(
    period: str,
    payload: MonthlyCloseReopenRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    _validate_period(period)
    before = get_monthly_close(db, household_id=user.household_id, period=period)
    before_state = {"status": before.status} if before else {"status": "open"}
    try:
        close = reopen_monthly_close(
            db, household_id=user.household_id, period=period, user_id=user.id, reason=payload.reason
        )
    except MonthlyCloseStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit(
        db,
        user,
        "monthly_close.reopen",
        "monthly_financial_close",
        close.id,
        before_state=before_state,
        after_state={"status": close.status},
        reason=payload.reason,
        source="financial_integrity_engine",
    )
    db.commit()
    db.refresh(close)
    return serialize_monthly_close(close, db=db, household_id=user.household_id, period=period)


# ---------------------------------------------------------------------------
# P0 -- historical card-competence repair
# (docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md). Admin-only, household-
# scoped, preview/apply/rollback around `app.services.card_competence_repair`
# -- see that module's docstring for the full contract. No competence
# formula lives here; every number these three endpoints surface or persist
# came from that service, which itself only ever calls
# `app.services.card_competence.card_invoice_competence`.
# ---------------------------------------------------------------------------


@router.post("/maintenance/card-competence/preview")
def preview_card_competence_repair_endpoint(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    result = preview_card_competence_repair(db, household_id=user.household_id)
    return {
        "financial_revision": result["financial_revision"],
        "periods_affected": result["periods_affected"],
        "eligible_count": result["eligible_count"],
        "blocked_count": result["blocked_count"],
        "candidates": [serialize_candidate(candidate) for candidate in result["candidates"]],
    }


@router.post("/maintenance/card-competence/apply")
def apply_card_competence_repair_endpoint(
    payload: CardCompetenceRepairApplyRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        result = apply_card_competence_repair(
            db,
            household_id=user.household_id,
            transaction_ids=tuple(payload.transaction_ids),
            expected_financial_revision=payload.expected_financial_revision,
            reason=payload.reason,
            user_id=user.id,
        )
    except StaleRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoLongerCandidateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BlockedCandidateError as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "blocked": list(exc.blocked)}
        ) from exc
    except CardCompetenceRepairError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.commit()
    return {
        "batch_id": result.batch_id,
        "applied": list(result.applied),
        "periods_recomputed": list(result.periods_recomputed),
        "financial_revision": result.financial_revision,
    }


@router.post("/maintenance/card-competence/rollback")
def rollback_card_competence_repair_endpoint(
    payload: CardCompetenceRepairRollbackRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        result = rollback_card_competence_repair(
            db,
            household_id=user.household_id,
            transaction_ids=tuple(payload.transaction_ids),
            expected_financial_revision=payload.expected_financial_revision,
            reason=payload.reason,
            user_id=user.id,
        )
    except StaleRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoLongerCandidateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BlockedCandidateError as exc:
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "blocked": list(exc.blocked)}
        ) from exc
    except CardCompetenceRepairError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.commit()
    return {
        "batch_id": result.batch_id,
        "reverted": list(result.reverted),
        "periods_recomputed": list(result.periods_recomputed),
        "financial_revision": result.financial_revision,
    }


@router.get("/users")
def users(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    _require_admin(user)
    rows = db.scalars(select(User).where(User.household_id == user.household_id).order_by(User.name)).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "username": item.username,
            "is_admin": item.is_admin,
            "active": item.active,
            "is_current": item.id == user.id,
        }
        for item in rows
    ]


@router.post("/users", status_code=201)
def create_user(
    payload: UserCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = User(
        household_id=user.household_id,
        name=payload.name,
        username=payload.username.lower(),
        password_hash=password_hash,
        is_admin=payload.is_admin,
        active=True,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Este nome de usuário já está em uso") from exc
    audit(
        db,
        user,
        "user.create",
        "user",
        item.id,
        {"name": item.name, "username": item.username, "is_admin": item.is_admin},
    )
    db.commit()
    return {"id": item.id, "name": item.name, "username": item.username}


@router.delete("/users/{user_id}")
def deactivate_user(
    user_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    if user_id == user.id:
        raise HTTPException(status_code=409, detail="Você não pode desativar o próprio acesso")
    item = db.scalar(select(User).where(User.id == user_id, User.household_id == user.household_id))
    if not item:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    item.active = False
    audit(db, user, "user.deactivate", "user", item.id, {"username": item.username})
    db.commit()
    return {"ok": True}


@router.get("/categories")
def categories(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Category).where(Category.household_id == user.household_id).order_by(Category.name)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "color": item.color,
            "cash_cap": decimal_value(item.cash_cap) if item.cash_cap is not None else None,
            "essential": item.essential,
        }
        for item in rows
    ]


@router.get("/accounts")
def accounts(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Account)
        .where(Account.household_id == user.household_id, Account.active.is_(True))
        .order_by(Account.name)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "institution": item.institution,
            "account_type": item.account_type,
            "owner_label": item.owner_label,
            "last_four": item.last_four,
        }
        for item in rows
    ]


@router.post("/accounts", status_code=201)
def create_account(
    payload: AccountRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    account = Account(household_id=user.household_id, **payload.model_dump())
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Já existe uma conta com esse nome") from exc
    audit(db, user, "account.create", "account", account.id, payload.model_dump())
    db.commit()
    return {"id": account.id, "name": account.name}


@router.post("/account-balances", status_code=status.HTTP_201_CREATED)
def create_account_balance_observation(
    payload: AccountBalanceObservationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    account = db.scalar(
        select(Account).where(
            Account.id == payload.account_id,
            Account.household_id == user.household_id,
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    if payload.document_id:
        document = db.scalar(
            select(Document).where(
                Document.id == payload.document_id,
                Document.household_id == user.household_id,
                Document.account_id == account.id,
            )
        )
        if not document:
            raise HTTPException(status_code=404, detail="Documento da conta não encontrado")
    previous = None
    if payload.supersedes_id:
        previous = db.scalar(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.id == payload.supersedes_id,
                AccountBalanceObservation.household_id == user.household_id,
                AccountBalanceObservation.account_id == account.id,
            )
        )
        if not previous:
            raise HTTPException(status_code=404, detail="Observação anterior não encontrada")
        if previous.superseded_by_id:
            raise HTTPException(status_code=409, detail="A observação já foi substituída")

    trace_id = str(uuid.uuid4())
    observation = AccountBalanceObservation(
        household_id=user.household_id,
        account_id=account.id,
        amount=money(payload.amount),
        as_of_date=payload.as_of_date,
        observation_type=payload.observation_type,
        source="manual_confirmed",
        document_id=payload.document_id,
        confirmed_by=user.id,
        confidence=Decimal("1.0000"),
        supersedes_id=previous.id if previous else None,
        trace_id=trace_id,
    )
    db.add(observation)
    db.flush()
    if previous:
        previous.superseded_by_id = observation.id
        previous.invalidated_at = datetime.now(UTC)
        previous.invalidated_by = user.id
        previous.invalidation_reason = payload.reason
    audit(
        db,
        user,
        "account_balance.confirm",
        "account_balance_observation",
        observation.id,
        {"account_id": account.id, "as_of_date": payload.as_of_date.isoformat()},
        before_state=(
            {"observation_id": previous.id, "amount": str(previous.amount)}
            if previous
            else None
        ),
        after_state={
            "observation_id": observation.id,
            "amount": str(observation.amount),
            "as_of_date": observation.as_of_date.isoformat(),
        },
        reason=payload.reason or "Saldo confirmado manualmente pelo usuário",
        trace_id=trace_id,
        source="balance_observation",
    )
    db.commit()
    return _balance_observation_response(observation)


@router.get("/accounts/{account_id}/balances")
def account_balance_history(
    account_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    if not db.scalar(
        select(Account.id).where(
            Account.id == account_id,
            Account.household_id == user.household_id,
        )
    ):
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    rows = db.scalars(
        select(AccountBalanceObservation)
        .where(
            AccountBalanceObservation.household_id == user.household_id,
            AccountBalanceObservation.account_id == account_id,
        )
        .order_by(
            AccountBalanceObservation.as_of_date.desc(),
            AccountBalanceObservation.created_at.desc(),
        )
    ).all()
    return [_balance_observation_response(item) for item in rows]


def _balance_observation_response(item: AccountBalanceObservation) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "amount": str(item.amount),
        "as_of_date": item.as_of_date,
        "observation_type": item.observation_type,
        "source": item.source,
        "document_id": item.document_id,
        "confirmed_by": item.confirmed_by,
        "confidence": str(item.confidence),
        "supersedes_id": item.supersedes_id,
        "superseded_by_id": item.superseded_by_id,
        "invalidated_at": item.invalidated_at,
        "invalidated_by": item.invalidated_by,
        "invalidation_reason": item.invalidation_reason,
        "trace_id": item.trace_id,
        "created_at": item.created_at,
    }


class _ImportRejected(Exception):
    """A per-file precondition failure that must stop *this* file before any
    `Document` row is persisted for it -- missing account, oversized upload,
    or an exact-hash duplicate. Carries the same `(status_code, detail)` the
    single-file endpoint has always raised as an `HTTPException`, so:

    - the single-file route below converts it straight back into that same
      `HTTPException` (byte-identical response to before this Work Order);
    - the batch route catches it per file and records a truthful `rejected`
      result for that one file while every other file in the same request
      keeps its own, independent outcome -- one bad file never erases or
      rewrites a sibling file's already-persisted result (Work Order
      requirement).

    `detail` is always one of the fixed, generic Portuguese messages already
    used by the pre-existing single-file path (e.g. "Conta não encontrada"):
    never raw file content, never a value derived from the file's bytes.
    """

    def __init__(self, status_code: int, detail: str, error_category: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_category = error_category


def _resolve_import_account(
    db: Session, user: User, account_id: str | None, document_type: str
) -> Account | None:
    account = None
    if account_id:
        account = db.scalar(
            select(Account).where(Account.id == account_id, Account.household_id == user.household_id)
        )
    if document_type != "payroll" and not account:
        raise _ImportRejected(404, "Conta não encontrada", "account_not_found")
    return account


async def _read_upload_within_limit(file: UploadFile, max_mb: int) -> bytes:
    payload = await file.read(max_mb * 1024 * 1024 + 1)
    if len(payload) > max_mb * 1024 * 1024:
        raise _ImportRejected(413, f"Arquivo maior que {max_mb} MB", "file_too_large")
    return payload


def _reject_if_duplicate_file(db: Session, user: User, payload: bytes) -> str:
    """Exact-file-hash duplicate check, unchanged from the single-file path.

    Returns the SHA-256 digest so the caller does not hash the payload
    twice. Because each file in a batch is persisted and committed
    independently (see `_import_one_document`), calling this once per file
    *in order* also correctly rejects an exact duplicate that appears twice
    within the same batch request, not only against previously imported
    documents -- by the time file N is checked, file N-1's `Document` row is
    already committed and visible to this query.
    """

    digest = file_sha256(payload)
    if db.scalar(
        select(Document.id).where(Document.household_id == user.household_id, Document.sha256 == digest)
    ):
        raise _ImportRejected(409, "Este arquivo já foi importado", "duplicate_file")
    return digest


def _import_one_document(
    db: Session,
    user: User,
    *,
    account: Account | None,
    document_type: str,
    filename: str,
    payload: bytes,
    digest: str,
) -> dict:
    """Canonical per-file import pipeline: parse -> classify -> persist
    transactions -> duplicate detection -> reconciliation -> audit, exactly
    as the single-file `POST /imports` endpoint has always done it. Both the
    single-file route and the batch route call this same function once per
    file -- there is no second/parallel import policy. The caller is
    responsible for the file-level preconditions above
    (`_resolve_import_account`, `_read_upload_within_limit`,
    `_reject_if_duplicate_file`) and for issuing `db.rollback()` if this
    function raises before reaching one of its own `db.commit()` calls
    below, so that a failure on one file cannot leave a half-written
    transaction that could affect the next file processed on the same
    `Session` (relevant for the batch route only; the single-file route's
    `Session` is discarded after the request either way).

    The encrypted artifact saved below is only truly owned by this
    `Document` once that row commits. If `_persist_parsed_document` raises
    anything unexpected (not one of its own `ValueError` parse-failure
    paths, which already commit a truthful `review_required` `Document`),
    the never-committed artifact this call just wrote would otherwise be
    left on disk/object storage with no owning DB row -- an orphaned
    encrypted financial document with no provenance and no supported
    lifecycle. That is caught and cleaned up here, narrowly scoped to the
    exact `document.encrypted_path` this call just produced (a fresh,
    per-attempt path keyed by a freshly generated `document.id`), so this
    can never remove a pre-existing or already-committed sibling document's
    artifact. The caller's own `db.rollback()` still applies to the `Document`
    row itself; this only keeps the filesystem consistent with it.

    The failure is logged with `logger.error` (never `logger.exception`/
    `exc_info=True`) and only fixed identifiers -- `document.id`,
    `document_type`, a constant `unexpected_error` category. An unexpected
    exception raised from parsing/classification/storage code can
    legitimately carry request-derived text (a merchant description, a
    filename fragment, parser detail) in `str(exc)` or its traceback, and
    the Work Order forbids raw financial content/PII in logs; this never
    serializes the exception object, its message or its traceback, so
    there is nothing here for that content to leak through.
    """

    document = Document(
        household_id=user.household_id,
        account_id=account.id if account else None,
        original_name=filename,
        document_type=document_type,
        sha256=digest,
        encrypted_path="pending",
    )
    db.add(document)
    db.flush()
    document.encrypted_path = EncryptedDocumentStore().save(document.id, payload)
    try:
        return _persist_parsed_document(
            db, user, document=document, account=account, document_type=document_type, payload=payload, digest=digest
        )
    except Exception:
        EncryptedDocumentStore().delete(document.encrypted_path)
        logger.error(
            "import.unexpected_failure document_id=%s document_type=%s error_category=unexpected_error",
            document.id,
            document_type,
        )
        raise


def _persist_parsed_document(
    db: Session,
    user: User,
    *,
    document: Document,
    account: Account | None,
    document_type: str,
    payload: bytes,
    digest: str,
) -> dict:
    """Parse, classify, persist and reconcile an already-created `Document`.

    Extracted verbatim from `_import_one_document` so the caller above can
    wrap it in a single `try/except` that cleans up the encrypted artifact
    on any unexpected failure (see that function's docstring). No parsing,
    classification, reconciliation or duplicate behavior changed by this
    split.
    """

    if document_type == "payroll":
        try:
            parsed_document = parse_payroll_document(payload)
            payroll = parsed_document.payroll
            if payroll is None:
                raise ValueError("Holerite sem os campos obrigatórios; encaminhado para revisão")
        except ValueError as exc:
            document.status = "review_required"
            document.notes = str(exc)
            parsed_document, reconciliation = unknown_reconciliation(
                document_type=document_type,
                parser_name="payroll_pdf",
                reason="payroll_parse_failed",
            )
            reconciliation_row = persist_reconciliation(
                db,
                household_id=user.household_id,
                document_id=document.id,
                parsed=parsed_document,
                result=reconciliation,
                confirmed_by=user.id,
            )
            db.add(
                ReviewItem(
                    household_id=user.household_id,
                    document_id=document.id,
                    reason="document_not_parsed",
                    details=str(exc),
                )
            )
            audit(db, user, "document.review_required", "document", document.id, {"reason": str(exc)})
            db.commit()
            return {
                "document_id": document.id,
                "status": document.status,
                "records": 0,
                "message": str(exc),
                "reconciliation": serialize_reconciliation(reconciliation_row),
            }
        record = PayrollRecord(
            household_id=user.household_id,
            document_id=document.id,
            person_name=payroll.person_name,
            competence=payroll.competence,
            payment_date=payroll.payment_date,
            payroll_kind="regular",
            gross_amount=payroll.gross_amount,
            deductions=payroll.deductions,
            net_amount=payroll.net_amount,
            payroll_loan=payroll.payroll_loan,
            notes="Importado automaticamente do holerite",
        )
        db.add(record)
        reconciliation = reconcile_parsed_document(parsed_document)
        reconciliation_row = persist_reconciliation(
            db,
            household_id=user.household_id,
            document_id=document.id,
            parsed=parsed_document,
            result=reconciliation,
            confirmed_by=user.id,
        )
        document.status = "imported"
        document.record_count = 1
        audit(db, user, "document.import", "document", document.id, {"records": 1})
        db.commit()
        return {
            "document_id": document.id,
            "status": document.status,
            "records": 1,
            "review_items": 0,
            "reconciliation": serialize_reconciliation(reconciliation_row),
        }

    try:
        parsed_document = parse_document_contract(
            document.original_name, payload, document_type
        )
        parsed = parsed_document.transactions
    except ValueError as exc:
        document.status = "review_required"
        document.notes = str(exc)
        parsed_document, reconciliation = unknown_reconciliation(
            document_type=document_type,
            parser_name="document_parser",
            reason="document_parse_failed",
        )
        reconciliation_row = persist_reconciliation(
            db,
            household_id=user.household_id,
            document_id=document.id,
            parsed=parsed_document,
            result=reconciliation,
            account_id=account.id,
            confirmed_by=user.id,
        )
        db.add(
            ReviewItem(
                household_id=user.household_id,
                document_id=document.id,
                reason="document_not_parsed",
                details=str(exc),
            )
        )
        audit(db, user, "document.review_required", "document", document.id, {"reason": str(exc)})
        db.commit()
        return {
            "document_id": document.id,
            "status": document.status,
            "records": 0,
            "message": str(exc),
            "reconciliation": serialize_reconciliation(reconciliation_row),
        }

    imported = 0
    review_count = 0
    internal_aliases = tuple(
        {
            *db.scalars(select(User.name).where(User.household_id == user.household_id)).all(),
            *db.scalars(select(Account.owner_label).where(Account.household_id == user.household_id)).all(),
        }
    )
    for item in parsed:
        fingerprint = transaction_fingerprint(account.id, item, account.owner_label)
        classification = classify_with_local_rules(
            db,
            household_id=user.household_id,
            description=item.description,
            amount=float(item.amount),
            internal_aliases=internal_aliases,
        )
        category = category_for(db, user.household_id, classification.category)
        transaction_trace_id = str(uuid.uuid4())
        transaction = Transaction(
            household_id=user.household_id,
            account_id=account.id,
            document_id=document.id,
            category_id=category.id,
            booked_at=item.booked_at,
            description=item.description,
            normalized_description=normalize_description(item.description),
            amount=item.amount,
            transaction_type=classification.transaction_type,
            owner_label=account.owner_label,
            card_last_four=item.card_last_four or account.last_four,
            installment_current=item.installment_current,
            installment_total=item.installment_total,
            fingerprint=fingerprint,
            source_line=item.source_line,
            occurred_at=item.occurred_at or item.booked_at,
            competence=item.booked_at.strftime("%Y-%m"),
            classification_source=classification.source,
            classification_version=classification.version,
            canonical_status="unassigned",
            trace_id=transaction_trace_id,
            source_priority=source_priority(document_type),
            confidence=Decimal(str(classification.confidence)),
            excluded=classification.excluded,
            possible_duplicate=False,
        )
        db.add(transaction)
        db.flush()
        duplicate_group, duplicate_assessment = register_transaction_duplicates(
            db,
            transaction=transaction,
            household_id=user.household_id,
        )
        duplicate = duplicate_group is not None
        if duplicate or classification.review_reason:
            reason = "possible_duplicate" if duplicate else "classification_low_confidence"
            details = (
                (
                    "Possível repetição persistida em grupo "
                    f"({duplicate_assessment.band}, confiança {duplicate_assessment.confidence})"
                )
                if duplicate and duplicate_assessment
                else classification.review_reason
            )
            db.add(
                ReviewItem(
                    household_id=user.household_id,
                    transaction_id=transaction.id,
                    document_id=document.id,
                    reason=reason,
                    details=details,
                )
            )
            review_count += 1
        imported += 1
    reconciliation = reconcile_parsed_document(parsed_document)
    reconciliation_row = persist_reconciliation(
        db,
        household_id=user.household_id,
        document_id=document.id,
        parsed=parsed_document,
        result=reconciliation,
        account_id=account.id,
        confirmed_by=user.id,
    )
    if reconciliation.status == "not_reconciled":
        db.add(
            ReviewItem(
                household_id=user.household_id,
                document_id=document.id,
                reason="document_not_reconciled",
                details="Os componentes declarados não fecham dentro da tolerância de R$ 0,01",
            )
        )
        review_count += 1
    document.status = "imported_with_review" if review_count else "imported"
    document.record_count = imported
    audit(
        db,
        user,
        "document.import",
        "document",
        document.id,
        {
            "records": imported,
            "review_items": review_count,
            "sha256": digest,
            "reconciliation_status": reconciliation.status,
            "reconciliation_id": reconciliation_row.id,
        },
        after_state={
            "status": document.status,
            "record_count": imported,
            "reconciliation_status": reconciliation.status,
        },
        trace_id=reconciliation_row.trace_id,
        source="document_reconciliation",
    )
    db.commit()
    return {
        "document_id": document.id,
        "status": document.status,
        "records": imported,
        "review_items": review_count,
        "reconciliation": serialize_reconciliation(reconciliation_row),
    }


@router.post("/imports", status_code=201)
async def import_document(
    account_id: str | None = Form(default=None),
    document_type: str = Form(..., pattern="^(bank_statement|credit_card|payroll)$"),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        account = _resolve_import_account(db, user, account_id, document_type)
        payload = await _read_upload_within_limit(file, settings.max_upload_mb)
        digest = _reject_if_duplicate_file(db, user, payload)
    except _ImportRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _import_one_document(
        db,
        user,
        account=account,
        document_type=document_type,
        filename=file.filename or "documento",
        payload=payload,
        digest=digest,
    )


def _batch_item_result(index: int, filename: str, status_code: int, error_category: str, message: str) -> dict:
    """Shape a rejected/failed batch entry with the same field set a
    successful entry gets below, so the frontend never has to special-case
    which keys exist -- it only ever displays fields the backend already
    computed (Work Order requirement: no client-side derivation).
    """

    return {
        "index": index,
        "filename": filename,
        "status": "rejected",
        "http_status": status_code,
        "error_category": error_category,
        "message": message,
        "document_id": None,
        "records": None,
        "review_items": None,
        "reconciliation": None,
    }


def _batch_size_cap_bytes() -> int:
    """Total combined upload size allowed for one batch request, derived
    from `settings.max_batch_total_mb`. Kept as its own function (rather
    than inlined at the call site) so tests can pin an exact byte boundary
    without depending on whole-megabyte granularity.
    """

    return settings.max_batch_total_mb * 1024 * 1024


def _summarize_batch(results: list[dict]) -> dict:
    """A count of backend-computed per-file `status` values -- never a
    financial total. `imported`/`imported_with_review`/`review_required` are
    the exact `Document.status` values the canonical pipeline already
    produces; `rejected` is this route's own precondition-failure status.
    """

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"total": len(results), "by_status": counts}


@router.post("/imports/batch", status_code=201)
async def import_documents_batch(
    account_id: str | None = Form(default=None),
    document_type: str = Form(..., pattern="^(bank_statement|credit_card|payroll)$"),
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Batch import: orchestration over the same canonical per-file pipeline
    `import_document` above uses (`_import_one_document`), called once per
    file -- there is no second/parallel parsing, classification,
    reconciliation or duplicate policy here.

    Transaction boundary: each file is parsed, persisted and committed
    independently (`_import_one_document` calls `db.commit()` itself, once
    per file, exactly as the single-file endpoint already does for its one
    file). A file that fails after partial writes only rolls back its own
    uncommitted work (`db.rollback()` below); every previously processed
    file in this same batch request keeps its own already-committed,
    independent outcome. This mirrors the single-file endpoint's existing
    architecture (each `POST /imports` call already is its own transaction)
    rather than introducing a new, batch-only policy -- a single shared
    transaction for the whole batch was rejected because it would let one
    bad file roll back sibling files that had already produced a truthful,
    independently valid result, which the Work Order explicitly forbids.

    `account_id`/`document_type` apply to every file in the batch, mirroring
    how a household actually uses this today (several statements/invoices
    for the same account and document type in one sitting, e.g. six months
    of the same card's PDF invoices, or a CSV and an OFX export of the same
    checking account for the same period). Reusing the exact same
    per-request account/type contract as the single-file endpoint -- instead
    of inventing a second, batch-only parameter shape -- keeps the
    documented single-file semantics (account resolution, payroll's account
    exemption, the `document_type` pattern) identical for every file with no
    new branch to keep in sync.
    """

    _require_admin(user)
    if not files:
        raise HTTPException(status_code=400, detail="Envie ao menos um arquivo")
    if len(files) > settings.max_batch_files:
        raise HTTPException(
            status_code=413,
            detail=f"Lote maior que {settings.max_batch_files} arquivos por envio",
        )
    try:
        account = _resolve_import_account(db, user, account_id, document_type)
    except _ImportRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    batch_id = str(uuid.uuid4())
    batch_cap_bytes = _batch_size_cap_bytes()
    total_bytes = 0
    batch_cap_exceeded = False
    results: list[dict] = []
    for index, file in enumerate(files):
        # A safe, non-PII identifier for a file the caller left unnamed --
        # never the raw filename guessed or fabricated, never file content.
        filename = file.filename or f"documento-{index + 1}"
        if batch_cap_exceeded:
            results.append(
                _batch_item_result(
                    index,
                    filename,
                    413,
                    "batch_too_large",
                    "Lote excede o limite total combinado; arquivo não processado",
                )
            )
            continue
        try:
            payload = await _read_upload_within_limit(file, settings.max_upload_mb)
            total_bytes += len(payload)
            if total_bytes > batch_cap_bytes:
                raise _ImportRejected(
                    413,
                    f"Lote maior que {settings.max_batch_total_mb} MB combinados",
                    "batch_too_large",
                )
            digest = _reject_if_duplicate_file(db, user, payload)
        except _ImportRejected as exc:
            db.rollback()
            results.append(_batch_item_result(index, filename, exc.status_code, exc.error_category, exc.detail))
            if exc.error_category == "batch_too_large":
                batch_cap_exceeded = True
            continue
        try:
            result = _import_one_document(
                db,
                user,
                account=account,
                document_type=document_type,
                filename=filename,
                payload=payload,
                digest=digest,
            )
        except Exception:
            # An unexpected failure processing this one file must not corrupt
            # the shared `Session` for the files still to come, and must not
            # leak file content/PII -- only a safe, generic category and this
            # file's index are recorded, exactly the Work Order's error-safety
            # requirement. `_import_one_document` already logs this failure
            # server-side for operators, deliberately without the exception's
            # own message or traceback (see its docstring) -- neither this
            # response nor that log line ever carries request-derived text.
            db.rollback()
            results.append(
                _batch_item_result(
                    index,
                    filename,
                    500,
                    "unexpected_error",
                    "Falha inesperada ao processar este arquivo; os demais arquivos do lote não foram afetados",
                )
            )
            continue
        result.setdefault("review_items", None)
        result.setdefault("message", None)
        result["index"] = index
        result["filename"] = filename
        result["http_status"] = 201
        result["error_category"] = None
        results.append(result)

    audit(
        db,
        user,
        "document.import_batch",
        None,
        None,
        {
            "batch_id": batch_id,
            "file_count": len(files),
            # `document_id`/`error_category` make the persisted audit trail
            # itself the deterministic batch -> Document lineage: a rejected
            # or precondition-failed file never created a Document, so it is
            # truthfully `document_id: None` here (matching the response's
            # own contract), never fabricated or reconstructed later from
            # timestamps.
            "outcomes": [
                {
                    "index": item["index"],
                    "status": item["status"],
                    "document_id": item["document_id"],
                    "error_category": item["error_category"],
                }
                for item in results
            ],
        },
    )
    db.commit()
    return {
        "batch_id": batch_id,
        "file_count": len(files),
        "results": results,
        "summary": _summarize_batch(results),
    }


@router.get("/imports")
def list_imports(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Document)
        .where(Document.household_id == user.household_id)
        .order_by(Document.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.original_name,
            "type": item.document_type,
            "status": item.status,
            "records": item.record_count,
            "notes": item.notes,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.get("/imports/{document_id}/reconciliation")
def document_reconciliation(
    document_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    document = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.household_id == user.household_id,
        )
    )
    if not document:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    reconciliation = db.scalar(
        select(DocumentReconciliation)
        .where(
            DocumentReconciliation.document_id == document.id,
            DocumentReconciliation.household_id == user.household_id,
        )
        .order_by(
            DocumentReconciliation.reconciled_at.desc(),
            DocumentReconciliation.id.desc(),
        )
        .limit(1)
    )
    if not reconciliation:
        raise HTTPException(status_code=404, detail="Reconciliação ainda não executada")
    return serialize_reconciliation(reconciliation)


@router.post("/imports/{document_id}/reconcile", status_code=status.HTTP_201_CREATED)
def rerun_document_reconciliation(
    document_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    document = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.household_id == user.household_id,
        )
    )
    if not document:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    try:
        payload = EncryptedDocumentStore().read(document.encrypted_path)
        if document.document_type == "payroll":
            parsed_document = parse_payroll_document(payload)
        else:
            parsed_document = parse_document_contract(
                document.original_name,
                payload,
                document.document_type,
            )
        result = reconcile_parsed_document(parsed_document)
    except ValueError as exc:
        parsed_document, result = unknown_reconciliation(
            document_type=document.document_type,
            parser_name="document_parser",
            reason="reconciliation_parse_failed",
        )
        document.notes = str(exc)
    reconciliation = persist_reconciliation(
        db,
        household_id=user.household_id,
        document_id=document.id,
        parsed=parsed_document,
        result=result,
        account_id=document.account_id,
        confirmed_by=user.id,
    )
    audit(
        db,
        user,
        "document.reconcile",
        "document",
        document.id,
        {"reconciliation_id": reconciliation.id, "status": reconciliation.status},
        after_state={"reconciliation_status": reconciliation.status},
        trace_id=reconciliation.trace_id,
        source="document_reconciliation",
    )
    db.commit()
    return serialize_reconciliation(reconciliation)


async def _analyze_and_persist_capture(
    db: Session,
    user: User,
    document: Document | None,
    *,
    text: str | None,
    filename: str | None,
    content_type: str | None,
    payload: bytes | None,
    document_type: str,
    account: Account | None,
    capture: CaptureDraft | None = None,
) -> CaptureDraft:
    """Run `preview_capture` (+ conditional Codex low-confidence enrichment)
    and persist the resulting proposal onto a `CaptureDraft`.

    This is the single place -- shared by the synchronous fast path
    (plain text / CSV / OFX, below) and the asynchronous OCR/audio worker
    (`_process_claimed_capture_job`) -- that turns processed text into a
    capture proposal; see `docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md` item
    2 ("reutilizar os mesmos parsers/processadores/serviços canônicos
    existentes; não criar segunda implementação").

    When `capture` is `None`, a new draft is created (the synchronous path,
    and the `CaptureParseError` "needs_input" branch it can also take).
    When `capture` is given, that existing row is updated in place instead
    -- what the worker and manual retry use, so reprocessing a draft can
    never create a second `CaptureDraft`/`Document` (item 4: retry must not
    duplicate a draft or document).

    Caller is responsible for `db.commit()`.
    """

    try:
        analysis = await run_in_threadpool(
            preview_capture,
            text=text,
            filename=filename,
            content_type=content_type,
            payload=payload,
            requested_type=document_type,
            account_type=account.account_type if account else None,
            db=db,
            household_id=user.household_id,
        )
    except CaptureParseError as exc:
        source_type = "text"
        if payload:
            if (content_type or "").startswith("audio/"):
                source_type = "audio"
            elif (content_type or "").startswith("image/"):
                source_type = "image"
            else:
                source_type = "document"
        if capture is None:
            capture = CaptureDraft(
                household_id=user.household_id,
                user_id=user.id,
                document_id=document.id if document else None,
                proposal_json="[]",
                confidence=Decimal("0"),
            )
            db.add(capture)
        capture.source_type = source_type
        capture.detected_type = document_type
        capture.status = "needs_input"
        capture.processor = "local_rules"
        capture.original_text = (text or "")[:10000] or None
        capture.notes = str(exc)
        db.flush()
        if document:
            document.status = "capture_needs_input"
            document.notes = str(exc)
        audit(
            db,
            user,
            "capture.needs_input",
            "capture_draft",
            capture.id,
            {"source_type": source_type, "reason": str(exc)},
        )
        return capture

    category_rows = db.scalars(select(Category).where(Category.household_id == user.household_id)).all()
    categories_by_name = {item.name.casefold(): item.name for item in category_rows}
    if (
        len(analysis["items"]) == 1
        and analysis["items"][0].get("kind") == "transaction"
        and float(analysis["items"][0].get("confidence", 0)) < 0.70
    ):
        codex = CodexAdvisorClient()
        classification = await run_in_threadpool(
            codex.classify,
            {
                "message": (analysis.get("extracted_text") or text or "")[:2000],
                "local_proposal": analysis["items"][0],
                "allowed_categories": [item.name for item in category_rows],
            },
        )
        if classification.payload:
            suggestion = classification.payload
            allowed_category = categories_by_name.get(str(suggestion.get("category_name") or "").casefold())
            if allowed_category:
                proposal = analysis["items"][0]
                proposal["category_name"] = allowed_category
                proposal["movement_type"] = suggestion.get("movement_type", proposal.get("movement_type"))
                proposed_description = " ".join(
                    str(suggestion.get("description") or proposal.get("description") or "").split()
                )
                if 2 <= len(proposed_description) <= 500:
                    proposal["description"] = proposed_description
                try:
                    suggested_confidence = float(suggestion.get("confidence", 0))
                except (TypeError, ValueError):
                    suggested_confidence = 0
                proposal["confidence"] = round(max(0, min(1, suggested_confidence)), 4)
                if suggestion.get("reason"):
                    proposal.setdefault("warnings", []).append(str(suggestion["reason"]))
                analysis["processor"] += "+codex"
                analysis["confidence"] = proposal["confidence"]

    proposals = _enrich_capture_items(
        db,
        user.household_id,
        analysis["items"],
        account.id if account else None,
    )
    warnings = list(analysis.get("warnings") or [])
    if any(item.get("requires_account") for item in proposals):
        warnings.append("Escolha a conta ou o cartão antes de confirmar")
    if capture is None:
        capture = CaptureDraft(household_id=user.household_id, user_id=user.id, document_id=document.id if document else None)
        db.add(capture)
    capture.source_type = analysis["source_type"]
    capture.detected_type = analysis["detected_type"]
    capture.status = "preview"
    capture.processor = analysis["processor"]
    capture.original_text = (text or "")[:10000] or None
    capture.extracted_text = analysis.get("extracted_text") or None
    capture.proposal_json = json.dumps(proposals, ensure_ascii=False)
    capture.confidence = Decimal(str(analysis["confidence"]))
    capture.notes = "; ".join(dict.fromkeys(warnings))[:4000] or None
    db.flush()
    if document:
        document.document_type = (
            analysis["detected_type"]
            if analysis["detected_type"] in {"bank_statement", "credit_card", "payroll"}
            else "smart_capture"
        )
        document.status = "capture_preview"
    audit(
        db,
        user,
        "capture.preview",
        "capture_draft",
        capture.id,
        {
            "source_type": capture.source_type,
            "detected_type": capture.detected_type,
            "processor": capture.processor,
            "items": len(proposals),
        },
    )
    return capture


async def _process_claimed_capture_job(
    db: Session, job: CaptureProcessingJob, *, payload: bytes | None
) -> None:
    """Process a job this call has already claimed (see
    `capture_worker.claim_capture_job`). Shared by the background task that
    runs right after `POST /captures/preview`/`/retry` responds and by the
    crash-recovery reconciler (`app/cli/capture_worker.py`) -- neither path
    duplicates the OCR/Whisper/classification work itself, only how a job
    gets claimed.

    `payload` is the plaintext bytes already read for the request that
    enqueued the job, when available (avoids a redundant decrypt); the
    reconciler has no such in-memory payload and this function reads the
    encrypted original from disk instead. Either way the original file
    itself is never rewritten, never leaves this process, and is never
    logged.

    Every row this function touches -- the draft, its document, the
    account, the confirming user -- is looked up scoped to `job.household_id`,
    never by bare id (`db.get`). The FKs on `capture_processing_jobs` do not
    by themselves guarantee those rows all belong to the same household, so
    an unscoped lookup could read/decrypt/mutate another household's data
    if the persisted state were ever inconsistent; scoping every lookup and
    fail-closing (finalizing the job as `failed` without processing
    anything) the moment one doesn't correlate is what the Work Order's
    "não permitir processamento cross-household" invariant actually
    requires.
    """

    # Pinned at the exact attempt this call claimed (the value
    # `claim_capture_job` returned right after its CAS `UPDATE` committed).
    # Every `finalize_capture_job` call below must compare against this same
    # frozen value, never a re-read of `job.attempts`, so that an old lease
    # whose job was reclaimed out from under it (see
    # `capture_worker.finalize_capture_job`'s docstring) cannot finalize a
    # newer attempt it no longer owns.
    claimed_attempt = job.attempts

    def _finalize_or_discard(
        *, status: str, error_code: str | None = None, duration_ms: int | None = None
    ) -> bool:
        """Atomically resolve `job` as terminal, or -- if it was already
        resolved by someone else (a concurrent cancellation, or a newer
        lease that reclaimed this job out from under this attempt) -- roll
        back anything staged in this transaction and report that we lost."""

        won = capture_worker.finalize_capture_job(
            db,
            job.id,
            status=status,
            claimed_attempt=claimed_attempt,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        if not won:
            db.rollback()
            logger.info(
                "capture_processing_job_finalize_conflict",
                extra={"job_id": job.id, "job_type": job.job_type, "household_id": job.household_id},
            )
        return won

    # Scoped correlation check before ever loading the draft's row: reading
    # only the `household_id` column (never the full `CaptureDraft`, which
    # carries captured content -- `original_text`, `proposal_json`, etc.)
    # is enough to decide, without materializing a foreign household's data
    # in memory, whether this job's own `household_id` still agrees with
    # the draft it is correlated to.
    draft_household_id = db.scalar(
        select(CaptureDraft.household_id).where(CaptureDraft.id == job.capture_draft_id)
    )
    if draft_household_id is None:
        # Draft no longer exists (out-of-band cleanup) -- benign; nothing
        # left to do, finish the job honestly.
        if _finalize_or_discard(status="completed"):
            db.commit()
        return
    if draft_household_id != job.household_id:
        # Fail closed: this job's own household_id disagrees with the
        # draft it is correlated to (corrupted/inconsistent persisted
        # state). Never load the foreign draft's row here -- writing to it,
        # or even reading its captured content, would itself be the
        # cross-household processing item 6 of the Work Order prohibits.
        if _finalize_or_discard(status="failed", error_code="cross_household_state"):
            db.commit()
        return
    capture = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == job.capture_draft_id, CaptureDraft.household_id == job.household_id
        )
    )
    if capture is None:
        # TOCTOU: the draft was removed between the household check above
        # and this scoped fetch -- benign, same as "no longer exists".
        if _finalize_or_discard(status="completed"):
            db.commit()
        return
    if capture.status == "cancelled":
        # The household cancelled this draft while the job was in flight;
        # never resurrect a cancelled draft by writing a proposal onto it.
        if _finalize_or_discard(status="completed"):
            db.commit()
        return

    document = None
    if job.document_id:
        document = db.scalar(
            select(Document).where(Document.id == job.document_id, Document.household_id == job.household_id)
        )
        if document is None:
            if _finalize_or_discard(status="failed", error_code="cross_household_state"):
                if capture.status not in {"confirmed", "cancelled"}:
                    capture.status = "failed"
                db.commit()
            return

    account = None
    if document is not None and document.account_id:
        account = db.scalar(
            select(Account).where(Account.id == document.account_id, Account.household_id == job.household_id)
        )
        if account is None:
            if _finalize_or_discard(status="failed", error_code="cross_household_state"):
                if capture.status not in {"confirmed", "cancelled"}:
                    capture.status = "failed"
                document.status = "capture_failed"
                db.commit()
            return

    actor_user = None
    if capture.user_id:
        actor_user = db.scalar(
            select(User).where(User.id == capture.user_id, User.household_id == job.household_id)
        )
        if actor_user is None:
            if _finalize_or_discard(status="failed", error_code="cross_household_state"):
                if capture.status not in {"confirmed", "cancelled"}:
                    capture.status = "failed"
                if document is not None:
                    document.status = "capture_failed"
                db.commit()
            return
    actor = actor_user or SimpleNamespace(household_id=job.household_id, id=capture.user_id)

    capture.status = "processing"
    db.commit()

    timer_started = time.perf_counter()
    try:
        if payload is None and document:
            payload = EncryptedDocumentStore().read(document.encrypted_path)
        await _analyze_and_persist_capture(
            db,
            actor,
            document,
            text=capture.original_text,
            filename=document.original_name if document else None,
            content_type=job.content_type,
            payload=payload,
            document_type=capture.detected_type,
            account=account,
            capture=capture,
        )
        duration_ms = round((time.perf_counter() - timer_started) * 1000)
        if _finalize_or_discard(status="completed", duration_ms=duration_ms):
            db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "capture_processing_job_failed",
            extra={"job_id": job.id, "job_type": job.job_type, "household_id": job.household_id},
        )
        duration_ms = round((time.perf_counter() - timer_started) * 1000)
        if _finalize_or_discard(status="failed", error_code="processing_error", duration_ms=duration_ms):
            # `db.rollback()` above expired every object in this session,
            # including `capture` -- re-fetch it scoped by `job.household_id`
            # (never a bare `db.get` by id) rather than trust the expired
            # reference, exactly like the initial lookup above.
            capture = db.scalar(
                select(CaptureDraft).where(
                    CaptureDraft.id == job.capture_draft_id, CaptureDraft.household_id == job.household_id
                )
            )
            if capture is not None and capture.status not in {"confirmed", "cancelled"}:
                capture.status = "failed"
            if document is not None:
                document.status = "capture_failed"
            db.commit()


def get_capture_job_session_factory() -> Callable[[], Session]:
    """FastAPI dependency wrapping `app.db.SessionLocal` for the async
    capture worker's background task.

    A `BackgroundTasks` target runs after the request's own `Depends(get_db)`
    session has already closed, so it must always open a session of its
    own -- but resolving *which* session factory to use through a
    dependency (rather than importing `SessionLocal` directly at the call
    site) lets tests override this exactly like they override `get_db`
    (`app.dependency_overrides[get_capture_job_session_factory] = ...`)
    instead of depending on which test module happens to import `app.db`
    first and fix its module-level engine for the rest of the test run.
    """

    return SessionLocal


async def _run_capture_job_background(
    job_id: str,
    payload: bytes | None,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """`BackgroundTasks` target for a freshly (re)queued OCR/audio job: runs
    after the HTTP response has already been sent, on its own DB session
    (`app.db.SessionLocal` by default) since the request's own session is
    closed by the time background tasks execute.

    `session_factory` defaults to the real `app.db.SessionLocal` -- tests
    inject their own isolated session factory instead of a global
    `get_db` dependency override, since this always opens a session itself
    rather than receiving one through FastAPI's dependency injection (see
    `app.cli.capture_worker.reconcile_once`, the crash-recovery reconciler,
    for the same pattern).
    """

    db = session_factory()
    try:
        job = capture_worker.claim_capture_job(
            db,
            job_id,
            worker_id=capture_worker.worker_identity(),
            stale_after_seconds=settings.capture_job_stale_after_seconds,
        )
        if job is None:
            return
        await _process_claimed_capture_job(db, job, payload=payload)
    finally:
        db.close()


@router.post("/captures/preview", status_code=201)
async def create_capture_preview(
    background_tasks: BackgroundTasks,
    text: str | None = Form(default=None),
    account_id: str | None = Form(default=None),
    document_type: str = Form(
        default="auto",
        pattern="^(auto|receipt|boleto|bank_statement|credit_card|payroll)$",
    ),
    file: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    job_session_factory: Callable[[], Session] = Depends(get_capture_job_session_factory),
) -> dict:
    _require_admin(user)
    if not (text and text.strip()) and file is None:
        raise HTTPException(status_code=422, detail="Escreva uma mensagem ou envie um arquivo")
    if text and len(text) > 10000:
        raise HTTPException(status_code=413, detail="Mensagem maior que 10.000 caracteres")
    account = None
    if account_id:
        account = db.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.household_id == user.household_id,
                Account.active.is_(True),
            )
        )
        if not account:
            raise HTTPException(status_code=404, detail="Conta não encontrada")

    payload: bytes | None = None
    document: Document | None = None
    filename = file.filename if file else None
    content_type = file.content_type if file else None
    if file:
        payload = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(payload) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Arquivo maior que {settings.max_upload_mb} MB")
        if not payload:
            raise HTTPException(status_code=422, detail="O arquivo enviado está vazio")
        digest = file_sha256(payload)
        if db.scalar(
            select(Document.id).where(
                Document.household_id == user.household_id,
                Document.sha256 == digest,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Este arquivo já foi enviado anteriormente; abra a captura existente ou escolha outro arquivo",
            )
        document = Document(
            household_id=user.household_id,
            account_id=account.id if account else None,
            original_name=(filename or "captura")[:255],
            document_type="smart_capture",
            sha256=digest,
            encrypted_path="pending",
            status="capture_processing",
        )
        db.add(document)
        db.flush()
        document.encrypted_path = EncryptedDocumentStore().save(document.id, payload)

    job_type = capture_worker.classify_async_job_type(filename, content_type, has_payload=payload is not None)
    if job_type and settings.capture_async_processing_enabled:
        source_type = "audio" if job_type == "audio" else (
            "image" if (content_type or "").startswith("image/") else "document"
        )
        capture = CaptureDraft(
            household_id=user.household_id,
            user_id=user.id,
            document_id=document.id if document else None,
            source_type=source_type,
            detected_type=document_type,
            status="queued",
            processor="pending",
            original_text=(text or "")[:10000] or None,
            proposal_json="[]",
            confidence=Decimal("0"),
        )
        db.add(capture)
        db.flush()
        job = capture_worker.create_capture_job(
            db,
            household_id=user.household_id,
            capture_draft_id=capture.id,
            document_id=document.id if document else None,
            job_type=job_type,
            content_type=content_type,
            max_attempts=settings.capture_job_max_attempts,
        )
        audit(db, user, "capture.queued", "capture_draft", capture.id, {"job_type": job_type})
        db.commit()
        background_tasks.add_task(
            _run_capture_job_background, job.id, payload, session_factory=job_session_factory
        )
        return _capture_response(capture)

    capture = await _analyze_and_persist_capture(
        db,
        user,
        document,
        text=text,
        filename=filename,
        content_type=content_type,
        payload=payload,
        document_type=document_type,
        account=account,
    )
    db.commit()
    return _capture_response(capture)


@router.get("/captures")
def list_captures(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(CaptureDraft)
        .where(CaptureDraft.household_id == user.household_id)
        .order_by(CaptureDraft.created_at.desc())
        .limit(50)
    ).all()
    return [_capture_response(item) for item in rows]


@router.get("/captures/{capture_id}")
def get_capture(
    capture_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    item = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == capture_id,
            CaptureDraft.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Captura não encontrada")
    return _capture_response(item)


@router.delete("/captures/{capture_id}")
def cancel_capture(
    capture_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == capture_id,
            CaptureDraft.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Captura não encontrada")
    if item.status == "confirmed":
        raise HTTPException(
            status_code=409,
            detail="Uma captura confirmada não pode ser cancelada; ajuste os registros criados",
        )
    item.status = "cancelled"
    if item.document:
        item.document.status = "capture_cancelled"
    job = item.processing_job
    if job is not None and job.status not in {"completed", "failed"}:
        capture_worker.mark_job_failed(db, job, error_code="cancelled_by_user")
    audit(db, user, "capture.cancel", "capture_draft", item.id)
    db.commit()
    return {"ok": True}


@router.post("/captures/{capture_id}/retry")
async def retry_capture(
    capture_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    job_session_factory: Callable[[], Session] = Depends(get_capture_job_session_factory),
) -> dict:
    """Re-run OCR/audio processing for a capture whose worker job ended in
    `failed` -- the only user-facing path that moves a job back to
    `queued` (see `docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md` items 4-5).
    Never creates a new `CaptureDraft`/`Document`: it re-processes the
    exact same rows in place, exactly like the worker's own crash-recovery
    reclaim does."""

    _require_admin(user)
    capture = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == capture_id,
            CaptureDraft.household_id == user.household_id,
        )
    )
    if not capture:
        raise HTTPException(status_code=404, detail="Captura não encontrada")
    if capture.status == "cancelled":
        # `cancel_capture` marks an in-flight job "failed"
        # (`error_code="cancelled_by_user"`) as its own honest terminal
        # state, so `job.status == "failed"` alone cannot distinguish a
        # cancelled capture from a genuinely failed one here -- checking
        # the draft's own status is what stops `/retry` from being a
        # second path (besides the race `_process_claimed_capture_job`'s
        # atomic finalize already closes) to resurrect a cancelled capture.
        raise HTTPException(status_code=409, detail="Esta captura foi cancelada")
    job = capture.processing_job
    if job is None:
        raise HTTPException(
            status_code=409,
            detail="Esta captura não usa processamento assíncrono",
        )
    if job.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Só é possível reprocessar capturas com falha (status atual: {job.status})",
        )
    capture_worker.reset_job_for_retry(db, job)
    capture.status = "queued"
    capture.notes = None
    audit(db, user, "capture.retry", "capture_draft", capture.id, {"job_type": job.job_type, "attempts": job.attempts})
    db.commit()
    background_tasks.add_task(
        _run_capture_job_background, job.id, None, session_factory=job_session_factory
    )
    return _capture_response(capture)


@router.post("/captures/{capture_id}/confirm")
def confirm_capture(
    capture_id: str,
    payload: CaptureConfirmRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    capture = db.scalar(
        select(CaptureDraft).where(
            CaptureDraft.id == capture_id,
            CaptureDraft.household_id == user.household_id,
        )
    )
    if not capture:
        raise HTTPException(status_code=404, detail="Captura não encontrada")
    if capture.status == "confirmed":
        raise HTTPException(status_code=409, detail="Esta captura já foi confirmada")
    if capture.status == "cancelled":
        raise HTTPException(status_code=409, detail="Esta captura foi cancelada")
    if capture.status in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Esta captura ainda está sendo processada")
    if capture.status == "failed":
        raise HTTPException(
            status_code=409,
            detail="O processamento desta captura falhou; use /captures/{id}/retry antes de confirmar",
        )
    selected = [item for item in payload.items if item.selected]
    if not selected:
        raise HTTPException(status_code=422, detail="Selecione ao menos um item para confirmar")
    profile = profile_for(db, user.household_id)
    large_threshold = _large_entry_threshold(profile)
    if (
        not payload.confirmed_large_amount
        and any(item.kind == "transaction" and item.amount >= large_threshold for item in selected)
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Há um lançamento de valor elevado. Confirme que ele aconteceu de verdade; "
                f"simulações devem ser feitas no Consultor. Limite de confirmação: "
                f"R$ {large_threshold:,.2f}."
            ),
        )

    result: dict[str, list[str]] = {
        "transactions": [],
        "obligations": [],
        "payroll": [],
    }
    structured_document = capture.detected_type in {"bank_statement", "credit_card"}
    review_count = 0
    for proposal in selected:
        if proposal.kind == "transaction":
            account = db.scalar(
                select(Account).where(
                    Account.id == proposal.account_id,
                    Account.household_id == user.household_id,
                    Account.active.is_(True),
                )
            )
            if not account:
                raise HTTPException(
                    status_code=422,
                    detail=f"Escolha uma conta válida para ‘{proposal.description}’",
                )
            movement_type = proposal.movement_type or "expense"
            amount = (
                money(proposal.signed_amount)
                if movement_type in {"transfer", "reconciliation"} and proposal.signed_amount is not None
                else _capture_signed_amount(movement_type, proposal.amount)
            )
            excluded = False
            transaction_type = movement_type
            if movement_type == "expense":
                if proposal.category_id:
                    category = db.scalar(
                        select(Category).where(
                            Category.id == proposal.category_id,
                            Category.household_id == user.household_id,
                        )
                    )
                    if not category:
                        raise HTTPException(status_code=422, detail="Categoria inválida")
                else:
                    category = category_for(db, user.household_id, proposal.category_name or "Revisar")
            elif movement_type == "income":
                # Mirror the expense branch: the canonical classification
                # proposal (`parse_text_capture`/`_parsed_transaction_item`,
                # both household-rule-aware via `classify_with_local_rules`)
                # may have selected a category other than "Receitas" for an
                # active household income rule. Confirmation is the
                # persisted financial fact, so it must not silently discard
                # that in favor of a hardcoded default -- an unmatched
                # proposal already falls back to the classifier's own
                # conservative "Revisar" category, not "Receitas".
                if proposal.category_id:
                    category = db.scalar(
                        select(Category).where(
                            Category.id == proposal.category_id,
                            Category.household_id == user.household_id,
                        )
                    )
                    if not category:
                        raise HTTPException(status_code=422, detail="Categoria inválida")
                else:
                    category = category_for(db, user.household_id, proposal.category_name or "Revisar")
            elif movement_type in {"investment", "redemption"}:
                transaction_type = "transfer"
                excluded = True
                category = category_for(db, user.household_id, "Transferência patrimonial")
                # No `FinancialProfile.investment_balance` mutation here --
                # see `create_manual_transaction`'s "investment"/"redemption"
                # branches for why.
            elif movement_type == "transfer":
                transaction_type = "transfer"
                excluded = True
                category = category_for(db, user.household_id, "Transferência interna")
            elif movement_type == "reconciliation":
                transaction_type = "reconciliation"
                excluded = True
                category = category_for(db, user.household_id, "Conciliação")
            else:
                transaction_type = "refund"
                category = category_for(db, user.household_id, "Reembolsos e estornos")

            # INV-017 (docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md):
            # a confirmed capture is just another expense-creation path and
            # must resolve competence through the exact same canonical
            # helper `create_manual_transaction` uses -- never a second,
            # parallel `booked_at.strftime("%Y-%m")` calculation that would
            # ignore the account's configured card cycle. Non-expense
            # movements (income/transfer/reconciliation/refund) keep the
            # pre-existing date-based competence; the invoice-cycle concept
            # only applies to card purchases.
            #
            # Issue #84 (P0): a Smart Capture transaction proposal is never
            # supposed to carry its own `competence` -- the frontend only
            # renders that field for payroll items -- but `CaptureItemRequest`
            # does not forbid it, so nothing stops a future capture-generation
            # path (or an out-of-band client) from populating it. For a
            # card-account expense, forward it into the same canonical
            # resolver `create_manual_transaction` uses instead of discarding
            # it unread: `resolve_expense_competence` then fails closed (422)
            # on any value that diverges from the invoice cycle, so a
            # conflicting competence is explicitly rejected -- never silently
            # bypassed -- rather than only happening to be safe because this
            # call site never looked at it. Non-card expenses are unaffected:
            # only credit-card purchases have an invoice cycle to diverge
            # from (INV-017 scope).
            explicit_competence = (
                proposal.competence.strftime("%Y-%m")
                if proposal.competence is not None and account.account_type == "credit_card"
                else None
            )
            competence = (
                _resolve_expense_competence(
                    account=account,
                    competence=explicit_competence,
                    booked_at=proposal.booked_at,
                )
                if movement_type == "expense"
                else proposal.booked_at.strftime("%Y-%m")
            )

            parsed = ParsedTransaction(
                booked_at=proposal.booked_at,
                description=proposal.description,
                amount=money(amount),
                source_line=proposal.source_line or 1,
                card_last_four=proposal.card_last_four,
                installment_current=proposal.installment_current,
                installment_total=proposal.installment_total,
            )
            fingerprint = transaction_fingerprint(account.id, parsed, account.owner_label)
            transaction_trace_id = str(uuid.uuid4())
            transaction = Transaction(
                household_id=user.household_id,
                account_id=account.id,
                document_id=capture.document_id if structured_document else None,
                category_id=category.id,
                booked_at=proposal.booked_at,
                description=proposal.description,
                normalized_description=normalize_description(proposal.description),
                amount=money(amount),
                transaction_type=transaction_type,
                owner_label=account.owner_label,
                card_last_four=proposal.card_last_four or account.last_four,
                installment_current=proposal.installment_current,
                installment_total=proposal.installment_total,
                fingerprint=fingerprint,
                source_line=proposal.source_line,
                occurred_at=proposal.booked_at,
                competence=competence,
                classification_source="capture_confirmed",
                classification_version=PARSER_CONTRACT_VERSION,
                canonical_status="unassigned",
                trace_id=transaction_trace_id,
                source_priority=source_priority("capture"),
                confidence=Decimal("1"),
                excluded=excluded,
                possible_duplicate=False,
                reviewed=True,
            )
            db.add(transaction)
            db.flush()
            duplicate_group, duplicate_assessment = register_transaction_duplicates(
                db,
                transaction=transaction,
                household_id=user.household_id,
            )
            duplicate = duplicate_group is not None
            if proposal.funding_source == "privilege":
                if proposal.movement_type != "expense":
                    raise HTTPException(
                        status_code=422,
                        detail="Privilège DI como origem só se aplica a despesas.",
                    )
                if structured_document:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Não é seguro descontar Privilège automaticamente ao confirmar "
                            "extrato/fatura. Use essa origem em lançamento avulso/recibo."
                        ),
                    )
                if duplicate:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A captura foi identificada como possível duplicidade. "
                            "Resolva antes de descontar o Privilège DI."
                        ),
                    )
                _fund_expense_from_privilege(
                    db,
                    user=user,
                    expense_transaction=transaction,
                    expense_account=account,
                    booked_at=proposal.booked_at,
                )
            transaction.reviewed = not duplicate
            result["transactions"].append(transaction.id)
            if duplicate:
                db.add(
                    ReviewItem(
                        household_id=user.household_id,
                        transaction_id=transaction.id,
                        document_id=capture.document_id if structured_document else None,
                        reason="possible_duplicate",
                        details=(
                            "Captura confirmada agrupada como possível repetição "
                            f"({duplicate_assessment.band}, confiança "
                            f"{duplicate_assessment.confidence})"
                            if duplicate_assessment
                            else "Captura confirmada coincide com um lançamento existente"
                        ),
                    )
                )
                review_count += 1
        elif proposal.kind == "obligation":
            obligation = Obligation(
                household_id=user.household_id,
                name=proposal.description,
                due_date=proposal.due_date,
                amount=money(proposal.amount),
                recurrence_months=proposal.recurrence_months,
                occurrence_count=proposal.occurrence_count,
                category=(proposal.category_name or "general")[:60],
                active=True,
            )
            db.add(obligation)
            db.flush()
            result["obligations"].append(obligation.id)
        else:
            record = PayrollRecord(
                household_id=user.household_id,
                document_id=capture.document_id,
                person_name=proposal.description,
                competence=proposal.competence,
                payment_date=proposal.payment_date,
                payroll_kind=proposal.payroll_kind,
                gross_amount=money(proposal.amount),
                deductions=Decimal("0"),
                net_amount=money(proposal.amount),
                payroll_loan=Decimal("0"),
                notes="Confirmado pela central de captura inteligente",
            )
            db.add(record)
            db.flush()
            result["payroll"].append(record.id)

    capture.status = "confirmed"
    capture.proposal_json = json.dumps(
        [item.model_dump(mode="json") for item in payload.items], ensure_ascii=False
    )
    capture.result_json = json.dumps(result)
    capture.confirmed_at = datetime.now(UTC)
    if capture.document:
        capture.document.status = "imported_with_review" if review_count else "imported"
        capture.document.record_count = len(selected)
    audit(
        db,
        user,
        "capture.confirm",
        "capture_draft",
        capture.id,
        {
            "transactions": len(result["transactions"]),
            "obligations": len(result["obligations"]),
            "payroll": len(result["payroll"]),
            "review_items": review_count,
        },
    )
    db.commit()
    return {"ok": True, "result": result, "review_items": review_count}


_LEDGER_PATRIMONIAL_CATEGORY_NAME = "Transferência patrimonial"
_LEDGER_MOVEMENT_TYPES = frozenset(
    {"income", "expense", "transfer", "investment", "redemption", "refund", "reconciliation"}
)
_LEDGER_ORIGINS = frozenset({"manual", "capture", "import", "legacy"})
_LEDGER_ORIGIN_LABELS = {
    "manual_confirmed": "Lançamento estruturado",
    "capture_confirmed": "Lançar agora",
    "local_rule": "Importação · regra local",
    "builtin_rule": "Importação · regra padrão",
    "financial_plan_workbook": "Carga inicial da planilha",
    "legacy": "Origem legada",
}


def _ledger_movement_type(transaction_type: str, category_name: str | None, amount: Decimal) -> str:
    """Read-only `movement_type` label for the livro-razão (go-live manual
    slice 4, `docs/WORK_ORDER_MANUAL_LEDGER.md`).

    `Transaction.transaction_type` only stores five values
    (`income|expense|transfer|refund|reconciliation`); `investment` and
    `redemption` are both persisted as `type="transfer"` + category
    "Transferência patrimonial", distinguished only by the sign of the
    already-computed `amount` (see the "investment"/"redemption" branches of
    `create_manual_transaction` and `confirm_capture`). This function is the
    single place that reverses that mapping for display/filtering -- it does
    not compute a new financial fact, only renders one that the canonical
    write path already decided, so the ledger never re-derives it a second
    time in the frontend.
    """
    if transaction_type != "transfer":
        return transaction_type
    if category_name == _LEDGER_PATRIMONIAL_CATEGORY_NAME:
        return "investment" if amount < 0 else "redemption"
    return "transfer"


@router.get("/transactions")
def transactions(
    month: str | None = None,
    review_only: bool = False,
    movement_type: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Livro-razão unificado de consulta/auditoria (go-live manual slice 4,
    `docs/WORK_ORDER_MANUAL_LEDGER.md` /
    `docs/GO_LIVE_MANUAL_UX_PLAN.md#lançamentos`).

    Still the same read-only query over `Transaction` every canonical
    command from slices 1-3 already writes (Entradas/Saídas, Transferências/
    Aplicação/Resgate, Contas a pagar/pagamento de fatura, Lançar agora,
    importação) -- this only adds filters and provenance fields, never a
    second calculation. `movement_type` filters on the same seven-value
    vocabulary already established by `ManualTransactionRequest`/
    `CaptureItemRequest` (see `_ledger_movement_type`); `origin` filters on
    the already-persisted `classification_source` channel each canonical
    command already tags its own rows with.

    Every cross-reference this endpoint can expose by name --
    `account_id`/`category_id`/`document_id`/`linked_transaction_id` -- is
    re-resolved through a household-scoped lookup before being surfaced. A
    row whose reference happens to point at another household (a corrupted/
    inconsistent reference that no canonical command produces, but that must
    never be trusted blindly either -- see
    `tests/test_manual_payables_card_payment.py`'s
    `test_card_invoice_obligation_stays_pending_for_cross_household_link_reference`
    for the established pattern this mirrors) is treated as unresolved
    rather than leaking that other household's name/description/id.
    """
    if movement_type is not None and movement_type not in _LEDGER_MOVEMENT_TYPES:
        raise HTTPException(status_code=422, detail="Tipo de movimentação inválido")
    if origin is not None and origin not in _LEDGER_ORIGINS:
        raise HTTPException(status_code=422, detail="Origem inválida")

    query = select(Transaction).where(Transaction.household_id == user.household_id)
    if month:
        start = _month_start(month)
        query = query.where(Transaction.booked_at >= start, Transaction.booked_at < add_months(start, 1))
    if review_only:
        query = query.where(Transaction.reviewed.is_(False))
    if movement_type in {"income", "expense", "refund", "reconciliation"}:
        query = query.where(Transaction.transaction_type == movement_type)
    elif movement_type in {"transfer", "investment", "redemption"}:
        # Single canonical predicate for the patrimonial (investment/redemption)
        # subset, shared by both the "transfer" and "investment"/"redemption"
        # branches below, so this filter can never diverge from
        # `_ledger_movement_type` the way an independent SQL predicate did
        # before (see PR #50 review): that helper labels a `transfer` row as
        # "transfer" for *any* category that isn't a household-scoped
        # "Transferência patrimonial" match -- including a legacy/different
        # category name, an unresolved reference, or no category at all --
        # never only rows tagged "Transferência interna".
        patrimonial_ids = (
            select(Transaction.id)
            .join(Category, Transaction.category_id == Category.id)
            .where(
                Transaction.household_id == user.household_id,
                Transaction.transaction_type == "transfer",
                Category.household_id == user.household_id,
                Category.name == _LEDGER_PATRIMONIAL_CATEGORY_NAME,
            )
        )
        if movement_type == "transfer":
            query = query.where(
                Transaction.transaction_type == "transfer",
                Transaction.id.notin_(patrimonial_ids),
            )
        else:
            query = query.where(
                Transaction.transaction_type == "transfer",
                Transaction.id.in_(patrimonial_ids),
                # Sign boundary must match `_ledger_movement_type` exactly
                # (see PR #50 review, BLOQUEIO DE MERGE #2): that helper's
                # `else` branch -- `amount < 0` -> "investment", anything
                # else (including `amount == 0`) -> "redemption" -- is not
                # reachable by two independent open intervals. A patrimonial
                # row with `amount == 0` (never produced by the current
                # manual/capture commands, which both require `amount > 0`,
                # but not excluded by the persisted schema and therefore a
                # possible legacy/inconsistent fact) must land on the same
                # side here as it does in the unfiltered label.
                Transaction.amount < 0 if movement_type == "investment" else Transaction.amount >= 0,
            )
    if origin == "manual":
        query = query.where(Transaction.classification_source == "manual_confirmed")
    elif origin == "capture":
        query = query.where(Transaction.classification_source == "capture_confirmed")
    elif origin == "import":
        query = query.where(
            Transaction.classification_source.in_(("local_rule", "builtin_rule", "financial_plan_workbook"))
        )
    elif origin == "legacy":
        query = query.where(Transaction.classification_source == "legacy")

    rows = db.scalars(
        query.order_by(Transaction.booked_at.desc(), Transaction.created_at.desc()).limit(min(limit, 1000))
    ).all()

    account_ids = {item.account_id for item in rows if item.account_id}
    accounts_by_id = (
        {
            account.id: account
            for account in db.scalars(
                select(Account).where(Account.id.in_(account_ids), Account.household_id == user.household_id)
            ).all()
        }
        if account_ids
        else {}
    )
    category_ids = {item.category_id for item in rows if item.category_id}
    categories_by_id = (
        {
            category.id: category
            for category in db.scalars(
                select(Category).where(Category.id.in_(category_ids), Category.household_id == user.household_id)
            ).all()
        }
        if category_ids
        else {}
    )
    document_ids = {item.document_id for item in rows if item.document_id}
    documents_by_id = (
        {
            document.id: document
            for document in db.scalars(
                select(Document).where(Document.id.in_(document_ids), Document.household_id == user.household_id)
            ).all()
        }
        if document_ids
        else {}
    )
    linked_ids = {item.linked_transaction_id for item in rows if item.linked_transaction_id}
    linked_by_id = (
        {
            linked.id: linked
            for linked in db.scalars(
                select(Transaction).where(
                    Transaction.id.in_(linked_ids), Transaction.household_id == user.household_id
                )
            ).all()
        }
        if linked_ids
        else {}
    )

    result = []
    for item in rows:
        account = accounts_by_id.get(item.account_id) if item.account_id else None
        category = categories_by_id.get(item.category_id) if item.category_id else None
        document = documents_by_id.get(item.document_id) if item.document_id else None
        linked = linked_by_id.get(item.linked_transaction_id) if item.linked_transaction_id else None
        category_name = category.name if category else "Revisar"
        result.append(
            {
                "id": item.id,
                "date": item.booked_at,
                "description": item.description,
                "amount": decimal_value(item.amount),
                "type": item.transaction_type,
                "movement_type": _ledger_movement_type(item.transaction_type, category_name, item.amount),
                "category_id": category.id if category else None,
                "category": category_name,
                "owner": item.owner_label,
                "account": account.name if account else "",
                "account_id": account.id if account else None,
                "excluded": item.excluded,
                "possible_duplicate": item.possible_duplicate,
                "canonical_status": item.canonical_status,
                "duplicate_group_id": item.duplicate_group_id,
                "reviewed": item.reviewed,
                "confidence": decimal_value(item.confidence),
                "competence": item.competence,
                "installment": (
                    f"{item.installment_current}/{item.installment_total}"
                    if item.installment_current and item.installment_total
                    else None
                ),
                "manual": item.document_id is None,
                "classification_source": item.classification_source,
                "origin_label": _LEDGER_ORIGIN_LABELS.get(
                    item.classification_source, item.classification_source
                ),
                "document_id": document.id if document else None,
                "document_name": document.original_name if document else None,
                "document_type": document.document_type if document else None,
                "transfer_group_id": item.transfer_group_id,
                "linked_transaction_id": linked.id if linked else None,
                "linked_description": linked.description if linked else None,
                "linked_date": linked.booked_at if linked else None,
                "linked_amount": decimal_value(linked.amount) if linked else None,
            }
        )
    return result


# FAMILY_FINANCE_PRIVILEGE_FUNDING_V15
def _fund_expense_from_privilege(
    db: Session,
    *,
    user: User,
    expense_transaction: Transaction,
    expense_account: Account,
    booked_at: date,
) -> Decimal:
    if expense_account.account_type == "credit_card":
        raise HTTPException(
            status_code=422,
            detail=(
                "Compra no cartão não pode descontar o Privilège no momento da compra. "
                "Informe a origem do recurso quando a fatura for paga."
            ),
        )
    if expense_account.account_type not in {"checking", "cash"}:
        raise HTTPException(
            status_code=422,
            detail="Privilège DI como origem só pode financiar despesa paga por conta corrente ou dinheiro.",
        )

    privilege = _privilege_account(db, user.household_id)
    current = _latest_active_balance_observation(
        db, household_id=user.household_id, account_id=privilege.id
    )
    if current is None:
        raise HTTPException(
            status_code=409,
            detail="Não existe saldo confirmado ativo para o Privilège DI.",
        )
    if current.as_of_date > booked_at:
        raise HTTPException(
            status_code=409,
            detail=(
                "Existe saldo do Privilège confirmado depois da data desta despesa. "
                "Não vou descontar retroativamente para evitar dupla contagem."
            ),
        )

    amount = money(abs(expense_transaction.amount))
    current_amount = money(current.amount)
    if current_amount < amount:
        raise HTTPException(
            status_code=409,
            detail=f"Saldo confirmado do Privilège ({current_amount}) é menor que a despesa ({amount}).",
        )

    new_amount = money(current_amount - amount)
    trace_id = expense_transaction.trace_id or str(uuid.uuid4())
    expense_transaction.trace_id = trace_id
    expense_transaction.transfer_group_id = trace_id

    category = category_for(db, user.household_id, "Transferência patrimonial")
    description = f"Resgate Privilège DI para despesa: {expense_transaction.description}"
    parsed = ParsedTransaction(
        booked_at=booked_at,
        description=description,
        amount=amount,
        source_line=1,
        card_last_four=privilege.last_four,
        occurred_at=booked_at,
    )
    funding_tx = Transaction(
        household_id=user.household_id,
        account_id=privilege.id,
        category_id=category.id,
        booked_at=booked_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=amount,
        transaction_type="transfer",
        owner_label=privilege.owner_label,
        card_last_four=privilege.last_four,
        fingerprint=transaction_fingerprint(privilege.id, parsed, privilege.owner_label),
        source_line=1,
        occurred_at=booked_at,
        competence=booked_at.strftime("%Y-%m"),
        classification_source="manual_confirmed",
        classification_version=PARSER_CONTRACT_VERSION,
        canonical_status="unassigned",
        linked_transaction_id=expense_transaction.id,
        transfer_group_id=trace_id,
        trace_id=trace_id,
        source_priority=source_priority("manual"),
        confidence=Decimal("1"),
        excluded=True,
        possible_duplicate=False,
        reviewed=True,
    )
    db.add(funding_tx)
    db.flush()
    expense_transaction.linked_transaction_id = funding_tx.id

    balance = AccountBalanceObservation(
        household_id=user.household_id,
        account_id=privilege.id,
        amount=new_amount,
        as_of_date=booked_at,
        observation_type="point_in_time",
        source="manual_confirmed",
        document_id=None,
        confirmed_by=user.id,
        confidence=Decimal("1.0000"),
        supersedes_id=current.id,
        trace_id=trace_id,
    )
    db.add(balance)
    db.flush()
    current.superseded_by_id = balance.id

    audit(
        db,
        user,
        "transaction.funding.privilege",
        "transaction",
        expense_transaction.id,
        {
            "expense_amount": str(amount),
            "expense_account_id": expense_account.id,
            "privilege_account_id": privilege.id,
            "funding_transaction_id": funding_tx.id,
            "balance_observation_id": balance.id,
            "balance_before": str(current_amount),
            "balance_after": str(new_amount),
        },
        reason="Usuário confirmou que a despesa foi paga com recurso retirado do Privilège DI.",
        trace_id=trace_id,
        source="manual_expense",
    )
    return new_amount


@router.post("/transactions", status_code=201)
def create_manual_transaction(
    payload: ManualTransactionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    account = db.scalar(
        select(Account).where(
            Account.id == payload.account_id,
            Account.household_id == user.household_id,
            Account.active.is_(True),
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    profile = profile_for(db, user.household_id)
    large_threshold = _large_entry_threshold(profile)
    if payload.amount >= large_threshold and not payload.confirmed_large_amount:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Este valor exige confirmação adicional. Confirme apenas se a movimentação "
                f"aconteceu de verdade; use o Consultor para simulações. Limite de confirmação: "
                f"R$ {large_threshold:,.2f}."
            ),
        )

    category = None
    transaction_type = "expense"
    amount = -abs(payload.amount)
    excluded = False
    if payload.movement_type == "expense":
        if payload.category_id:
            category = db.scalar(
                select(Category).where(
                    Category.id == payload.category_id,
                    Category.household_id == user.household_id,
                )
            )
            if not category:
                raise HTTPException(status_code=404, detail="Categoria não encontrada")
        elif payload.category_name:
            custom_name = " ".join(payload.category_name.split())
            category = db.scalar(
                select(Category).where(
                    Category.household_id == user.household_id,
                    func.lower(Category.name) == custom_name.lower(),
                )
            )
            if not category:
                category = Category(
                    household_id=user.household_id,
                    name=custom_name,
                    color="#64748B",
                    cash_cap=Decimal("0"),
                    essential=False,
                )
                db.add(category)
                db.flush()
        else:
            raise HTTPException(status_code=422, detail="Escolha ou crie uma categoria para a despesa")
    elif payload.movement_type == "income":
        transaction_type = "income"
        amount = abs(payload.amount)
        category = category_for(db, user.household_id, "Receitas")
    elif payload.movement_type == "investment":
        transaction_type = "transfer"
        excluded = True
        category = category_for(db, user.household_id, "Transferência patrimonial")
        # Deliberately does not touch `FinancialProfile.investment_balance`.
        # docs/INTEGRITY_IMPLEMENTATION_PLAN.md documents that field as an
        # unreconciled mutable counter outside the ledger, and the Work
        # Order for this slice forbids using investment/redemption to
        # silently change it. The `Transaction` recorded below is the
        # canonical, auditable fact; the household's investment balance
        # remains whatever it last explicitly declared via `PUT /profile`,
        # or is derived from an `AccountBalanceObservation` when one
        # exists (see `_opening_balance` in financial_snapshots.py).
    elif payload.movement_type == "redemption":
        transaction_type = "transfer"
        amount = abs(payload.amount)
        excluded = True
        category = category_for(db, user.household_id, "Transferência patrimonial")
        # See the "investment" branch above: no `FinancialProfile` mutation.
    elif payload.movement_type == "refund":
        transaction_type = "refund"
        amount = abs(payload.amount)
        category = category_for(db, user.household_id, "Reembolsos e estornos")

    if payload.movement_type == "expense":
        competence = _resolve_expense_competence(
            account=account,
            competence=payload.competence,
            booked_at=payload.booked_at,
        )
    else:
        competence = payload.booked_at.strftime("%Y-%m")

    parsed = ParsedTransaction(
        booked_at=payload.booked_at,
        description=payload.description,
        amount=money(amount),
        source_line=1,
        card_last_four=account.last_four,
        installment_current=payload.installment_current,
        installment_total=payload.installment_total,
        occurred_at=payload.booked_at,
    )
    transaction_trace_id = str(uuid.uuid4())
    transaction = Transaction(
        household_id=user.household_id,
        account_id=account.id,
        category_id=category.id,
        booked_at=payload.booked_at,
        description=payload.description,
        normalized_description=normalize_description(payload.description),
        amount=money(amount),
        transaction_type=transaction_type,
        owner_label=account.owner_label,
        card_last_four=account.last_four,
        installment_current=payload.installment_current,
        installment_total=payload.installment_total,
        fingerprint=transaction_fingerprint(account.id, parsed, account.owner_label),
        occurred_at=payload.booked_at,
        competence=competence,
        classification_source="manual_confirmed",
        classification_version=PARSER_CONTRACT_VERSION,
        canonical_status="unassigned",
        trace_id=transaction_trace_id,
        source_priority=source_priority("manual"),
        confidence=Decimal("1"),
        excluded=excluded,
        reviewed=True,
    )
    db.add(transaction)
    db.flush()
    duplicate_group, duplicate_assessment = register_transaction_duplicates(
        db,
        transaction=transaction,
        household_id=user.household_id,
    )
    if duplicate_group is not None:
        transaction.reviewed = False
        db.add(
            ReviewItem(
                household_id=user.household_id,
                transaction_id=transaction.id,
                reason="possible_duplicate",
                details=(
                    "Lançamento manual agrupado como possível repetição "
                    f"({duplicate_assessment.band}, confiança "
                    f"{duplicate_assessment.confidence})"
                    if duplicate_assessment
                    else "Lançamento manual coincide com um lançamento existente"
                ),
            )
        )
    funding_balance_after = None
    if payload.movement_type == "expense" and payload.funding_source == "privilege":
        if duplicate_group is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A despesa foi identificada como possível duplicidade. "
                    "Resolva a duplicidade antes de descontar o Privilège DI."
                ),
            )
        funding_balance_after = _fund_expense_from_privilege(
            db,
            user=user,
            expense_transaction=transaction,
            expense_account=account,
            booked_at=payload.booked_at,
        )

    audit(
        db,
        user,
        "transaction.create_manual",
        "transaction",
        transaction.id,
        {
            "movement_type": payload.movement_type,
            "amount": str(transaction.amount),
            "account_id": account.id,
            "competence": transaction.competence,
            "competence_explicitly_confirmed": payload.competence is not None,
            "funding_source": payload.funding_source,
            "privilege_balance_after": (
                str(funding_balance_after) if funding_balance_after is not None else None
            ),
        },
    )
    db.commit()
    return {"id": transaction.id}


@router.post("/transfers", status_code=201)
def create_manual_transfer(
    payload: TransferRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Structured transfer between two accounts of the same household.

    `docs/GO_LIVE_MANUAL_UX_PLAN.md` ("Transferências") and
    `docs/WORK_ORDER_MANUAL_TRANSFERS_INVESTMENT_REDEMPTION.md`: the only
    canonical command that creates a linked pair of `transaction_type =
    "transfer"` rows (INV-001) -- origin and destination are both explicit,
    human-confirmed account ids from this household; neither is inferred.
    Reuses `create_internal_transfer` (`app/services/transfers.py`), the
    same category (`Transferência interna`, already used by the Central
    Inteligente `transfer` proposal in `confirm_capture`) and the same
    duplicate-detection/audit building blocks every other manual command
    already uses -- no parallel financial engine.
    """
    _require_admin(user)
    from_account = db.scalar(
        select(Account).where(
            Account.id == payload.from_account_id,
            Account.household_id == user.household_id,
            Account.active.is_(True),
        )
    )
    if not from_account:
        raise HTTPException(status_code=404, detail="Conta de origem não encontrada")
    to_account = db.scalar(
        select(Account).where(
            Account.id == payload.to_account_id,
            Account.household_id == user.household_id,
            Account.active.is_(True),
        )
    )
    if not to_account:
        raise HTTPException(status_code=404, detail="Conta de destino não encontrada")

    profile = profile_for(db, user.household_id)
    large_threshold = _large_entry_threshold(profile)
    if payload.amount >= large_threshold and not payload.confirmed_large_amount:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Este valor exige confirmação adicional. Confirme apenas se a transferência "
                f"aconteceu de verdade; use o Consultor para simulações. Limite de confirmação: "
                f"R$ {large_threshold:,.2f}."
            ),
        )

    category = category_for(db, user.household_id, "Transferência interna")
    try:
        result = create_internal_transfer(
            db,
            household_id=user.household_id,
            from_account=from_account,
            to_account=to_account,
            category=category,
            amount=payload.amount,
            booked_at=payload.booked_at,
            description=payload.description,
        )
    except TransferError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    review_count = 0
    for leg in (result.debit, result.credit):
        if leg.duplicate_assessment is not None:
            review_count += 1
            db.add(
                ReviewItem(
                    household_id=user.household_id,
                    transaction_id=leg.transaction.id,
                    reason="possible_duplicate",
                    details=(
                        "Transferência manual agrupada como possível repetição "
                        f"({leg.duplicate_assessment.band}, confiança "
                        f"{leg.duplicate_assessment.confidence})"
                    ),
                )
            )

    audit(
        db,
        user,
        "transfer.create",
        "transaction",
        result.debit.transaction.id,
        {
            "transfer_group_id": result.transfer_group_id,
            "from_account_id": from_account.id,
            "to_account_id": to_account.id,
            "amount": str(money(payload.amount)),
            "debit_transaction_id": result.debit.transaction.id,
            "credit_transaction_id": result.credit.transaction.id,
        },
        trace_id=result.transfer_group_id,
    )
    db.commit()
    return {
        "transfer_group_id": result.transfer_group_id,
        "from_transaction_id": result.debit.transaction.id,
        "to_transaction_id": result.credit.transaction.id,
        "review_items": review_count,
    }


@router.get("/transactions/manual/installment-preview")
def preview_manual_installment(
    account_id: str = Query(...),
    description: str = Query(..., min_length=2, max_length=500),
    amount: Decimal = Query(..., gt=0),
    booked_at: date = Query(...),
    installment_current: int = Query(..., ge=1, le=999),
    installment_total: int = Query(..., ge=1, le=999),
    competence: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only preview for the `Saídas` installment fields, shown before
    confirmation (`docs/GO_LIVE_MANUAL_UX_PLAN.md`): the per-installment
    value, the remaining schedule of future months, and how that schedule
    changes the canonical projection's future `installments` load per month.

    `account_id` is required so the prévia can apply the exact same
    competence policy `create_manual_transaction` will enforce when the
    purchase is actually confirmed (`_resolve_expense_competence`): a card
    account requires an explicitly confirmed invoice competence (INV-017,
    which may diverge from `booked_at`'s month), while every other account
    type is always anchored on `booked_at`'s month and rejects a diverging
    `competence`. Without knowing the account, the prévia could show a
    schedule anchored on a month the POST would refuse -- this endpoint
    must never carry a second, looser policy.

    `description` is required too (engineering review on PR 47, head
    `0f82ce8`): the "before/after" comparison simulates the candidate
    purchase as an in-memory item alongside the already-persisted rows and
    runs both through `_project_installments` -- the exact series
    identification and latest-observation-wins logic `_future_installments`
    always applies. Without `description` the candidate could never be
    recognized as the *next observed installment of an existing series*, so
    a naive `baseline + schedule` would double-count a commitment that
    `_future_installments` would actually replace once persisted (this was
    the bug the review caught). No transaction is created and no state is
    written; the projection engine
    (`app.services.projection_engine.build_projection`) consumes the exact
    same `_future_installments` once the purchase is actually persisted --
    the UI never computes any of this itself.
    """
    if installment_current > installment_total:
        raise HTTPException(
            status_code=422, detail="A parcela atual não pode ser maior que o total de parcelas"
        )
    account = db.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.household_id == user.household_id,
            Account.active.is_(True),
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    resolved_competence = _resolve_expense_competence(
        account=account,
        competence=competence,
        booked_at=booked_at,
    )
    anchor_month = _installment_anchor_month(competence=resolved_competence, booked_at=booked_at)
    schedule = _installment_remaining_schedule(
        amount=amount,
        installment_current=installment_current,
        installment_total=installment_total,
        anchor_month=anchor_month,
    )
    persisted_rows = _persisted_installment_rows(db, user.household_id)
    candidate = SimpleNamespace(
        account_id=account.id,
        card_last_four=account.last_four,
        description=description,
        amount=money(abs(amount)),
        installment_current=installment_current,
        installment_total=installment_total,
        competence=resolved_competence,
        booked_at=booked_at,
    )
    baseline = _project_installments(persisted_rows)
    projected = _project_installments([*persisted_rows, candidate])
    affected_months = sorted(set(baseline) | set(projected) | {month for month, _ in schedule})
    return {
        "monthly_payment": decimal_value(money(abs(amount))),
        "installment_current": installment_current,
        "installment_total": installment_total,
        "schedule": [{"month": month, "amount": decimal_value(value)} for month, value in schedule],
        "canonical_projection_effect": [
            {
                "month": month,
                "installments_before": decimal_value(baseline.get(month, Decimal("0"))),
                "installments_after": decimal_value(projected.get(month, Decimal("0"))),
            }
            for month in affected_months
        ],
    }


@router.patch("/transactions/{transaction_id}")
def update_transaction(
    transaction_id: str,
    payload: TransactionUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    transaction = db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.household_id == user.household_id
        )
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    before_state = {
        "category_id": transaction.category_id,
        "excluded": transaction.excluded,
        "possible_duplicate": transaction.possible_duplicate,
        "reviewed": transaction.reviewed,
        "owner_label": transaction.owner_label,
    }
    changes = payload.model_dump(exclude_unset=True)
    is_linked_reconciliation = (
        transaction.linked_transaction_id is not None and transaction.transaction_type == "reconciliation"
    )
    linked_paid_obligation = db.scalar(
        select(Obligation).where(
            Obligation.household_id == user.household_id,
            Obligation.paid_transaction_id == transaction.id,
            Obligation.active.is_(True),
            Obligation.status == "paid",
        )
    )
    if linked_paid_obligation and (
        changes.get("excluded") is True or changes.get("possible_duplicate") is True
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Este lançamento liquida uma obrigação paga. Desfaça o pagamento da obrigação "
                "antes de removê-lo dos totais ou marcá-lo como possível duplicidade"
            ),
        )
    if transaction.transfer_group_id is not None and (
        "excluded" in changes or "category_id" in changes
    ):
        # Both legs of a structured transfer (`POST /transfers`) must stay
        # symmetric -- INV-001 depends on the pair having the same
        # `excluded`/category treatment. Editing only one leg's `excluded`
        # or `category_id` would silently break that pairing (one side
        # still zero-effect, the other now counted, or reclassified away
        # from "Transferência interna" while its twin isn't) without
        # touching the other leg, which this endpoint has no way to do
        # atomically. `reviewed`/`possible_duplicate`/`owner_label` stay
        # editable -- they carry no operational-effect meaning.
        raise HTTPException(
            status_code=422,
            detail=(
                "Uma perna de transferência não pode ter categoria ou inclusão nos totais "
                "alteradas isoladamente; exclua a transferência e registre novamente se o "
                "vínculo estiver incorreto"
            ),
        )
    if is_linked_reconciliation and "excluded" in changes:
        # INV-002: a linked card-payment-reconciliation leg (imported or
        # created by `pay_card_invoice`) must stay excluded from every
        # operating total forever -- flipping `excluded` to `False` here
        # would turn a quitação fact into a fabricated second expense for
        # the same invoice. `category_id`/`reviewed`/`possible_duplicate`/
        # `owner_label` carry no INV-002 meaning and stay editable.
        raise HTTPException(
            status_code=422,
            detail=(
                "Um lançamento de conciliação vinculado não pode ter sua inclusão nos totais "
                "alterada; desvincule o pagamento da fatura antes, se o vínculo estiver incorreto"
            ),
        )
    if "category_id" in changes and changes["category_id"]:
        category = db.scalar(
            select(Category).where(
                Category.id == changes["category_id"], Category.household_id == user.household_id
            )
        )
        if not category:
            raise HTTPException(status_code=404, detail="Categoria não encontrada")
    for field, value in changes.items():
        setattr(transaction, field, value)
    if changes.get("reviewed"):
        reviews = db.scalars(
            select(ReviewItem).where(ReviewItem.transaction_id == transaction.id, ReviewItem.status == "open")
        ).all()
        for item in reviews:
            item.status = "resolved"
            item.resolved_at = datetime.now(UTC)
    learned_rule = None
    if (
        changes.get("reviewed") is True
        and changes.get("category_id")
        and changes["category_id"] != before_state["category_id"]
    ):
        learned_rule = record_confirmed_correction(
            db,
            household_id=user.household_id,
            transaction_id=transaction.id,
            description=transaction.description,
            category_id=changes["category_id"],
            movement_type=transaction.transaction_type,
        )
    after_state = {
        "category_id": transaction.category_id,
        "excluded": transaction.excluded,
        "possible_duplicate": transaction.possible_duplicate,
        "reviewed": transaction.reviewed,
        "owner_label": transaction.owner_label,
        "classification_rule_id": learned_rule.id if learned_rule else None,
    }
    audit(
        db,
        user,
        "transaction.update",
        "transaction",
        transaction.id,
        changes,
        before_state=before_state,
        after_state=after_state,
        reason="Correção confirmada pelo usuário" if changes.get("reviewed") else None,
        trace_id=transaction.trace_id,
    )
    db.commit()
    return {"ok": True}


# FAMILY_FINANCE_PRIVILEGE_DELETE_ROLLBACK_V15_4
def _rollback_privilege_balance_for_deleted_transaction(
    db: Session,
    *,
    user: User,
    transaction: Transaction,
) -> bool:
    group_id = transaction.transfer_group_id or transaction.trace_id
    if not group_id:
        return False

    observations = list(
        db.scalars(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.household_id == user.household_id,
                AccountBalanceObservation.trace_id == group_id,
                AccountBalanceObservation.invalidated_at.is_(None),
            )
        ).all()
    )
    if not observations:
        return False
    if len(observations) != 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "Existem múltiplas observações de saldo vinculadas a este lançamento. "
                "A exclusão foi bloqueada para evitar inconsistência."
            ),
        )

    balance = observations[0]
    if balance.superseded_by_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Existe um saldo do Privilège confirmado depois deste lançamento. "
                "Não é seguro excluir automaticamente."
            ),
        )

    if not balance.supersedes_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "A observação do Privilège não possui saldo anterior para restaurar. "
                "A exclusão foi bloqueada."
            ),
        )

    previous = db.scalar(
        select(AccountBalanceObservation).where(
            AccountBalanceObservation.id == balance.supersedes_id,
            AccountBalanceObservation.household_id == user.household_id,
        )
    )
    if previous is None:
        raise HTTPException(
            status_code=409,
            detail="Saldo anterior do Privilège não encontrado; exclusão bloqueada.",
        )

    previous.superseded_by_id = None
    balance.invalidated_at = datetime.now(UTC)
    balance.invalidated_by = user.id

    audit(
        db,
        user,
        "transaction.funding.privilege.rollback",
        "transaction",
        transaction.id,
        {
            "trace_id": group_id,
            "balance_observation_id": balance.id,
            "restored_observation_id": previous.id,
            "balance_before_rollback": str(money(balance.amount)),
            "balance_after_rollback": str(money(previous.amount)),
        },
        reason="Despesa financiada pelo Privilège foi excluída; saldo confirmado restaurado.",
        trace_id=group_id,
        source="transaction_delete",
    )
    return True


@router.delete("/transactions/{transaction_id}")
def delete_manual_transaction(
    transaction_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    transaction = db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.household_id == user.household_id,
        )
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    linked_paid_obligation = db.scalar(
        select(Obligation).where(
            Obligation.household_id == user.household_id,
            Obligation.paid_transaction_id == transaction.id,
            Obligation.active.is_(True),
            Obligation.status == "paid",
        )
    )
    if linked_paid_obligation:
        raise HTTPException(
            status_code=409,
            detail=(
                "Este lançamento está vinculado a uma obrigação paga. "
                "Use 'Desfazer pagamento' na obrigação antes de excluir o lançamento"
            ),
        )
    _rollback_privilege_balance_for_deleted_transaction(
        db,
        user=user,
        transaction=transaction,
    )

    if transaction.document_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Lançamentos importados não são apagados; use Ignorar para preservar a auditoria",
        )
    if transaction.linked_transaction_id is not None and transaction.transfer_group_id is None:
        # A card-payment-reconciliation leg (`POST
        # /card-payment-reconciliations/pay`, `.../link`) keeps a symmetric
        # `linked_transaction_id` on both sides, same as a structured
        # transfer's two legs -- but a transfer also carries
        # `transfer_group_id`, already handled atomically below, so this
        # guard only needs to cover the reconciliation case. Its counterpart
        # usually has its own `document_id` guard above, but a leg created
        # by `pay_card_invoice` never does (it is manual, not imported) --
        # so without this check it could be deleted here directly, leaving
        # its still-imported counterpart pointing at a transaction id that
        # no longer exists. Unlink first (`POST
        # /card-payment-reconciliations/unlink`), which clears both sides
        # symmetrically; only then does this row become deletable.
        raise HTTPException(
            status_code=409,
            detail=(
                "Este lançamento está vinculado a uma conciliação de pagamento de fatura; "
                "desvincule antes de excluir"
            ),
        )
    if transaction.transfer_group_id is not None:
        # A structured transfer (`POST /transfers`) is two atomically-linked
        # legs (INV-001); deleting only the requested id would orphan its
        # twin. Delete the whole group together, in this single commit, so
        # there is never a state with just one leg persisted -- the same
        # atomicity guarantee `create_internal_transfer` gives on creation.
        group = db.scalars(
            select(Transaction).where(
                Transaction.household_id == user.household_id,
                Transaction.transfer_group_id == transaction.transfer_group_id,
            )
        ).all()
        audit(
            db,
            user,
            "transfer.delete",
            "transaction",
            transaction.id,
            {
                "transfer_group_id": transaction.transfer_group_id,
                "transaction_ids": [item.id for item in group],
                "amounts": [str(item.amount) for item in group],
            },
            trace_id=transaction.transfer_group_id,
        )
        for item in group:
            db.delete(item)
        db.commit()
        return {"ok": True, "deleted_transaction_ids": [item.id for item in group]}

    audit(
        db,
        user,
        "transaction.delete_manual",
        "transaction",
        transaction.id,
        {"description": transaction.description, "amount": str(transaction.amount)},
    )
    # No `FinancialProfile.investment_balance` reversal here: creating an
    # investment/redemption ("Transferência patrimonial") transaction no
    # longer mutates that field (see the "investment"/"redemption" branches
    # of `create_manual_transaction` and `confirm_capture`), so undoing a
    # mutation that never happened would silently corrupt the value instead
    # of restoring it.
    db.delete(transaction)
    db.commit()
    return {"ok": True}


@router.get("/reviews")
def reviews(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(ReviewItem)
        .where(ReviewItem.household_id == user.household_id, ReviewItem.status == "open")
        .order_by(ReviewItem.created_at.desc())
        .limit(300)
    ).all()
    return [
        {
            "id": item.id,
            "reason": item.reason,
            "details": item.details,
            "transaction_id": item.transaction_id,
            "description": item.transaction.description if item.transaction else "Documento não processado",
            "amount": decimal_value(item.transaction.amount) if item.transaction else None,
            "date": item.transaction.booked_at if item.transaction else None,
            "category_id": item.transaction.category_id if item.transaction else None,
            "category": (
                item.transaction.category.name
                if item.transaction and item.transaction.category
                else "Revisar"
            ),
            "account": (_account_display(item.transaction.account) if item.transaction else ""),
            "excluded": item.transaction.excluded if item.transaction else None,
            "possible_duplicate": (item.transaction.possible_duplicate if item.transaction else False),
            "manual": item.transaction.document_id is None if item.transaction else False,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.post("/reviews/{review_id}/resolve")
def resolve_review(
    review_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    item = db.scalar(
        select(ReviewItem).where(ReviewItem.id == review_id, ReviewItem.household_id == user.household_id)
    )
    if not item:
        raise HTTPException(status_code=404, detail="Pendência não encontrada")
    item.status = "resolved"
    item.resolved_at = datetime.now(UTC)
    if item.transaction:
        item.transaction.reviewed = True
    audit(db, user, "review.resolve", "review_item", item.id)
    db.commit()
    return {"ok": True}


@router.get("/duplicate-groups")
def duplicate_groups(
    group_status: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    if group_status and group_status not in {"open", "resolved"}:
        raise HTTPException(status_code=422, detail="Status de grupo inválido")
    statement = select(DuplicateGroup).where(
        DuplicateGroup.household_id == user.household_id
    )
    if group_status:
        statement = statement.where(DuplicateGroup.status == group_status)
    groups = db.scalars(
        statement.order_by(DuplicateGroup.updated_at.desc()).limit(200)
    ).all()
    result = []
    for group in groups:
        members = db.scalars(
            select(DuplicateGroupMember)
            .where(DuplicateGroupMember.group_id == group.id)
            .order_by(DuplicateGroupMember.role, DuplicateGroupMember.added_at)
        ).all()
        result.append(serialize_duplicate_group(group, members))
    return result


@router.get("/duplicate-groups/{group_id}")
def duplicate_group_detail(
    group_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    group = db.scalar(
        select(DuplicateGroup).where(
            DuplicateGroup.id == group_id,
            DuplicateGroup.household_id == user.household_id,
        )
    )
    if not group:
        raise HTTPException(status_code=404, detail="Grupo de duplicidade não encontrado")
    members = db.scalars(
        select(DuplicateGroupMember)
        .where(DuplicateGroupMember.group_id == group.id)
        .order_by(DuplicateGroupMember.role, DuplicateGroupMember.added_at)
    ).all()
    return serialize_duplicate_group(group, members)


@router.post("/duplicate-groups/{group_id}/resolve")
def resolve_persisted_duplicate_group(
    group_id: str,
    payload: DuplicateResolutionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        group = resolve_duplicate_group(
            db,
            group_id=group_id,
            household_id=user.household_id,
            resolution=payload.resolution,
            canonical_transaction_id=payload.canonical_transaction_id,
            user_id=user.id,
            reason=payload.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Grupo de duplicidade não encontrado") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        db,
        user,
        "duplicate_group.resolve",
        "duplicate_group",
        group.id,
        {"resolution": group.resolution},
        after_state={
            "status": group.status,
            "resolution": group.resolution,
            "canonical_transaction_id": group.canonical_transaction_id,
        },
        reason=payload.reason,
        source="duplicate_engine",
    )
    db.commit()
    return duplicate_group_detail(group.id, user, db)


@router.get("/card-payment-reconciliations")
def card_payment_reconciliations(
    period: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    matches = list_card_payment_reconciliations(db, household_id=user.household_id, period=period)
    return [serialize_card_payment_match(db, match) for match in matches]


@router.post("/card-payment-reconciliations/link", status_code=status.HTTP_201_CREATED)
def link_card_payment_reconciliation(
    payload: CardPaymentLinkRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        checking, card = link_card_payment(
            db,
            household_id=user.household_id,
            checking_transaction_id=payload.checking_transaction_id,
            card_transaction_id=payload.card_transaction_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado") from exc
    except CardPaymentLinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(
        db,
        user,
        "card_payment_reconciliation.link",
        "transaction",
        checking.id,
        {"card_transaction_id": card.id},
        before_state={"linked_transaction_id": None},
        after_state={
            "checking_transaction_id": checking.id,
            "card_transaction_id": card.id,
            "checking_amount": str(checking.amount),
            "card_amount": str(card.amount),
        },
        reason=payload.reason,
        source="card_payment_reconciliation",
    )
    db.commit()
    match = next(
        match
        for match in list_card_payment_reconciliations(db, household_id=user.household_id)
        if match.checking_transaction_id == checking.id
    )
    return serialize_card_payment_match(db, match)


@router.post("/card-payment-reconciliations/unlink")
def unlink_card_payment_reconciliation(
    payload: CardPaymentUnlinkRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        transaction, counterpart = unlink_card_payment(
            db, household_id=user.household_id, transaction_id=payload.transaction_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado") from exc
    except CardPaymentLinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    is_checking = transaction.account and transaction.account.account_type == "checking"
    checking = transaction if is_checking else counterpart
    card = counterpart if is_checking else transaction
    audit(
        db,
        user,
        "card_payment_reconciliation.unlink",
        "transaction",
        checking.id,
        {"card_transaction_id": card.id},
        before_state={"checking_transaction_id": checking.id, "card_transaction_id": card.id},
        after_state={"linked_transaction_id": None},
        reason=payload.reason,
        source="card_payment_reconciliation",
    )
    db.commit()
    match = next(
        match
        for match in list_card_payment_reconciliations(db, household_id=user.household_id)
        if match.checking_transaction_id == checking.id
    )
    return serialize_card_payment_match(db, match)


@router.get("/card-payment-reconciliations/invoices")
def card_invoice_obligations(
    period: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """"Contas a pagar" -- card-invoice side.

    `docs/WORK_ORDER_MANUAL_PAYABLES_CARD_PAYMENT.md`: every card invoice
    the household has already imported, with `paid`/`pending` derived only
    from `Transaction.linked_transaction_id` -- never a fabricated due-date
    bucket (see `list_card_invoice_obligations`'s docstring).
    """
    items = list_card_invoice_obligations(db, household_id=user.household_id, period=period)
    return [serialize_card_invoice_obligation(item) for item in items]


@router.post("/card-payment-reconciliations/pay", status_code=status.HTTP_201_CREATED)
def pay_card_invoice_reconciliation(
    payload: CardInvoicePaymentRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Manual payment of a card invoice -- go-live manual slice 3.

    Creates the checking-side reconciliation leg and links it to the
    invoice's existing payment-received line in one call
    (`pay_card_invoice`). INV-002 holds because the new leg is always
    `transaction_type = "reconciliation"`, category "Conciliação" and
    `excluded = True` -- exactly like every other reconciliation row, so it
    can never be counted as a new expense; the invoice's own purchases
    remain the only economic expense facts.
    """
    _require_admin(user)
    paying_account = db.scalar(
        select(Account).where(
            Account.id == payload.paying_account_id,
            Account.household_id == user.household_id,
            Account.active.is_(True),
        )
    )
    if not paying_account:
        raise HTTPException(status_code=404, detail="Conta pagadora não encontrada")

    profile = profile_for(db, user.household_id)
    large_threshold = _large_entry_threshold(profile)
    if payload.amount >= large_threshold and not payload.confirmed_large_amount:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Este valor exige confirmação adicional. Confirme apenas se o pagamento "
                f"aconteceu de verdade; use o Consultor para simulações. Limite de confirmação: "
                f"R$ {large_threshold:,.2f}."
            ),
        )

    category = category_for(db, user.household_id, "Conciliação")
    try:
        checking_leg, card, duplicate_assessment = pay_card_invoice(
            db,
            household_id=user.household_id,
            card_transaction_id=payload.card_transaction_id,
            paying_account=paying_account,
            category=category,
            amount=payload.amount,
            booked_at=payload.booked_at,
            description=payload.description,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Fatura não encontrada") from exc
    except CardPaymentLinkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    review_created = False
    if duplicate_assessment is not None:
        review_created = True
        db.add(
            ReviewItem(
                household_id=user.household_id,
                transaction_id=checking_leg.id,
                reason="possible_duplicate",
                details=(
                    "Pagamento manual de fatura agrupado como possível repetição "
                    f"({duplicate_assessment.band}, confiança {duplicate_assessment.confidence})"
                ),
            )
        )

    audit(
        db,
        user,
        "card_payment_reconciliation.pay",
        "transaction",
        checking_leg.id,
        {
            "card_transaction_id": card.id,
            "paying_account_id": paying_account.id,
            "amount": str(checking_leg.amount),
        },
        before_state={"card_transaction_id": card.id, "linked_transaction_id": None},
        after_state={"checking_transaction_id": checking_leg.id, "card_transaction_id": card.id},
        trace_id=checking_leg.trace_id,
        source="card_payment_reconciliation",
    )
    db.commit()
    return {
        "checking_transaction_id": checking_leg.id,
        "card_transaction_id": card.id,
        "review_items": 1 if review_created else 0,
    }


@router.get("/classification-rules")
def classification_rules(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.execute(
        select(ClassificationRule, Category.name)
        .join(Category, Category.id == ClassificationRule.category_id)
        .where(ClassificationRule.household_id == user.household_id)
        .order_by(
            ClassificationRule.active.desc(),
            ClassificationRule.confirmation_count.desc(),
            ClassificationRule.updated_at.desc(),
        )
    ).all()
    return [serialize_classification_rule(rule, category_name) for rule, category_name in rows]


@router.post("/classification-rules/{rule_id}/activate")
def activate_classification_rule(
    rule_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        rule = accept_classification_rule(
            db,
            household_id=user.household_id,
            rule_id=rule_id,
            user_id=user.id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Regra local não encontrada") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    category_name = db.scalar(select(Category.name).where(Category.id == rule.category_id))
    audit(
        db,
        user,
        "classification_rule.activate",
        "classification_rule",
        rule.id,
        {"confirmation_count": rule.confirmation_count},
        # `accept_classification_rule` only succeeds when the rule was
        # `pending_acceptance`/`active=False` (it raises `ValueError`
        # otherwise -- see its precondition), so this before-state is
        # deterministic, exactly like `deactivate`'s hardcoded before-state
        # below.
        before_state={"status": "pending_acceptance", "active": False},
        after_state={"status": rule.status, "active": rule.active},
        reason="Aceite explícito de regra após três correções consistentes",
        source="classification_learning",
    )
    db.commit()
    return serialize_classification_rule(rule, category_name or "")


@router.patch("/classification-rules/{rule_id}")
def edit_classification_rule_endpoint(
    rule_id: str,
    payload: ClassificationRuleEditRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    before = db.execute(
        select(
            ClassificationRule.category_id,
            Category.name,
            ClassificationRule.status,
            ClassificationRule.active,
            ClassificationRule.confirmation_count,
        )
        .join(Category, Category.id == ClassificationRule.category_id)
        .where(
            ClassificationRule.id == rule_id,
            ClassificationRule.household_id == user.household_id,
        )
    ).first()
    if before is None:
        raise HTTPException(status_code=404, detail="Regra local não encontrada")
    (
        before_category_id,
        before_category_name,
        before_status,
        before_active,
        before_confirmation_count,
    ) = before
    try:
        rule = edit_classification_rule(
            db,
            household_id=user.household_id,
            rule_id=rule_id,
            category_id=payload.category_id,
        )
        db.flush()
    except LookupError as exc:
        # The rule itself was already confirmed to exist above (`before`);
        # a `LookupError` at this point can only be the requested category.
        raise HTTPException(status_code=404, detail="Categoria não encontrada") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Já existe uma regra para este estabelecimento com esta categoria e tipo de movimento",
        ) from exc
    category_name = db.scalar(select(Category.name).where(Category.id == rule.category_id))
    audit(
        db,
        user,
        "classification_rule.edit",
        "classification_rule",
        rule.id,
        {},
        # A category-changing edit is also a governance/lifecycle reset
        # (`edit_classification_rule` drops confirmation_count/status/active
        # back to the observed/zero state -- see its docstring), so the
        # audit trail must capture that transition, not just the category
        # change, per the Work Order's before/after/reason/actor
        # requirement. This does not dump the unbounded `evidence` payload
        # into AuditEvent -- only the material lifecycle fields.
        before_state={
            "category_id": before_category_id,
            "category": before_category_name,
            "status": before_status,
            "active": before_active,
            "confirmation_count": before_confirmation_count,
        },
        after_state={
            "category_id": rule.category_id,
            "category": category_name,
            "status": rule.status,
            "active": rule.active,
            "confirmation_count": rule.confirmation_count,
        },
        reason=payload.reason,
        source="classification_learning",
    )
    db.commit()
    return serialize_classification_rule(rule, category_name or "")


@router.post("/classification-rules/{rule_id}/deactivate")
def deactivate_classification_rule_endpoint(
    rule_id: str,
    payload: ClassificationRuleDeactivateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    try:
        rule = deactivate_classification_rule(
            db,
            household_id=user.household_id,
            rule_id=rule_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Regra local não encontrada") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    category_name = db.scalar(select(Category.name).where(Category.id == rule.category_id))
    audit(
        db,
        user,
        "classification_rule.deactivate",
        "classification_rule",
        rule.id,
        {},
        before_state={"status": "active", "active": True},
        after_state={"status": rule.status, "active": rule.active},
        reason=payload.reason,
        source="classification_learning",
    )
    db.commit()
    return serialize_classification_rule(rule, category_name or "")


@router.get("/commissions")
def commissions(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Commission)
        .where(Commission.household_id == user.household_id)
        .order_by(Commission.expected_date)
    ).all()
    result = []
    for item in rows:
        tax, net = commission_net(item.gross_amount, item.tax_rate)
        result.append(
            {
                "id": item.id,
                "description": item.description,
                "expected_date": item.expected_date,
                "gross": decimal_value(item.gross_amount),
                "tax_rate": decimal_value(item.tax_rate),
                "tax": decimal_value(tax),
                "net": decimal_value(net),
                "delay_days": item.delay_days,
                "status": item.status,
            }
        )
    return result


@router.post("/commissions", status_code=201)
def create_commission(
    payload: CommissionRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    item = Commission(household_id=user.household_id, **payload.model_dump())
    db.add(item)
    db.flush()
    tax, net = commission_net(item.gross_amount, item.tax_rate)
    audit(db, user, "commission.create", "commission", item.id, payload.model_dump())
    db.commit()
    return {"id": item.id, "tax": decimal_value(tax), "net": decimal_value(net)}


@router.delete("/commissions/{commission_id}")
def delete_commission(
    commission_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = db.scalar(
        select(Commission).where(
            Commission.id == commission_id,
            Commission.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Comissão não encontrada")
    audit(db, user, "commission.delete", "commission", item.id, {"description": item.description})
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/payroll")
def payroll(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(PayrollRecord)
        .where(PayrollRecord.household_id == user.household_id)
        .order_by(PayrollRecord.payment_date.desc())
    ).all()
    return [
        {
            "id": item.id,
            "person_name": item.person_name,
            "competence": item.competence,
            "payment_date": item.payment_date,
            "kind": item.payroll_kind,
            "gross": decimal_value(item.gross_amount),
            "deductions": decimal_value(item.deductions),
            "net": decimal_value(item.net_amount),
            "payroll_loan": decimal_value(item.payroll_loan),
            "manual": item.document_id is None,
        }
        for item in rows
    ]


@router.post("/payroll", status_code=201)
def create_payroll(
    payload: PayrollRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    item = PayrollRecord(household_id=user.household_id, **payload.model_dump())
    db.add(item)
    db.flush()
    audit(db, user, "payroll.create", "payroll", item.id, payload.model_dump())
    db.commit()
    return {"id": item.id}


@router.delete("/payroll/{payroll_id}")
def delete_manual_payroll(
    payroll_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = db.scalar(
        select(PayrollRecord).where(
            PayrollRecord.id == payroll_id,
            PayrollRecord.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Registro de folha não encontrado")
    if item.document_id is not None:
        raise HTTPException(status_code=409, detail="Holerites importados devem ser preservados")
    audit(db, user, "payroll.delete_manual", "payroll", item.id, {"person_name": item.person_name})
    db.delete(item)
    db.commit()
    return {"ok": True}


# FAMILY_FINANCE_OBLIGATION_PAYMENT_V8
def _obligation_payment_category(
    db: Session, household_id: str, obligation: Obligation
) -> Category:
    if obligation.category in {"asset_acquisition", "chacara"}:
        name = "Aquisição da chácara"
        color = "#15803D"
        essential = True
    elif obligation.category == "loan":
        name = "Obrigações financeiras"
        color = "#B7791F"
        essential = True
    else:
        name = "Compromissos"
        color = "#64748B"
        essential = False

    category = db.scalar(
        select(Category).where(
            Category.household_id == household_id,
            func.lower(Category.name) == name.lower(),
        )
    )
    if category:
        return category
    category = Category(
        household_id=household_id,
        name=name,
        color=color,
        cash_cap=Decimal("0"),
        essential=essential,
    )
    db.add(category)
    db.flush()
    return category


def _obligation_payment_candidate_rows(
    db: Session, *, household_id: str, obligation: Obligation
) -> list[dict]:
    start = obligation.due_date - timedelta(days=31)
    end = obligation.due_date + timedelta(days=31)

    already_linked = set(
        db.scalars(
            select(Obligation.paid_transaction_id).where(
                Obligation.household_id == household_id,
                Obligation.paid_transaction_id.is_not(None),
                Obligation.id != obligation.id,
            )
        ).all()
    )

    rows = db.execute(
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Account.household_id == household_id,
            Account.active.is_(True),
            Account.account_type.in_(("checking", "cash")),
            Transaction.booked_at >= start,
            Transaction.booked_at <= end,
            Transaction.amount == -money(obligation.amount),
            Transaction.transaction_type == "expense",
            Transaction.excluded.is_(False),
            Transaction.possible_duplicate.is_(False),
            Transaction.canonical_status != "supporting",
        )
    ).all()

    candidates = []
    for transaction, account in rows:
        if transaction.id in already_linked:
            continue
        candidates.append(
            {
                "transaction_id": transaction.id,
                "date": transaction.booked_at,
                "description": transaction.description,
                "amount": decimal_value(abs(transaction.amount)),
                "account_id": account.id,
                "account": _account_display(account),
                "imported": transaction.document_id is not None,
                "distance_days": abs((transaction.booked_at - obligation.due_date).days),
            }
        )
    candidates.sort(key=lambda row: (row["distance_days"], row["date"], row["transaction_id"]))
    return candidates[:20]


def _get_payable_obligation(
    db: Session, *, household_id: str, obligation_id: str
) -> Obligation:
    item = db.scalar(
        select(Obligation).where(
            Obligation.id == obligation_id,
            Obligation.household_id == household_id,
            Obligation.active.is_(True),
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Obrigação não encontrada")
    return item


@router.get("/obligations/{obligation_id}/payment-candidates")
def obligation_payment_candidates(
    obligation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    item = _get_payable_obligation(
        db, household_id=user.household_id, obligation_id=obligation_id
    )
    if item.status == "paid":
        return []
    return _obligation_payment_candidate_rows(
        db, household_id=user.household_id, obligation=item
    )


def _privilege_account(db: Session, household_id: str, *, required: bool = True) -> Account | None:
    """Resolve the household's single Privilège DI investment account.

    A household that has not created any `investment` account yet is not an
    ambiguity -- it is the ordinary state of a fresh household, before it has
    ever used the Privilège-funding feature (`FAMILY_FINANCE_PRIVILEGE_FUNDING_V15`).
    Callers that need a real account to fund/redeem against keep `required=True`
    (the default) and get the existing fail-closed 422 either way -- zero
    candidates or more than one with no unique "PRIVILEGE" match, never a
    guess. Callers that only *observe* the account when one exists (profile
    read/write) pass `required=False` and get `None` back for the "not
    configured yet" case, while an unresolved ambiguity among *existing*
    investment accounts still raises: guessing which one is Privilège would
    be worse than surfacing the conflict.
    """
    investments = list(
        db.scalars(
            select(Account).where(
                Account.household_id == household_id,
                Account.active.is_(True),
                Account.account_type == "investment",
            )
        ).all()
    )
    matches = [
        account for account in investments
        if "PRIVILEGE" in normalize_description(f"{account.institution} {account.name}")
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(investments) == 1:
        return investments[0]
    if not investments and not required:
        return None
    raise HTTPException(
        status_code=422,
        detail="Não foi possível identificar unicamente a conta Privilège DI.",
    )


def _latest_active_balance_observation(
    db: Session, *, household_id: str, account_id: str
) -> AccountBalanceObservation | None:
    return db.scalar(
        select(AccountBalanceObservation)
        .where(
            AccountBalanceObservation.household_id == household_id,
            AccountBalanceObservation.account_id == account_id,
            AccountBalanceObservation.invalidated_at.is_(None),
            AccountBalanceObservation.superseded_by_id.is_(None),
        )
        .order_by(
            AccountBalanceObservation.as_of_date.desc(),
            AccountBalanceObservation.created_at.desc(),
        )
        .limit(1)
    )


def _fund_obligation_from_privilege(
    db: Session,
    *,
    user: User,
    obligation: Obligation,
    paid_at: date,
) -> Decimal:
    if (
        obligation.funding_source == "privilege"
        and obligation.funding_balance_observation_id
    ):
        privilege = _privilege_account(db, user.household_id)
        current = _latest_active_balance_observation(
            db, household_id=user.household_id, account_id=privilege.id
        )
        return money(current.amount) if current else Decimal("0")

    privilege = _privilege_account(db, user.household_id)
    current = _latest_active_balance_observation(
        db, household_id=user.household_id, account_id=privilege.id
    )
    if current is None:
        raise HTTPException(status_code=409, detail="Não existe saldo confirmado ativo para o Privilège DI.")
    if current.as_of_date > paid_at:
        raise HTTPException(
            status_code=409,
            detail=(
                "Existe um saldo do Privilège confirmado depois da data deste pagamento. "
                "Não vou descontar retroativamente para evitar dupla contagem."
            ),
        )

    amount = money(obligation.amount)
    current_amount = money(current.amount)
    if current_amount < amount:
        raise HTTPException(status_code=409, detail="Saldo confirmado do Privilège é menor que o valor a resgatar.")
    new_amount = money(current_amount - amount)
    trace_id = str(uuid.uuid4())

    category = category_for(db, user.household_id, "Transferência patrimonial")
    description = f"Resgate Privilège DI para {obligation.name}"
    parsed = ParsedTransaction(
        booked_at=paid_at,
        description=description,
        amount=amount,
        source_line=1,
        card_last_four=privilege.last_four,
        occurred_at=paid_at,
    )
    funding_tx = Transaction(
        household_id=user.household_id,
        account_id=privilege.id,
        category_id=category.id,
        booked_at=paid_at,
        description=description,
        normalized_description=normalize_description(description),
        amount=amount,
        transaction_type="transfer",
        owner_label=privilege.owner_label,
        card_last_four=privilege.last_four,
        fingerprint=transaction_fingerprint(privilege.id, parsed, privilege.owner_label),
        source_line=1,
        occurred_at=paid_at,
        competence=paid_at.strftime("%Y-%m"),
        classification_source="manual_confirmed",
        classification_version=PARSER_CONTRACT_VERSION,
        canonical_status="unassigned",
        trace_id=trace_id,
        source_priority=source_priority("manual"),
        confidence=Decimal("1"),
        excluded=True,
        possible_duplicate=False,
        reviewed=True,
    )
    db.add(funding_tx)
    db.flush()

    balance = AccountBalanceObservation(
        household_id=user.household_id,
        account_id=privilege.id,
        amount=new_amount,
        as_of_date=paid_at,
        observation_type="point_in_time",
        source="manual_confirmed",
        document_id=None,
        confirmed_by=user.id,
        confidence=Decimal("1.0000"),
        supersedes_id=current.id,
        trace_id=trace_id,
    )
    db.add(balance)
    db.flush()
    current.superseded_by_id = balance.id

    obligation.funding_source = "privilege"
    obligation.funding_transaction_id = funding_tx.id
    obligation.funding_balance_observation_id = balance.id

    audit(
        db,
        user,
        "obligation.funding.privilege",
        "obligation",
        obligation.id,
        {
            "amount": str(amount),
            "privilege_account_id": privilege.id,
            "funding_transaction_id": funding_tx.id,
            "balance_observation_id": balance.id,
            "balance_before": str(current_amount),
            "balance_after": str(new_amount),
        },
        reason="Usuário confirmou que o pagamento foi financiado por resgate do Privilège DI.",
        trace_id=trace_id,
        source="obligation_payment",
    )
    return new_amount


def _undo_obligation_privilege_funding(
    db: Session, *, user: User, obligation: Obligation
) -> None:
    if obligation.funding_source != "privilege":
        return

    balance = None
    if obligation.funding_balance_observation_id:
        balance = db.scalar(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.id == obligation.funding_balance_observation_id,
                AccountBalanceObservation.household_id == user.household_id,
            )
        )
    if balance is not None:
        if balance.invalidated_at is not None:
            raise HTTPException(status_code=409, detail="A observação de saldo deste resgate já foi invalidada.")
        if balance.superseded_by_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Há um saldo do Privilège confirmado depois deste pagamento; não é seguro desfazer automaticamente.",
            )
        previous = None
        if balance.supersedes_id:
            previous = db.scalar(
                select(AccountBalanceObservation).where(
                    AccountBalanceObservation.id == balance.supersedes_id,
                    AccountBalanceObservation.household_id == user.household_id,
                )
            )
        if previous is not None:
            previous.superseded_by_id = None
        balance.invalidated_at = datetime.now(UTC)
        balance.invalidated_by = user.id

    if obligation.funding_transaction_id:
        funding_tx = db.scalar(
            select(Transaction).where(
                Transaction.id == obligation.funding_transaction_id,
                Transaction.household_id == user.household_id,
            )
        )
        if funding_tx is not None:
            if funding_tx.document_id is not None:
                raise HTTPException(status_code=409, detail="O resgate vinculado veio de importação e não pode ser apagado automaticamente.")
            db.delete(funding_tx)

    obligation.funding_source = "account"
    obligation.funding_transaction_id = None
    obligation.funding_balance_observation_id = None


@router.post("/obligations/{obligation_id}/pay")
def pay_obligation(
    obligation_id: str,
    payload: ObligationPaymentRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = _get_payable_obligation(
        db, household_id=user.household_id, obligation_id=obligation_id
    )
    if item.status == "paid":
        raise HTTPException(status_code=409, detail="Esta obrigação já está paga")
    if item.recurrence_months != 0 or item.occurrence_count != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Marcar como paga está disponível para obrigações de ocorrência única. "
                "Cadastre cada parcela como uma obrigação individual para liquidá-la separadamente"
            ),
        )

    transaction: Transaction | None = None
    created_transaction = False

    if payload.transaction_id:
        transaction = db.scalar(
            select(Transaction).where(
                Transaction.id == payload.transaction_id,
                Transaction.household_id == user.household_id,
            )
        )
        if not transaction:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado")
        account = db.scalar(
            select(Account).where(
                Account.id == transaction.account_id,
                Account.household_id == user.household_id,
                Account.active.is_(True),
            )
        )
        if not account or account.account_type not in {"checking", "cash"}:
            raise HTTPException(
                status_code=422,
                detail="O pagamento precisa sair de uma conta corrente ou caixa",
            )
        if (
            transaction.transaction_type != "expense"
            or transaction.excluded
            or transaction.possible_duplicate
            or transaction.canonical_status == "supporting"
            or transaction.amount >= 0
            or money(abs(transaction.amount)) != money(item.amount)
        ):
            raise HTTPException(
                status_code=422,
                detail="O lançamento selecionado não é uma saída válida com o mesmo valor da obrigação",
            )
        used_by = db.scalar(
            select(Obligation).where(
                Obligation.household_id == user.household_id,
                Obligation.paid_transaction_id == transaction.id,
                Obligation.id != item.id,
            )
        )
        if used_by:
            raise HTTPException(
                status_code=409,
                detail="Este lançamento já está vinculado ao pagamento de outra obrigação",
            )
        paid_at = transaction.booked_at
    else:
        account = db.scalar(
            select(Account).where(
                Account.id == payload.account_id,
                Account.household_id == user.household_id,
                Account.active.is_(True),
            )
        )
        if not account:
            raise HTTPException(status_code=404, detail="Conta não encontrada")
        if account.account_type not in {"checking", "cash"}:
            raise HTTPException(
                status_code=422,
                detail="O pagamento precisa sair de uma conta corrente ou caixa",
            )
        assert payload.paid_at is not None
        paid_at = payload.paid_at

        profile = profile_for(db, user.household_id)
        large_threshold = _large_entry_threshold(profile)
        if item.amount >= large_threshold and not payload.confirmed_large_amount:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Este pagamento exige confirmação adicional por ser de valor elevado. "
                    "Confirme novamente que o pagamento realmente aconteceu"
                ),
            )

        nearby = _obligation_payment_candidate_rows(
            db, household_id=user.household_id, obligation=item
        )
        collision = next(
            (
                candidate
                for candidate in nearby
                if candidate["account_id"] == account.id
                and abs((candidate["date"] - paid_at).days) <= 7
            ),
            None,
        )
        if collision:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Já existe uma saída bancária compatível nesta conta e próxima desta data. "
                    "Escolha esse lançamento existente para evitar duplicidade"
                ),
            )

        category = _obligation_payment_category(db, user.household_id, item)
        description = " ".join(
            (payload.description or f"Pagamento {item.name}").split()
        )
        parsed = ParsedTransaction(
            booked_at=paid_at,
            description=description,
            amount=-money(item.amount),
            source_line=1,
            card_last_four=account.last_four,
            occurred_at=paid_at,
        )
        fingerprint = transaction_fingerprint(account.id, parsed, account.owner_label)

        same_fingerprint = db.scalar(
            select(Transaction).where(
                Transaction.household_id == user.household_id,
                Transaction.fingerprint == fingerprint,
            )
        )
        if same_fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Já existe um lançamento manual idêntico. "
                    "Use a opção de vincular lançamento existente"
                ),
            )

        transaction = Transaction(
            household_id=user.household_id,
            account_id=account.id,
            category_id=category.id,
            booked_at=paid_at,
            description=description,
            normalized_description=normalize_description(description),
            amount=-money(item.amount),
            transaction_type="expense",
            owner_label=account.owner_label,
            card_last_four=account.last_four,
            fingerprint=fingerprint,
            source_line=1,
            occurred_at=paid_at,
            competence=paid_at.strftime("%Y-%m"),
            classification_source="manual_confirmed",
            classification_version=PARSER_CONTRACT_VERSION,
            canonical_status="unassigned",
            trace_id=str(uuid.uuid4()),
            source_priority=source_priority("manual"),
            confidence=Decimal("1"),
            excluded=False,
            possible_duplicate=False,
            reviewed=True,
        )
        db.add(transaction)
        db.flush()
        created_transaction = True

    assert transaction is not None
    before_state = {
        "status": item.status,
        "paid_at": item.paid_at.isoformat() if item.paid_at else None,
        "paid_transaction_id": item.paid_transaction_id,
    }
    item.status = "paid"
    item.paid_at = paid_at
    item.paid_transaction_id = transaction.id
    item.payment_transaction_created = created_transaction

    funding_balance = None
    if payload.funding_source == "privilege":
        funding_balance = _fund_obligation_from_privilege(
            db,
            user=user,
            obligation=item,
            paid_at=paid_at,
        )
    else:
        item.funding_source = "account"
        item.funding_transaction_id = None
        item.funding_balance_observation_id = None

    audit(
        db,
        user,
        "obligation.pay",
        "obligation",
        item.id,
        {
            "transaction_id": transaction.id,
            "transaction_created": created_transaction,
            "amount": str(money(item.amount)),
            "paid_at": paid_at.isoformat(),
        },
        before_state=before_state,
        after_state={
            "status": item.status,
            "paid_at": paid_at.isoformat(),
            "paid_transaction_id": transaction.id,
            "payment_transaction_created": created_transaction,
        },
        reason="Pagamento confirmado pelo usuário",
        trace_id=transaction.trace_id,
        source="obligation_payment",
    )
    db.commit()
    return {
        "ok": True,
        "obligation_id": item.id,
        "status": item.status,
        "paid_at": item.paid_at,
        "transaction_id": transaction.id,
        "transaction_created": created_transaction,
        "funding_source": item.funding_source,
        "funding_balance": decimal_value(funding_balance) if funding_balance is not None else None,
    }


@router.post("/obligations/{obligation_id}/unpay")
def unpay_obligation(
    obligation_id: str,
    payload: ObligationPaymentUndoRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = _get_payable_obligation(
        db, household_id=user.household_id, obligation_id=obligation_id
    )
    if item.status != "paid":
        raise HTTPException(status_code=409, detail="Esta obrigação não está marcada como paga")

    transaction = None
    if item.paid_transaction_id:
        transaction = db.scalar(
            select(Transaction).where(
                Transaction.id == item.paid_transaction_id,
                Transaction.household_id == user.household_id,
            )
        )

    before_state = {
        "status": item.status,
        "paid_at": item.paid_at.isoformat() if item.paid_at else None,
        "paid_transaction_id": item.paid_transaction_id,
        "payment_transaction_created": item.payment_transaction_created,
    }
    transaction_id = item.paid_transaction_id
    created_transaction = item.payment_transaction_created
    funding_undone = item.funding_source == "privilege"
    _undo_obligation_privilege_funding(db, user=user, obligation=item)

    if created_transaction and transaction is not None:
        if transaction.document_id is not None:
            raise HTTPException(
                status_code=409,
                detail="O lançamento vinculado é importado e não pode ser apagado automaticamente",
            )
        item.paid_transaction_id = None
        item.payment_transaction_created = False
        db.flush()
        db.delete(transaction)
    else:
        item.paid_transaction_id = None
        item.payment_transaction_created = False

    item.status = "pending"
    item.paid_at = None

    audit(
        db,
        user,
        "obligation.unpay",
        "obligation",
        item.id,
        {
            "transaction_id": transaction_id,
            "transaction_deleted": bool(created_transaction and transaction is not None),
        },
        before_state=before_state,
        after_state={
            "status": "pending",
            "paid_at": None,
            "paid_transaction_id": None,
            "payment_transaction_created": False,
        },
        reason=payload.reason,
        source="obligation_payment",
    )
    db.commit()
    return {
        "ok": True,
        "obligation_id": item.id,
        "status": item.status,
        "transaction_deleted": bool(created_transaction and transaction is not None),
        "funding_undone": funding_undone,
    }


@router.get("/obligations")
def obligations(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return _obligation_rows(db, user.household_id, include_paid=True)


@router.post("/obligations", status_code=201)
def create_obligation(
    payload: ObligationRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    item = Obligation(household_id=user.household_id, **payload.model_dump())
    db.add(item)
    db.flush()
    audit(db, user, "obligation.create", "obligation", item.id, payload.model_dump())
    db.commit()
    return {"id": item.id}


@router.delete("/obligations/{obligation_id}")
def delete_obligation(
    obligation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    item = db.scalar(
        select(Obligation).where(
            Obligation.id == obligation_id,
            Obligation.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Compromisso não encontrado")
    if item.status == "paid":
        raise HTTPException(
            status_code=409,
            detail="Desfaça o pagamento antes de excluir uma obrigação paga",
        )
    item.active = False
    item.status = "cancelled"
    audit(
        db,
        user,
        "obligation.deactivate",
        "obligation",
        item.id,
        {"name": item.name},
        after_state={"active": False, "status": "cancelled"},
    )
    db.commit()
    return {"ok": True}


@router.get("/profile")
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    item = profile_for(db, user.household_id)
    # FAMILY_FINANCE_PROFILE_CONFIRMED_BALANCE_SYNC_V17
    current_confirmed_balance = item.investment_balance
    try:
        privilege_account = _privilege_account(db, user.household_id, required=False)
        if privilege_account is not None:
            current_observation = _latest_active_balance_observation(
                db,
                household_id=user.household_id,
                account_id=privilege_account.id,
            )
            if current_observation is not None:
                current_confirmed_balance = money(current_observation.amount)
    except HTTPException:
        # Ambiguous Privilège account among *existing* investment accounts:
        # fall back to the legacy scalar rather than guessing which one to trust.
        pass
    db.commit()
    return {
        "monthly_salary_net": decimal_value(item.monthly_salary_net),
        "monthly_cash_cap": decimal_value(item.monthly_cash_cap),
        "emergency_floor": decimal_value(item.emergency_floor),
        "food_allowance": decimal_value(item.food_allowance),
        "meal_allowance_daily": decimal_value(item.meal_allowance_daily),
        "workdays_month": item.workdays_month,
        "investment_name": item.investment_name,
        "investment_balance": decimal_value(current_confirmed_balance),
        "investment_gross_annual_rate": decimal_value(item.investment_gross_annual_rate),
        "investment_income_tax_rate": decimal_value(item.investment_income_tax_rate),
        "projection_end": item.projection_end,
    }


@router.put("/profile")
def update_profile(
    payload: ProfileRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    _require_admin(user)
    item = profile_for(db, user.household_id)
    values = payload.model_dump()
    desired_confirmed_balance = money(payload.investment_balance)

    # `required=False`: a household that has not created any investment
    # account yet has nothing to sync an observation against -- that is the
    # ordinary state before the Privilège-funding feature is first used, not
    # an ambiguity, and must not block the rest of the profile (salary, cash
    # cap, etc.) from being saved. An unresolved ambiguity among *existing*
    # investment accounts still raises 422 from `_privilege_account` itself.
    privilege_account = _privilege_account(db, user.household_id, required=False)
    current_observation = (
        _latest_active_balance_observation(
            db,
            household_id=user.household_id,
            account_id=privilege_account.id,
        )
        if privilege_account is not None
        else None
    )

    today = date.today()
    if current_observation is not None and current_observation.as_of_date > today:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Existe saldo confirmado do Privilège em data futura "
                f"({current_observation.as_of_date.isoformat()}). "
                "Corrija essa data antes de salvar um novo saldo."
            ),
        )

    for field, value in values.items():
        setattr(item, field, value)

    # No Privilège account resolved yet: there is no observation trail to
    # reconcile against, so this is not a "confirmed balance changed" event --
    # `item.investment_balance` below still stores the plain scalar the
    # household declared, exactly as it did before this feature existed.
    balance_changed = privilege_account is not None and (
        current_observation is None
        or money(current_observation.amount) != desired_confirmed_balance
    )

    new_observation = None
    if balance_changed:
        trace_id = str(uuid.uuid4())
        new_observation = AccountBalanceObservation(
            household_id=user.household_id,
            account_id=privilege_account.id,
            amount=desired_confirmed_balance,
            as_of_date=today,
            observation_type="point_in_time",
            source="manual_confirmed",
            document_id=None,
            confirmed_by=user.id,
            confidence=Decimal("1.0000"),
            supersedes_id=current_observation.id if current_observation else None,
            trace_id=trace_id,
        )
        db.add(new_observation)
        db.flush()

        if current_observation is not None:
            current_observation.superseded_by_id = new_observation.id

        audit(
            db,
            user,
            "profile.investment_balance.confirm",
            "account_balance_observation",
            new_observation.id,
            {
                "account_id": privilege_account.id,
                "previous_balance": (
                    str(money(current_observation.amount))
                    if current_observation is not None
                    else None
                ),
                "confirmed_balance": str(desired_confirmed_balance),
                "as_of_date": today.isoformat(),
            },
            reason="Saldo atual confirmado informado manualmente em Configurações.",
            trace_id=trace_id,
            source="profile_settings",
        )

    item.investment_balance = desired_confirmed_balance

    audit(
        db,
        user,
        "profile.update",
        "financial_profile",
        item.id,
        {
            **values,
            "investment_balance": str(desired_confirmed_balance),
            "confirmed_balance_changed": balance_changed,
            "confirmed_balance_observation_id": (
                new_observation.id if new_observation is not None else None
            ),
        },
    )
    db.commit()
    return {
        "ok": True,
        "investment_balance": decimal_value(desired_confirmed_balance),
        "confirmed_balance_changed": balance_changed,
        "confirmed_balance_date": today.isoformat(),
    }


def _forecast_obligations(items: list[Obligation]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in items:
        # FAMILY_FINANCE_OBLIGATION_PAYMENT_V8
        # Pago deixa de ser previsto. O débito real permanece no ledger.
        if not item.active or item.status != "pending":
            continue
        for occurrence in range(item.occurrence_count):
            due = add_months(item.due_date.replace(day=1), occurrence * item.recurrence_months)
            key = month_key(due)
            values[key] = values.get(key, Decimal("0")) + item.amount
            if item.recurrence_months == 0:
                break
    return values


# FAMILY_FINANCE_CARD_CYCLES_PRIVILEGE_V13
#
# Hotfix (docs/WORK_ORDER_CARD_OPEN_INVOICE_COMPETENCE_HOTFIX.md, PR #81):
# this used to be two functions of the same name -- a dead
# `_credit_card_closing_day`/`_card_invoice_competence(*, booked_at,
# closing_day)` pair that hardcoded Itaú/Nubank/Mercado Pago closing days by
# bank name, silently shadowed by this real implementation (the one every
# caller actually resolved to, since Python keeps only the last definition
# of a name).
#
# P0 (docs/WORK_ORDER_CARD_COMPETENCE_REPAIR_P0.md, "Arquitetura
# obrigatória" item 1): the formula itself now lives in
# `app.services.card_competence`, the single canonical implementation every
# consumer -- manual entry, Smart Capture confirmation, installment
# preview/projection, and the historical repair detector/repairer
# (`app.services.card_competence_repair`) -- must call. These two names stay
# bound here, unchanged, purely so every existing call site in this module
# and the public test surface (`tests/test_card_open_invoice_competence.py`
# imports `from app.api import _card_invoice_competence,
# _resolve_expense_competence`) keep working without a second copy of either
# function ever existing.
_card_invoice_competence = card_invoice_competence
_resolve_expense_competence = resolve_expense_competence



def _installment_anchor_month(*, competence: str | None, booked_at: date) -> date:
    """First-of-month this installment fact is anchored to for
    schedule/projection purposes.

    INV-017: a card purchase's competence is the invoice's canonical
    competence, not necessarily `booked_at`'s month (see
    `create_manual_transaction`). Once a `competence` has been explicitly
    confirmed, it -- not `booked_at` -- is the source of truth for "which
    month does this observed installment belong to"; `booked_at` keeps being
    recorded as lineage but must never be (mis)used as a stand-in for
    competence here. Falls back to `booked_at`'s month only when there is no
    confirmed competence (legacy rows, or non-card accounts where the two
    already coincide by the `create_manual_transaction` fallback).
    """
    if competence:
        return datetime.strptime(competence, "%Y-%m").date().replace(day=1)
    return booked_at.replace(day=1)


def _installment_remaining_schedule(
    *, amount: Decimal, installment_current: int, installment_total: int, anchor_month: date
) -> list[tuple[str, Decimal]]:
    """Canonical month -> amount schedule for the installments that remain
    *after* `installment_current`, given the confirmed per-installment
    `amount` and `anchor_month` -- the first-of-month this installment is
    effectively competence-anchored to (`_installment_anchor_month`), which
    is *not* necessarily `booked_at`'s calendar month.

    This is the single source of truth for "which future months this
    commitment adds and how much": `_future_installments` (already-persisted
    purchases feeding the canonical projection) and the pre-confirmation
    `Saídas` preview (`GET /transactions/manual/installment-preview`) both
    call this exact function with the same anchor resolution, so the number
    shown before confirming can never drift from the number the projection
    engine uses afterwards -- the UI is never allowed to compute this on its
    own (no parallel formula in JS), and there is no second formula here
    either for the competence-vs-booked_at distinction.
    """
    remaining = max(0, installment_total - installment_current)
    value = money(abs(amount))
    return [
        (month_key(add_months(anchor_month.replace(day=1), offset)), value)
        for offset in range(1, remaining + 1)
    ]


def _installment_series_key(
    *, account_id: str, card_last_four: str | None, description: str, amount: Decimal, installment_total: int, origin_month: date
) -> tuple:
    """Identity of an installment commitment across its repeated monthly
    observations, shared by `_project_installments` for both a persisted
    `Transaction` and an in-memory not-yet-persisted candidate -- the exact
    same tuple must be computed the same way in both cases, or a genuinely
    later observation of the same purchase would never be recognized as
    such."""
    stripped_description = re.sub(
        r"(?:PARCELA\s*)?\d{1,2}\s*/\s*\d{1,2}",
        " ",
        description,
        flags=re.IGNORECASE,
    )
    return (
        account_id,
        card_last_four or "",
        normalize_description(stripped_description),
        money(abs(amount)),
        installment_total,
        month_key(origin_month),
    )


def _project_installments(items) -> dict[str, Decimal]:
    """Canonical month -> amount projection for a set of installment facts.

    `items` may mix persisted `Transaction` rows with a single in-memory,
    not-yet-persisted candidate (a `SimpleNamespace` carrying the same
    `account_id`/`card_last_four`/`description`/`amount`/
    `installment_current`/`installment_total`/`competence`/`booked_at`
    attributes) -- this is the one place that groups observations into a
    series (`_installment_series_key`) and keeps only the latest one by
    `(booked_at, installment_current)`, so a later observation of an
    existing series *replaces* the earlier one's projected schedule instead
    of adding to it (the same fact observed twice must never be counted
    twice). `_future_installments` (already-persisted commitments) and the
    `Saídas` installment prévia (`GET /transactions/manual/installment-preview`,
    simulating "as if the candidate were persisted" without writing
    anything) both go through this exact function with the exact same
    policy, so the two can never drift on how a series is identified or
    which observation of it wins.
    """
    latest_by_series: dict[tuple, object] = {}
    for item in items:
        current = item.installment_current or 0
        total = item.installment_total or 0
        anchor = _installment_anchor_month(competence=item.competence, booked_at=item.booked_at)
        origin = add_months(anchor, -(max(1, current) - 1))
        series = _installment_series_key(
            account_id=item.account_id,
            card_last_four=item.card_last_four,
            description=item.description,
            amount=item.amount,
            installment_total=total,
            origin_month=origin,
        )
        previous = latest_by_series.get(series)
        if previous is None or (item.booked_at, current) > (
            previous.booked_at,
            previous.installment_current or 0,
        ):
            latest_by_series[series] = item
    values: dict[str, Decimal] = {}
    for item in latest_by_series.values():
        anchor = _installment_anchor_month(competence=item.competence, booked_at=item.booked_at)
        for key, value in _installment_remaining_schedule(
            amount=item.amount,
            installment_current=item.installment_current or 0,
            installment_total=item.installment_total or 0,
            anchor_month=anchor,
        ):
            values[key] = values.get(key, Decimal("0")) + value
    return values


def _persisted_installment_rows(db: Session, household_id: str) -> list[Transaction]:
    return list(
        db.scalars(
            select(Transaction).where(
                Transaction.household_id == household_id,
                Transaction.excluded.is_(False),
                Transaction.installment_current.is_not(None),
                Transaction.installment_total.is_not(None),
            )
        )
    )


def _future_installments(db: Session, household_id: str) -> dict[str, Decimal]:
    return _project_installments(_persisted_installment_rows(db, household_id))


def _build_projection_gate_checks(
    db: Session,
    *,
    household_id: str,
    profile: FinancialProfile,
    snapshot: FinancialSnapshot,
    start_month: date,
    end_month: date,
    check_period: str,
    entity_type: str,
    entity_id: str,
    extra_installments: dict[str, Decimal] | None = None,
) -> tuple[tuple[IntegrityCheck, ...], list[dict], object]:
    """Run the canonical Projection Engine + independent Validator over
    `[start_month, end_month]` starting from `snapshot`'s closing position and
    derive the deterministic checks that gate `trusted_for_projection`
    (INV-005, INV-006, INV-018, INV-022).

    Shared by `GET /forecast` (full display horizon) and
    `POST /monthly-closes/{period}/run` (a minimal one-month check proving
    the just-closed period is a trustworthy seed for the next one) so the
    projection trust gate is never evaluated two different ways -- see the
    engineering review on PR 7 that blocked `run -> trust` from ever reaching
    a real `trusted` state because the monthly close never ran this coverage
    at all.

    `extra_installments` (`month -> amount`, added on top of the household's
    already-persisted `_future_installments`) is the one seam
    `POST /purchases/scenario-comparison` uses to run a not-yet-existing
    purchase through this exact same canonical path instead of a second,
    parallel projection: it never touches persisted data and defaults to
    `None`, which reproduces the previous behaviour exactly for every
    existing caller.
    """

    obligations_rows = db.scalars(
        select(Obligation).where(Obligation.household_id == household_id, Obligation.active.is_(True))
    ).all()
    commission_rows = db.scalars(
        select(Commission).where(
            Commission.household_id == household_id, Commission.status != "cancelled"
        )
    ).all()
    payroll_rows = db.scalars(
        select(PayrollRecord).where(
            PayrollRecord.household_id == household_id, PayrollRecord.payroll_kind != "regular"
        )
    ).all()
    payroll_extras: dict[str, Decimal] = {}
    for item in payroll_rows:
        key = month_key(item.payment_date)
        payroll_extras[key] = payroll_extras.get(key, Decimal("0")) + item.net_amount
    commissions_input = []
    for item in commission_rows:
        _, net = commission_net(item.gross_amount, item.tax_rate)
        commissions_input.append(ForecastCommission(item.expected_date, net, item.delay_days))
    rate = monthly_net_rate(profile.investment_gross_annual_rate, profile.investment_income_tax_rate)
    installments = dict(_future_installments(db, household_id))
    if extra_installments:
        for key, value in extra_installments.items():
            installments[key] = installments.get(key, Decimal("0")) + value
    projection_input = ForecastInput(
        start_month=start_month,
        end_month=end_month,
        starting_balance=Decimal(snapshot.closing_liquidity_balance),
        monthly_salary=profile.monthly_salary_net,
        monthly_cash_cap=profile.monthly_cash_cap,
        monthly_investment_rate=rate,
        obligations=_forecast_obligations(list(obligations_rows)),
        installments=installments,
        payroll_extras=payroll_extras,
        commissions=tuple(commissions_input),
        starting_uncovered_deficit=Decimal(snapshot.closing_uncovered_deficit),
        safety_floor=Decimal(profile.emergency_floor),
    )
    rows = build_forecast(projection_input)
    validation = validate_projection(projection_input, rows)
    liquidity_facts = liquidity_transition_facts(snapshot)
    lineage_facts = snapshot_lineage_facts(db, snapshot)
    checks = (
        IntegrityCheck(
            "INV-018",
            InvariantContext(
                facts={
                    "financial_engine_values": validation.actual_values,
                    "projection_validator_values": validation.expected_values,
                    "monetary_tolerance": PROJECTION_TOLERANCE,
                },
                scope=InvariantScope.PROJECTION,
                entity_type=entity_type,
                entity_id=entity_id,
                period=check_period,
            ),
        ),
        IntegrityCheck(
            "INV-005",
            InvariantContext(
                facts=liquidity_facts,
                scope=InvariantScope.PROJECTION,
                entity_type=entity_type,
                entity_id=entity_id,
                period=check_period,
            ),
        ),
        IntegrityCheck(
            "INV-006",
            InvariantContext(
                facts=liquidity_facts,
                scope=InvariantScope.PROJECTION,
                entity_type=entity_type,
                entity_id=entity_id,
                period=check_period,
            ),
        ),
        IntegrityCheck(
            "INV-022",
            InvariantContext(
                facts=lineage_facts,
                scope=InvariantScope.PROJECTION,
                entity_type=entity_type,
                entity_id=entity_id,
                period=check_period,
            ),
        ),
    )
    return checks, rows, validation


@router.get("/forecast")
def forecast(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    profile = profile_for(db, user.household_id)
    current_start = date.today().replace(day=1)
    current_snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=month_key(current_start),
        generated_by=user.id,
    )
    end = profile.projection_end or date(date.today().year + 1, 12, 1)
    current_period = month_key(current_start)
    projection_identity = f"projection:{user.household_id}:{current_period}"
    # The projection trust gate is not `balance_evidence_trusted and
    # validation.valid`: that pair only proves acceptable opening evidence and
    # engine/validator parity, never the projection gate's required invariant
    # coverage (INV-005, INV-006, INV-018, INV-022 -- `_trust_gate` in
    # financial_integrity.py). A snapshot can fail lineage or liquidity
    # closure and still reproduce identical engine/validator numbers, so this
    # run evaluates the full required set against the current snapshot itself
    # and lets the canonical `_trust_gate` decide, instead of substituting a
    # more permissive ad hoc gate.
    checks, rows, validation = _build_projection_gate_checks(
        db,
        household_id=user.household_id,
        profile=profile,
        snapshot=current_snapshot,
        start_month=add_months(date.today().replace(day=1), 1),
        end_month=end,
        check_period=current_period,
        entity_type="projection",
        entity_id=projection_identity,
    )
    integrity_run, gate_results = execute_integrity_run(
        db,
        household_id=user.household_id,
        scope=IntegrityRunScope.PROJECTION,
        trigger=IntegrityRunTrigger.SYSTEM,
        checks=checks,
        created_by=user.id,
        period=current_period,
        scope_entity_type="projection",
        scope_entity_id=projection_identity,
        calculation_version=PROJECTION_CALCULATION_VERSION,
    )
    rate = monthly_net_rate(profile.investment_gross_annual_rate, profile.investment_income_tax_rate)
    projection_gate_trusted = bool(integrity_run.summary.get("trusted_for_projection", False))
    balance_evidence_trusted = bool(current_snapshot.payload.get("balance_evidence_trusted", False))
    inv018_result = next(result for result in gate_results if result.invariant_id == "INV-018")
    db.commit()
    serialized = [
        {key: decimal_value(value) if isinstance(value, Decimal) else value for key, value in row.items()}
        for row in rows
    ]
    delayed_balances = [row["balance_delayed"] for row in rows] or [0]
    return {
        "monthly_net_investment_rate": float(rate),
        "rows": serialized,
        "summary": {
            "starting_balance": decimal_value(current_snapshot.closing_liquidity_balance),
            "current_uncovered_deficit": decimal_value(
                current_snapshot.closing_uncovered_deficit
            ),
            "final_delayed": delayed_balances[-1],
            "minimum_delayed": min(delayed_balances),
            "emergency_floor": decimal_value(profile.emergency_floor),
            "viable": min(delayed_balances) >= decimal_value(profile.emergency_floor),
            "trusted_for_projection": bool(
                balance_evidence_trusted and validation.valid and projection_gate_trusted
            ),
            "projection_formula_trusted": validation.valid,
            "projection_invariant_gate_trusted": projection_gate_trusted,
            "source_snapshot_trusted_for_projection": current_snapshot.trusted_for_projection,
            "source_balance_evidence_trusted": balance_evidence_trusted,
            "source_snapshot_integrity_status": current_snapshot.integrity_status,
            "integrity_status": inv018_result.status.value,
            "integrity_run_id": integrity_run.id,
            "source_snapshot_id": current_snapshot.id,
            "source_snapshot_checksum": current_snapshot.checksum,
            "projection_calculation_version": PROJECTION_CALCULATION_VERSION,
            "validator_tolerance": float(PROJECTION_TOLERANCE),
            "validator_mismatches": len(validation.mismatches),
        },
    }


def _scenario_projection_summary(rows: list[dict], scenario: str) -> dict:
    """Summarize one canonical projection scenario (`no_commission`,
    `delayed` or `expected`) as independent, non-prescriptive facts -- never
    a single derived verdict that elevates one scenario over the other two.

    `docs/FINANCIAL_RULES.md`/the Advisor's own disclosed assumptions
    describe the safety floor as a reference/alert, never blocked money or a
    hard limit ("um déficit real pode consumi-lo e até zerar a liquidez");
    `docs/WORK_ORDER_PURCHASE_SCENARIO_COMPARISON.md` requires it be treated
    as a reference, never an artificial block, and forbids fabricating a
    recommendation. An earlier revision of this function fed a single
    `min(balance_delayed) >= emergency_floor` boolean into a `viable` field
    that (a) treated crossing the floor as equivalent to economic
    unviability even when the balance stayed positive with no uncovered
    deficit at all, and (b) picked `delayed` as the one authoritative
    scenario for that verdict with no normative contract saying it should
    be -- see the engineering review on this PR. `crosses_safety_floor` and
    `has_uncovered_deficit` below are the fix: pure boolean readouts of
    fields the canonical Projection Engine/Validator already produce and
    INV-018 already validates (`distance_to_floor_{scenario}` is signed,
    `balance - safety_floor`, in `projection_validator.py`), reported once
    per scenario instead of collapsed into a single cross-scenario flag.
    Never use these two fields to gate, block or recommend anything; they
    are facts for a human to read, not a computed decision.
    """

    balances = [row[f"balance_{scenario}"] for row in rows] or [Decimal("0")]
    deficits = [row[f"uncovered_deficit_{scenario}"] for row in rows] or [Decimal("0")]
    distances = [row[f"distance_to_floor_{scenario}"] for row in rows] or [Decimal("0")]
    minimum_distance_to_floor = min(distances)
    maximum_uncovered_deficit = max(deficits)
    return {
        "final_balance": decimal_value(balances[-1]),
        "minimum_balance": decimal_value(min(balances)),
        "final_uncovered_deficit": decimal_value(deficits[-1]),
        "maximum_uncovered_deficit": decimal_value(maximum_uncovered_deficit),
        "minimum_distance_to_floor": decimal_value(minimum_distance_to_floor),
        "crosses_safety_floor": minimum_distance_to_floor < 0,
        "has_uncovered_deficit": maximum_uncovered_deficit > 0,
    }


def _purchase_scenario_candidate_schedule(
    alternative: PurchaseScenarioAlternativeRequest, *, purchase_month: date
) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    """Deterministic `month -> amount` schedule for one hypothetical
    purchase, plus its `(monthly_payment, total_financed_cost)`.

    Reuses `app.services.finance.amortized_installment_payment` (the exact
    formula the Advisor chat's free-text parser already uses -- never a
    second amortization formula) for the amount of each installment. No
    `Transaction`/`Obligation` row is read or written for this candidate; it
    exists only in memory for this one request.

    `purchase_month` is, literally and without exception, the first month
    this purchase affects the projection (matching its own field
    documentation, `PurchaseScenarioAlternativeRequest.purchase_month`): the
    down payment, if any, and the financed remainder's first installment
    (if any) are both due in that same month; each subsequent installment
    follows one month after the previous one. There is no inferred
    "first installment due next month" gap -- an earlier revision of this
    function introduced exactly that as a disclosed-but-unsourced
    convention justified by "common retail installment plans", which the
    engineering review on this PR correctly rejected as inventing calendar
    semantics the Work Order forbids (`docs/WORK_ORDER_PURCHASE_SCENARIO_
    COMPARISON.md`: reuse existing contracts, never a second one). This
    version makes no assumption at all about when a real retailer would
    charge the first installment; it only places `installment_count`
    equal amounts starting at `purchase_month`, which is the one placement
    consistent with `purchase_month`'s own documented meaning for every
    combination of `down_payment`/`installment_count`.

    `_installment_remaining_schedule` -- the single source of truth for
    "which future months a commitment adds and how much" for
    already-persisted card installments -- is deliberately *not* reused
    here: its `installment_current`/`installment_total` contract describes
    the months remaining *after* an installment that a real `Transaction`
    already booked, which has no equivalent for a purchase that was only
    compared, never made. Reusing it anyway (as the earlier revision did,
    passing `installment_current=0` to fabricate a pre-first-installment
    "month zero") does not turn its result into a canonical convention; it
    would still be this function inventing one. Placing `installment_count`
    equal amounts on consecutive months from `purchase_month` is instead
    plain, unambiguous date arithmetic with no embedded financial policy.
    """

    financed_amount = money(alternative.price - alternative.down_payment)
    if financed_amount <= 0:
        monthly_payment = Decimal("0")
        total_financed_cost = Decimal("0")
    else:
        monthly_payment, total_financed_cost = amortized_installment_payment(
            financed_amount, alternative.installment_count, alternative.monthly_interest_rate
        )
    schedule: dict[str, Decimal] = {}
    if alternative.down_payment > 0:
        schedule[month_key(purchase_month)] = money(alternative.down_payment)
    if financed_amount > 0:
        for offset in range(alternative.installment_count):
            key = month_key(add_months(purchase_month, offset))
            schedule[key] = schedule.get(key, Decimal("0")) + monthly_payment
    return schedule, monthly_payment, total_financed_cost


@router.post("/purchases/scenario-comparison")
def compare_purchase_scenarios(
    payload: PurchaseScenarioComparisonRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only comparison of two or more hypothetical purchase
    alternatives, each run through the exact canonical Projection
    Engine/Validator path `GET /forecast` uses
    (`_build_projection_gate_checks`) -- never a second projection/liquidity/
    deficit/floor/commission formula here or in the browser
    (`docs/WORK_ORDER_PURCHASE_SCENARIO_COMPARISON.md`).

    Purely simulative: nothing here is persisted. Every invariant check is
    evaluated in memory (`evaluate_invariant`/`assess_integrity`, both pure
    functions with no database access) instead of `execute_integrity_run`,
    and `db` is never committed in this request -- `build_snapshot`'s own
    internal `add`/`flush` calls (idempotent: it returns the existing
    current-period snapshot unchanged when nothing about the household's
    real facts has changed) are discarded when `get_db` closes the session
    at the end of the request without a commit. A hypothetical purchase that
    was only compared, never confirmed, must never leave an
    `IntegrityRun`/`IntegrityFinding` audit trail as if it had actually been
    evaluated for real money movement, and must never mutate `Transaction`,
    `Obligation`, `Commission`, `PayrollRecord`, `Document` or any other
    historical fact.
    """

    profile = profile_for(db, user.household_id)
    current_start = date.today().replace(day=1)
    current_period = month_key(current_start)
    current_snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=current_period,
        generated_by=user.id,
    )
    end = profile.projection_end or date(date.today().year + 1, 12, 1)
    start_month = add_months(current_start, 1)
    balance_evidence_trusted = bool(current_snapshot.payload.get("balance_evidence_trusted", False))

    def _run_scenario(*, extra_installments: dict[str, Decimal] | None, entity_suffix: str) -> dict:
        checks, rows, validation = _build_projection_gate_checks(
            db,
            household_id=user.household_id,
            profile=profile,
            snapshot=current_snapshot,
            start_month=start_month,
            end_month=end,
            check_period=current_period,
            entity_type="projection_scenario_comparison",
            entity_id=(
                f"projection_scenario_comparison:{user.household_id}:"
                f"{current_period}:{entity_suffix}"
            ),
            extra_installments=extra_installments,
        )
        results = tuple(evaluate_invariant(check.invariant_id, check.context) for check in checks)
        assessment = assess_integrity(results)
        inv018_result = next(result for result in results if result.invariant_id == "INV-018")
        projection_gate_trusted = assessment.trusted_for_projection
        trusted_for_projection = bool(
            balance_evidence_trusted and validation.valid and projection_gate_trusted
        )
        serialized_rows = [
            {
                key: decimal_value(value) if isinstance(value, Decimal) else value
                for key, value in row.items()
            }
            for row in rows
        ]
        return {
            "scenarios": {
                scenario: _scenario_projection_summary(rows, scenario)
                for scenario in ("no_commission", "delayed", "expected")
            },
            "trusted_for_projection": trusted_for_projection,
            "projection_formula_trusted": validation.valid,
            "projection_invariant_gate_trusted": projection_gate_trusted,
            "integrity_status": inv018_result.status.value,
            "validator_mismatches": len(validation.mismatches),
            "commitment_schedule": _advisor_commitment_schedule({"rows": serialized_rows}),
        }

    baseline = _run_scenario(extra_installments=None, entity_suffix="baseline")

    alternatives_response = []
    for index, alternative in enumerate(payload.alternatives):
        if alternative.purchase_month:
            purchase_month = datetime.strptime(alternative.purchase_month, "%Y-%m").date().replace(
                day=1
            )
        else:
            purchase_month = start_month
        if purchase_month < start_month or purchase_month > end:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"O mês da compra de '{alternative.label}' precisa estar entre "
                    f"{month_key(start_month)} e {month_key(end)} para poder aparecer na "
                    "projeção; o mês corrente já está fechado pelo snapshot atual."
                ),
            )
        schedule, monthly_payment, total_financed_cost = _purchase_scenario_candidate_schedule(
            alternative, purchase_month=purchase_month
        )
        scenario_result = _run_scenario(extra_installments=schedule, entity_suffix=str(index))
        alternatives_response.append(
            {
                "label": alternative.label,
                "inputs": {
                    "price": decimal_value(alternative.price),
                    "down_payment": decimal_value(alternative.down_payment),
                    "installment_count": alternative.installment_count,
                    "monthly_interest_rate": decimal_value(alternative.monthly_interest_rate),
                    "purchase_month": month_key(purchase_month),
                },
                "monthly_payment": decimal_value(monthly_payment),
                "total_purchase_cost": decimal_value(
                    money(alternative.down_payment + total_financed_cost)
                ),
                "candidate_installment_schedule": [
                    {"month": key, "amount": decimal_value(value)}
                    for key, value in sorted(schedule.items())
                ],
                **scenario_result,
            }
        )

    return {
        "baseline": baseline,
        "alternatives": alternatives_response,
        "emergency_floor": decimal_value(profile.emergency_floor),
        "projection_start_month": month_key(start_month),
        "projection_end_month": month_key(end),
        "projection_calculation_version": PROJECTION_CALCULATION_VERSION,
    }


def _advisor_commitment_schedule(forecast_data: dict, months: int = 6) -> list[dict]:
    schedule = []
    for row in forecast_data.get("rows", [])[:months]:
        installments = money(row.get("installments", 0))
        obligations = money(row.get("obligations", 0))
        schedule.append(
            {
                "month": row["month"],
                "card_installments": decimal_value(installments),
                "obligations": decimal_value(obligations),
                "total": decimal_value(money(installments + obligations)),
            }
        )
    return schedule


def _advisor_commitment_appendix(schedule: list[dict]) -> str:
    if not schedule:
        return ""
    lines = []
    for row in schedule:
        year, month = (int(value) for value in row["month"].split("-"))
        lines.append(
            f"- {MONTH_LABELS_PT[month]} de {year}: cartões "
            f"{_brl(row['card_installments'])} + obrigações "
            f"{_brl(row['obligations'])} = {_brl(row['total'])}"
        )
    return "Cronograma dos próximos meses já considerado no cálculo:\n" + "\n".join(lines)


def _advisor_purchase_reflection_appendix(
    status_name: str,
    purchase_context: dict[str, object],
    *,
    answered: bool = False,
) -> str:
    kind = str(purchase_context["kind"])
    depth = str(purchase_context["analysis_depth"])
    category = str(purchase_context["suggested_category"])

    if depth == "quick":
        if kind == "personal_care":
            detail = (
                "É um item de consumo pessoal e de baixo impacto. Aluguel, manutenção e "
                "análise patrimonial não se aplicam; confira apenas se já existe um produto "
                f"equivalente e acompanhe a recorrência na categoria {category}."
            )
        elif kind in {"consumable", "clothing"}:
            detail = (
                "É uma compra de baixo impacto. Basta conferir se ela não duplica algo que já "
                f"existe e acompanhar a recorrência na categoria {category}."
            )
        else:
            detail = (
                "O impacto isolado é baixo, portanto não faz sentido aplicar um checklist de "
                "bem durável. Observe apenas se pequenas compras semelhantes estão se repetindo."
            )
        if status_name == "not_recommended":
            detail = (
                "O item isoladamente tem baixo impacto, mas o teto do mês já foi ultrapassado. "
                "O foco deve ser o conjunto das despesas, não uma comparação artificial com "
                f"aluguel ou manutenção. Registre a compra em {category}."
            )
        return f"Compra consciente:\n{detail}"

    if answered:
        answered_guidance = {
            "equipment": (
                "Compare o custo por uso de comprar com aluguel, contratação do serviço ou "
                "compra usada, incluindo manutenção e armazenamento."
            ),
            "vehicle": (
                "Considere o custo total de propriedade: seguro, impostos, manutenção, "
                "combustível e depreciação, além do preço de compra."
            ),
            "electronics": (
                "Compare o ganho real sobre o equipamento atual, a vida útil esperada, garantia "
                "e alternativas usadas ou recondicionadas."
            ),
            "property": (
                "Confirme custo total, documentação, manutenção e impacto nos demais projetos "
                "antes de comprometer o caixa."
            ),
            "personal_care": (
                "Considere se já existe produto equivalente, a frequência real de uso e a "
                f"recorrência na categoria {category}."
            ),
        }
        guidance = answered_guidance.get(
            kind,
            "Compare a necessidade real, a frequência de uso e uma alternativa mais econômica.",
        )
        return (
            "Compra consciente:\nAs informações adicionais foram consideradas. "
            f"{guidance} Se a comparação ainda não estiver clara, adie a decisão."
        )

    if status_name == "not_recommended":
        opening = (
            "Pelo caixa, a resposta já é não por enquanto. Mesmo quando houver folga, "
            "não transforme capacidade de pagamento em justificativa automática para comprar."
        )
    else:
        opening = (
            "Caber matematicamente no orçamento não significa que a compra vale a pena. "
            "Antes de decidir, responda com honestidade:"
        )

    questions_by_kind = {
        "equipment": (
            "Necessidade: isso resolve um problema real agora ou é vontade do momento?",
            "Uso: quantas vezes você realmente usará nos próximos 12 meses?",
            "Alternativa: quanto custaria alugar, contratar o serviço ou comprar usado?",
            "Custo total: haverá manutenção, combustível, armazenamento ou perda de valor?",
            "Impulso: se não for urgente, espere 72 horas e refaça a pergunta.",
        ),
        "vehicle": (
            "Necessidade: o veículo resolve uma necessidade real ou apenas um desejo de troca?",
            "Uso: qual será a frequência e qual problema de mobilidade ele resolve?",
            "Custo total: quanto somam seguro, impostos, manutenção, combustível e depreciação?",
            "Alternativa: manter o veículo atual, alugar ou usar transporte por demanda custa menos?",
            "Impulso: compare propostas e espere 72 horas antes de assumir o compromisso.",
        ),
        "electronics": (
            "Necessidade: o equipamento atual deixou de atender ou a compra é apenas um upgrade?",
            "Uso: quantas horas por semana o produto realmente será utilizado?",
            "Alternativa: usado, recondicionado ou reparo do atual resolveria por menos?",
            "Custo total: há jogos, acessórios, assinaturas, garantia ou perda rápida de valor?",
            "Impulso: espere 72 horas e compare preços antes de decidir.",
        ),
        "property": (
            "Objetivo: qual problema familiar ou patrimonial essa compra resolve?",
            "Custo total: documentação, impostos, obra e manutenção estão incluídos?",
            "Prioridade: ela atrasa a reserva ou outro projeto mais importante?",
            "Risco: existe margem para imprevistos sem consumir a reserva mínima?",
            "Decisão: revise documentos e orçamento antes de assumir o compromisso.",
        ),
        "personal_care": (
            "Necessidade: já existe produto equivalente em casa?",
            "Uso: ele será utilizado antes de vencer ou perder qualidade?",
            f"Recorrência: a soma dessas compras continua adequada à categoria {category}?",
            "Alternativa: uma opção mais barata entrega o mesmo resultado?",
            "Impulso: a compra continuaria fazendo sentido amanhã?",
        ),
        "clothing": (
            "Necessidade: a peça cobre uma necessidade ou repete algo que você já possui?",
            "Uso: quantas vezes ela será usada no próximo ano?",
            "Alternativa: uma peça existente ou mais barata atende da mesma forma?",
            f"Recorrência: o total permanece adequado à categoria {category}?",
            "Impulso: espere até amanhã se não houver necessidade imediata.",
        ),
        "consumable": (
            "Necessidade: o item está faltando ou será apenas estoque adicional?",
            "Quantidade: existe risco de desperdício ou vencimento?",
            f"Recorrência: o total permanece adequado à categoria {category}?",
            "Alternativa: outra marca ou quantidade oferece melhor custo por uso?",
        ),
        "general": (
            "Necessidade: qual problema concreto essa compra resolve?",
            "Uso: com que frequência ela será utilizada?",
            "Alternativa: existe opção mais barata que entrega o mesmo resultado?",
            "Custo total: haverá outros gastos necessários depois da compra?",
            "Impulso: se não for urgente, espere 72 horas e refaça a pergunta.",
        ),
    }
    questions = questions_by_kind[kind]
    if depth == "standard":
        questions = questions[:3]
    closing_by_kind = {
        "equipment": "Se o uso for ocasional e contratar o serviço custar menos, não compre.",
        "vehicle": "A parcela caber não basta: o custo total mensal precisa caber com folga.",
        "electronics": "Se o ganho sobre o equipamento atual for pequeno, adie a troca.",
        "personal_care": "O ponto relevante é a recorrência, não aluguel ou custo patrimonial.",
        "consumable": "Avalie necessidade, quantidade e recorrência; não trate como bem durável.",
    }
    closing = closing_by_kind.get(
        kind,
        "Se a necessidade ou o benefício não forem claros, adie a compra.",
    )
    lines = "\n".join(f"- {question}" for question in questions)
    return f"Compra consciente:\n{opening}\n{lines}\n{closing}"


@router.get("/cut-plan")
def cut_plan(
    month: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    end = add_months(_month_start(month), 1)
    start = add_months(end, -6)
    rows, duplicates_ignored = _consolidated_expenses(db, user.household_id, start, end)
    covered = {(transaction.booked_at.year, transaction.booked_at.month) for transaction, _ in rows}
    month_count = max(1, len(covered))
    monthly_average = money(
        max(
            Decimal("0"),
            sum((-Decimal(transaction.amount) for transaction, _ in rows), Decimal("0")) / month_count,
        )
    )
    totals: dict[str, Decimal] = {}
    for transaction, category_name in rows:
        totals[category_name] = totals.get(category_name, Decimal("0")) - Decimal(transaction.amount)
    categories_rows = db.scalars(
        select(Category).where(Category.household_id == user.household_id, Category.cash_cap.is_not(None))
    ).all()
    recommendations = []
    for category in categories_rows:
        average = money(max(Decimal("0"), totals.get(category.name, Decimal("0")) / month_count))
        configured_cap = money(category.cash_cap or 0)
        excess = max(Decimal("0"), average - configured_cap)
        reduction_rate = Decimal("0.10") if category.essential else Decimal("0.20")
        if configured_cap == 0 and not category.essential:
            reduction_rate = Decimal("0.25")
        saving = money(min(excess, average * reduction_rate))
        if saving <= 0:
            continue
        realistic_target = money(max(Decimal("0"), average - saving))
        if category.essential and configured_cap == 0:
            rationale = "Reduzir gradualmente o uso de dinheiro e priorizar VA/VR quando aplicável."
        elif category.essential:
            rationale = "Meta inicial de até 10%, preservando o que é essencial."
        elif configured_cap == 0:
            rationale = "Começar com até 25% de redução, sem assumir eliminação total da categoria."
        else:
            rationale = "Começar com até 20% de redução e acompanhar por limite semanal."
        recommendations.append(
            {
                "category": category.name,
                "average": decimal_value(average),
                "configured_cap": decimal_value(configured_cap),
                "target": decimal_value(realistic_target),
                "suggested_cut": decimal_value(saving),
                "priority": "alta" if not category.essential or configured_cap == 0 else "média",
                "rationale": rationale,
            }
        )
    recommendations.sort(key=lambda item: (item["priority"] == "alta", item["suggested_cut"]), reverse=True)
    potential = money(sum((Decimal(str(item["suggested_cut"])) for item in recommendations), Decimal("0")))
    return {
        "window_start": start,
        "window_end": end,
        "analysis_start_month": month_key(start),
        "analysis_end_month": month_key(add_months(end, -1)),
        "covered_months": len(covered),
        "duplicates_ignored": duplicates_ignored,
        "monthly_average": decimal_value(monthly_average),
        "potential_monthly_savings": decimal_value(potential),
        "recommendations": recommendations,
    }


@router.get("/financial-snapshots/{period}")
def financial_snapshot(
    period: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    start = _month_start(period)
    snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=month_key(start),
        generated_by=user.id,
    )
    db.commit()
    db.refresh(snapshot)
    return serialize_snapshot(snapshot)


@router.post("/financial-snapshots/{period}/rebuild")
def rebuild_financial_snapshot(
    period: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(user)
    start = _month_start(period)
    snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=month_key(start),
        generated_by=user.id,
        force=True,
    )
    audit(
        db,
        user,
        "financial_snapshot.rebuilt",
        "financial_snapshot",
        snapshot.id,
        {"period": snapshot.period, "version": snapshot.version, "checksum": snapshot.checksum},
        trace_id=snapshot.trace_id,
    )
    db.commit()
    db.refresh(snapshot)
    return serialize_snapshot(snapshot)


@router.get("/financial-snapshots/{snapshot_id}/lineage")
def financial_snapshot_lineage(
    snapshot_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    snapshot = db.scalar(
        select(FinancialSnapshot).where(
            FinancialSnapshot.id == snapshot_id,
            FinancialSnapshot.household_id == user.household_id,
        )
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot financeiro não encontrado")
    rows = tuple(
        db.scalars(
            select(FinancialSnapshotLineage)
            .where(FinancialSnapshotLineage.snapshot_id == snapshot.id)
            .order_by(
                FinancialSnapshotLineage.metric_key,
                FinancialSnapshotLineage.created_at,
                FinancialSnapshotLineage.id,
            )
        ).all()
    )
    return {
        "snapshot_id": snapshot.id,
        "checksum": snapshot.checksum,
        "trace_id": snapshot.trace_id,
        "items": [
            {
                "id": item.id,
                "metric_key": item.metric_key,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "document_id": item.document_id,
                "rule_id": item.rule_id,
                "contribution": (
                    decimal_value(item.contribution) if item.contribution is not None else None
                ),
                "source_role": item.source_role,
                "trace_id": item.trace_id,
            }
            for item in rows
        ],
    }


# FAMILY_FINANCE_CARD_SUMMARY_V10
@router.get("/credit-cards/summary")
def credit_cards_summary(
    month: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # actual_spending: mesmo snapshot canônico do dashboard.
    # projected_installments/future_installments_after_month:
    # mesmo motor canônico `_project_installments` usado pelo /forecast.
    selected_start = _month_start(month)
    selected_month = selected_start.strftime("%Y-%m")

    snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=selected_month,
        generated_by=user.id,
    )
    actual_rows = {
        str(row.get("account_id")): row
        for row in account_cash_flow_rows(snapshot)
        if row.get("account_id") and row.get("account_type") == "credit_card"
    }

    cards = list(
        db.scalars(
            select(Account)
            .where(
                Account.household_id == user.household_id,
                Account.active.is_(True),
                Account.account_type == "credit_card",
            )
            .order_by(Account.institution, Account.name)
        ).all()
    )
    installment_facts = _persisted_installment_rows(db, user.household_id)

    rows = []
    total_actual = Decimal("0")
    total_projected = Decimal("0")
    total_future_after = Decimal("0")

    for card in cards:
        actual_row = actual_rows.get(card.id, {})
        actual_spending = money(
            Decimal(str(actual_row.get("card_spending", 0) or 0))
        )

        card_facts = [
            item for item in installment_facts
            if item.account_id == card.id
        ]
        projection = _project_installments(card_facts)
        projected_month = money(
            Decimal(projection.get(selected_month, Decimal("0")))
        )
        projected_after = money(
            sum(
                (
                    Decimal(value)
                    for key, value in projection.items()
                    if key > selected_month
                ),
                Decimal("0"),
            )
        )
        projected_months = sorted(
            key for key, value in projection.items()
            if key >= selected_month and money(Decimal(value)) > 0
        )

        total_actual += actual_spending
        total_projected += projected_month
        total_future_after += projected_after

        rows.append(
            {
                "account_id": card.id,
                "account": _account_display(card),
                "institution": card.institution,
                "last_four": card.last_four,
                "actual_spending": decimal_value(actual_spending),
                "projected_installments": decimal_value(projected_month),
                "future_installments_after_month": decimal_value(projected_after),
                "first_projected_month": projected_months[0] if projected_months else None,
                "last_projected_month": projected_months[-1] if projected_months else None,
                "installment_source_rows": len(card_facts),
            }
        )

    return {
        "month": selected_month,
        "rows": rows,
        "totals": {
            "actual_spending": decimal_value(money(total_actual)),
            "projected_installments": decimal_value(money(total_projected)),
            "future_installments_after_month": decimal_value(money(total_future_after)),
        },
        "accounting_note": (
            "Pagamento de fatura é conciliação e não é somado novamente como gasto."
        ),
    }


@router.get("/dashboard")
def dashboard(
    month: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    profile = profile_for(db, user.household_id)
    start = _month_start(month)
    end = add_months(start, 1)
    snapshot = build_snapshot(
        db,
        household_id=user.household_id,
        period=month_key(start),
        generated_by=user.id,
    )
    snapshot_payload = serialize_snapshot(snapshot)
    review_count = db.scalar(
        select(func.count(ReviewItem.id))
        .join(Transaction, Transaction.id == ReviewItem.transaction_id)
        .where(
            ReviewItem.household_id == user.household_id,
            ReviewItem.status == "open",
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
        )
    )
    # `future_commissions_gross` is a live query against `Commission` rows
    # with `expected_date` in the current window and `status != "cancelled"`
    # -- not a Financial Engine/snapshot output. It is deliberately excluded
    # from `dashboard_monetary_publication`/INV-019: unlike a snapshot
    # column, this figure can change between two reads of the *same*,
    # already-generated snapshot (a commission gets cancelled or
    # rescheduled a moment later), so folding it into the INV-019 gate would
    # either falsely certify a value the snapshot never fixed, or make
    # `trusted_for_reports` flap on data that isn't a financial fact
    # `build_snapshot` recorded. See the engineering review on PR 7, Round 6:
    # "classify explicitly ... do not present as certified by
    # snapshot/INV-019; preserve the contract without inventing or freezing
    # a financial fact."
    #
    # Round 7: a bare top-level `future_commissions_gross` scalar sat beside
    # `snapshot_id`/`snapshot_checksum`/`trusted_for_reports` with nothing in
    # the response itself telling a consumer it is the odd one out -- see
    # the engineering review on PR 7, Round 7: "cannot remain a first-level
    # monetary field ... without marking". Nothing in this codebase (no
    # frontend, no test outside this endpoint's own) reads the flat key, so
    # there is no compatible consumer to preserve a deprecation shim for;
    # it is dropped and replaced by `noncanonical.future_commissions_gross`,
    # an explicitly-labelled object carrying `source`/`freshness`/
    # `certified_by`/`as_of` so a consumer cannot mistake it for a
    # snapshot-backed, INV-019-covered figure. If a future slice needs this
    # value certified, the fix belongs in the Financial Engine (a real
    # snapshot column), not in loosening what `noncanonical` means.
    future_commission = db.scalar(
        select(func.coalesce(func.sum(Commission.gross_amount), 0)).where(
            Commission.household_id == user.household_id,
            Commission.expected_date >= start,
            Commission.status != "cancelled",
        )
    )
    current_liquidity_observation = None
    liquidity_account = db.scalar(
        select(Account)
        .where(
            Account.household_id == user.household_id,
            Account.active.is_(True),
            Account.account_type == "investment",
            Account.name == profile.investment_name,
        )
        .limit(1)
    )
    if liquidity_account is not None:
        observed = db.scalar(
            select(AccountBalanceObservation)
            .where(
                AccountBalanceObservation.household_id == user.household_id,
                AccountBalanceObservation.account_id == liquidity_account.id,
                AccountBalanceObservation.as_of_date >= start,
                AccountBalanceObservation.as_of_date < end,
                AccountBalanceObservation.invalidated_at.is_(None),
                AccountBalanceObservation.superseded_by_id.is_(None),
                AccountBalanceObservation.source == "manual_confirmed",
                AccountBalanceObservation.confidence >= Decimal("1"),
            )
            .order_by(
                AccountBalanceObservation.as_of_date.desc(),
                AccountBalanceObservation.created_at.desc(),
            )
            .limit(1)
        )
        if observed is not None:
            current_liquidity_observation = {
                "value": decimal_value(observed.amount),
                "as_of_date": observed.as_of_date.isoformat(),
                "account_id": observed.account_id,
                "source": "account_balance_observation",
                "freshness": "point_in_time",
                "certified_by": "manual_confirmed",
                "trusted": True,
            }

    obligation_alerts = [
        item for item in _obligation_rows(db, user.household_id) if item["days_until_due"] <= 30
    ][:5]
    db.commit()
    # `dashboard_monetary_publication` is the exact function INV-019 reads to
    # verify this response -- see its docstring and
    # `dashboard_and_report_consistency_facts`. This endpoint spreads its
    # return value verbatim (only the uniform `decimal_value()` cast applied
    # on top) instead of re-deriving each key inline, so there is no
    # endpoint-only mapping step left for INV-019 to be blind to -- see the
    # engineering review on PR 7, Round 3. `liquidity_starting_balance`,
    # `liquidity_available`, `liquidity_deposit`, `liquidity_withdrawal`
    # (Round 5), and `liquidity_flow`/`emergency_floor`/`food_benefits`
    # (Round 6) used to be built here from `snapshot`/`snapshot_payload`/
    # `profile` directly, outside this dict -- now they all come from
    # `publication` too, so INV-019's `dashboard_and_report_consistency_facts`
    # (which reads the exact same function) observes them as well.
    publication = dashboard_monetary_publication(snapshot, profile=profile)
    return {
        "month": month_key(start),
        "snapshot_id": snapshot.id,
        "snapshot_checksum": snapshot.checksum,
        "integrity_status": snapshot.integrity_status,
        "trusted_for_reports": snapshot.trusted_for_reports,
        **{key: decimal_value(value) for key, value in publication.items()},
        # `cash_flow_by_account`/`category_spending` are non-scalar (row
        # lists), so they cannot join the flat `publication` dict above --
        # `account_cash_flow_rows`/`category_spending_rows` are still the
        # exact functions `dashboard_and_report_consistency_facts()`
        # flattens into the INV-019 facts it observes (see their
        # docstrings), so this endpoint calling them (not `snapshot.payload`
        # inline) keeps the same verbatim-publication guarantee.
        "cash_flow_by_account": account_cash_flow_rows(snapshot),
        "liquidity_name": profile.investment_name,
        "liquidity_direction": (
            "deposit"
            if publication["liquidity_flow"] > 0
            else "withdrawal"
            if publication["liquidity_flow"] < 0
            else "balanced"
        ),
        "review_count": int(review_count or 0),
        "duplicates_ignored": snapshot_payload["duplicates_ignored"],
        "category_spending": category_spending_rows(snapshot),
        # Explicitly-labelled section for monetary figures the response
        # publishes but that are *not* snapshot-backed / INV-019-covered
        # facts -- see the comment above `future_commission`'s query. Every
        # entry here must carry enough metadata (`source`, `freshness`,
        # `certified_by`) for a consumer to tell it apart from the
        # `trusted_for_reports`-gated fields above.
        "noncanonical": {
            "current_liquidity_observation": current_liquidity_observation,
            "future_commissions_gross": {
                "value": decimal_value(future_commission),
                "source": "live_query",
                "freshness": "live",
                "certified_by": None,
                "as_of": datetime.now(UTC).isoformat(),
            },
            # `large_entry_threshold` is the exact confirmation limit
            # `_large_entry_threshold(profile)` computes and every mutable
            # money-entry endpoint (`/captures/{id}/confirm`,
            # `/transactions`, `/transfers`, `/card-payment-reconciliations`)
            # actually enforces server-side before it ever accepts a
            # large-value entry without `confirmed_large_amount`. It is
            # published here, instead of the frontend recomputing
            # `max(5000, monthly_cash_cap * 2)` on its own, so the browser
            # never carries a second, independently-maintained copy of this
            # financial policy -- see `docs/GO_LIVE_MANUAL_UX_PLAN.md` line
            # 114 ("a UI nunca calcula uma política financeira diferente do
            # backend") and the slice 5 PR review that required this fix.
            # It is `noncanonical` (not folded into the `publication` block
            # above / INV-019) because it is a live derivation of the
            # household's *current* `monthly_cash_cap` profile setting, not
            # a fact `build_snapshot` fixed into this period's snapshot --
            # exactly the same reasoning `future_commissions_gross` already
            # documents.
            "large_entry_threshold": {
                "value": decimal_value(_large_entry_threshold(profile)),
                "source": "computed",
                "freshness": "live",
                "certified_by": None,
                "as_of": datetime.now(UTC).isoformat(),
            },
        },
        "obligation_alerts": obligation_alerts,
    }


def _build_report_payload(db: Session, user: User, *, end_month: str | None, months: int) -> dict:
    """Build an auditable comparison for one to twelve consecutive months.

    Extracted from `GET /reports` so `GET /reports/export` (Excel/PDF) can
    call this exact function -- the same validation, the same snapshots via
    `build_snapshot`, the same publication helpers (`report_month_monetary_publication`,
    `category_monetary_publication`, `report_summary_monetary_publication`,
    `category_spending_rows`, `account_cash_flow_rows`) -- instead of a second,
    export-only report-building policy. Every caller of this function gets the
    identical dict `GET /reports` has always returned; nothing below is
    HTTP-specific, and neither export format recomputes or reclassifies
    anything this function did not already compute.
    """
    if months < 1 or months > 12:
        raise HTTPException(status_code=422, detail="O período deve ter entre 1 e 12 meses")

    profile = profile_for(db, user.household_id)
    last_month = _month_start(end_month)
    start = add_months(last_month, -(months - 1))
    end = add_months(last_month, 1)
    movement_rows, _ignored_movements = _consolidated_transactions(db, user.household_id, start, end)

    month_rows: dict[str, dict[str, object]] = {}
    for offset in range(months):
        current = add_months(start, offset)
        key = month_key(current)
        month_rows[key] = {
            "month": key,
            "spending": Decimal("0"),
            "cash_in": Decimal("0"),
            "cash_out": Decimal("0"),
            "cash_net": Decimal("0"),
            "cash_cap": money(profile.monthly_cash_cap),
            "remaining_cap": money(profile.monthly_cash_cap),
            "transaction_count": 0,
        }

    movements_by_month: dict[str, list[tuple[Transaction, str]]] = {key: [] for key in month_rows}
    for transaction, category_name in movement_rows:
        key = month_key(transaction.booked_at.replace(day=1))
        if key in movements_by_month:
            movements_by_month[key].append((transaction, category_name))
            month_rows[key]["transaction_count"] += 1

    serialized_months = []
    report_snapshots: list[FinancialSnapshot] = []
    category_totals: dict[str, Decimal] = {}
    duplicates_ignored = 0
    previous_active_spending: Decimal | None = None
    for key, item in month_rows.items():
        snapshot = build_snapshot(
            db,
            household_id=user.household_id,
            period=key,
            generated_by=user.id,
        )
        report_snapshots.append(snapshot)
        duplicates_ignored += int(snapshot.payload.get("duplicates_ignored", 0))
        # `category_spending_rows` is the exact function `/dashboard`
        # publishes verbatim and INV-019 observes -- summing its rows here
        # (instead of reading `snapshot.payload["category_spending"]`
        # directly) means a regression in that function now surfaces in
        # `/reports`' own `categories` totals too, closing the INV-020 gap
        # the engineering review named on PR 7, Round 7 ("nested amounts of
        # categories ... continues coming direct from snapshot.payload").
        for category in category_spending_rows(snapshot):
            name = str(category["category"])
            category_totals[name] = category_totals.get(name, Decimal("0")) + Decimal(
                str(category["amount"])
            )
        # `report_month_monetary_publication` is the exact function INV-020
        # reads to verify this response -- see its docstring and
        # `dashboard_and_report_consistency_facts`. This row spreads its
        # return value verbatim (only the uniform `decimal_value()` cast
        # applied on top) instead of re-deriving each key inline, so there is
        # no endpoint-only mapping step left for INV-020 to be blind to --
        # see the engineering review on PR 7, Round 3.
        publication = report_month_monetary_publication(snapshot)
        spending = publication["spending"]
        change_percentage = None
        if (
            item["transaction_count"] > 0
            and previous_active_spending is not None
            and previous_active_spending > 0
        ):
            change_percentage = float(
                money(((spending - previous_active_spending) / previous_active_spending) * Decimal("100"))
            )
        serialized_months.append(
            {
                "month": key,
                **{key_name: decimal_value(value) for key_name, value in publication.items()},
                "transaction_count": int(item["transaction_count"]),
                "change_percentage": change_percentage,
                "snapshot_id": snapshot.id,
                "snapshot_checksum": snapshot.checksum,
                "integrity_status": snapshot.integrity_status,
            }
        )
        if item["transaction_count"] > 0:
            previous_active_spending = spending

    total_spending = money(sum((Decimal(str(item["spending"])) for item in serialized_months), Decimal("0")))
    total_cash_in = money(sum((Decimal(str(item["cash_in"])) for item in serialized_months), Decimal("0")))
    total_cash_out = money(sum((Decimal(str(item["cash_out"])) for item in serialized_months), Decimal("0")))
    total_bank_cash_out = money(
        sum((Decimal(str(item["bank_cash_out"])) for item in serialized_months), Decimal("0"))
    )
    total_card_spending = money(
        sum((Decimal(str(item["card_spending"])) for item in serialized_months), Decimal("0"))
    )
    covered_months = sum(item["transaction_count"] > 0 for item in serialized_months)
    average_denominator = Decimal(max(1, covered_months))
    average_spending = money(total_spending / average_denominator)
    activity_rows = [item for item in serialized_months if item["transaction_count"] > 0]
    comparison_rows = activity_rows or serialized_months
    highest_month = max(comparison_rows, key=lambda item: item["spending"])
    lowest_month = min(comparison_rows, key=lambda item: item["spending"])
    category_colors = {
        item.name: item.color
        for item in db.scalars(select(Category).where(Category.household_id == user.household_id)).all()
    }
    # `category_monetary_publication` is the exact function INV-020 reads
    # (via `report_categories_publication_facts`) to verify `amount`/
    # `average` for a one-month window -- see its docstring and the
    # engineering review on PR 7, Round 8 ("categories[].average" was
    # previously an endpoint-only division INV-020 could not see). Spread
    # verbatim instead of computing `average` as a separate literal.
    categories = [
        {
            "category": name,
            **{
                key: decimal_value(value)
                for key, value in category_monetary_publication(
                    amount, average_denominator=average_denominator
                ).items()
            },
            "share": float(money((amount / total_spending) * Decimal("100"))) if total_spending > 0 else 0,
            "color": category_colors.get(name, "#64748B"),
        }
        for name, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
        if amount > 0
    ]
    last_change = activity_rows[-1]["change_percentage"] if months > 1 and activity_rows else None
    total_result = money(total_cash_in - total_cash_out)
    first_snapshot = report_snapshots[0]
    last_snapshot = report_snapshots[-1]
    total_liquidity_deposit = money(
        sum(
            (Decimal(str(item.payload["liquidity_deposit"])) for item in report_snapshots),
            Decimal("0"),
        )
    )
    total_liquidity_used = money(
        sum((Decimal(item.liquidity_used) for item in report_snapshots), Decimal("0"))
    )
    # `report_summary_monetary_publication` is the exact function INV-020
    # reads to verify a one-month window's `summary` -- every canonical
    # total/average/liquidity field, not `savings_rate` alone (PR 7, Round
    # 7), and now `highest_spending`/`lowest_spending` too (PR 7, Round 8:
    # these used to be assigned as separate literals below, past this
    # contract) -- see its docstring and
    # `dashboard_and_report_consistency_facts`. `summary` below spreads its
    # return value verbatim instead of assigning any of these keys as its
    # own literal, so there is no endpoint-only assembly step left for
    # INV-020 to be blind to -- see the engineering review on PR 7, Round 7:
    # "all canonical monetary values derived from snapshot/profile that are
    # published by /reports must be represented by side-effect-free
    # publication builders consumed verbatim by the endpoint".
    summary_publication = report_summary_monetary_publication(
        total_spending=total_spending,
        average_spending=average_spending,
        total_cash_in=total_cash_in,
        total_cash_out=total_cash_out,
        total_bank_cash_out=total_bank_cash_out,
        total_card_spending=total_card_spending,
        cash_net=total_result,
        liquidity_starting_balance=first_snapshot.opening_liquidity_balance,
        liquidity_balance=last_snapshot.closing_liquidity_balance,
        emergency_floor=profile.emergency_floor,
        liquidity_available=last_snapshot.distance_to_floor,
        liquidity_deposit=total_liquidity_deposit,
        liquidity_withdrawal=total_liquidity_used,
        liquidity_uncovered_deficit=last_snapshot.closing_uncovered_deficit,
        highest_spending=highest_month["spending"],
        lowest_spending=lowest_month["spending"],
    )
    account_totals: dict[str, dict[str, object]] = {}
    for snapshot in report_snapshots:
        # `account_cash_flow_rows` is the exact function `/dashboard`
        # publishes verbatim and INV-019 observes -- see the matching
        # comment above the `categories` loop for why summing its rows
        # (instead of `snapshot.payload["cash_flow_by_account"]` directly)
        # closes the INV-020 gap for `/reports`' own `accounts` totals.
        for account in account_cash_flow_rows(snapshot):
            key = str(account.get("account_id") or "unidentified")
            total = account_totals.setdefault(
                key,
                {
                    **account,
                    "cash_in": Decimal("0"),
                    "cash_out": Decimal("0"),
                    "bank_cash_out": Decimal("0"),
                    "card_spending": Decimal("0"),
                    "refunds": Decimal("0"),
                    "net": Decimal("0"),
                },
            )
            for metric in (
                "cash_in",
                "cash_out",
                "bank_cash_out",
                "card_spending",
                "refunds",
                "net",
            ):
                total[metric] = Decimal(str(total[metric])) + Decimal(str(account.get(metric, 0)))
    report_accounts = [
        {
            **item,
            **{
                metric: decimal_value(money(Decimal(str(item[metric]))))
                for metric in (
                    "cash_in",
                    "cash_out",
                    "bank_cash_out",
                    "card_spending",
                    "refunds",
                    "net",
                )
            },
        }
        for item in sorted(
            account_totals.values(),
            key=lambda item: (str(item["account_type"]), str(item["account"])),
        )
    ]
    db.commit()
    return {
        "start_month": month_key(start),
        "end_month": month_key(last_month),
        "months": months,
        "covered_months": covered_months,
        "duplicates_ignored": duplicates_ignored,
        "summary": {
            "liquidity_name": profile.investment_name,
            "liquidity_direction": (
                "deposit"
                if total_cash_in > total_cash_out
                else "withdrawal"
                if total_cash_out > total_cash_in
                else "balanced"
            ),
            **{key: decimal_value(value) for key, value in summary_publication.items()},
            "highest_month": highest_month["month"],
            "lowest_month": lowest_month["month"],
            "last_change_percentage": last_change,
        },
        "monthly": serialized_months,
        "categories": categories,
        "accounts": report_accounts,
    }


@router.get("/reports")
def reports(
    end_month: str | None = None,
    months: int = 6,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _build_report_payload(db, user, end_month=end_month, months=months)


@router.get("/reports/export")
def export_report(
    end_month: str | None = None,
    months: int = 6,
    format: str = Query(..., pattern="^(xlsx|pdf)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Export the exact same canonical report `GET /reports` returns as a
    real `.xlsx` or `.pdf` file, generated server-side from that dict --
    never a second calculation, never HTML/CSV renamed to look like one of
    these formats. `format` is the only new parameter; `end_month`/`months`
    take the identical validation and household scoping as `GET /reports`
    because both routes call `_build_report_payload`.
    """

    report = _build_report_payload(db, user, end_month=end_month, months=months)
    generated_at = datetime.now(UTC)
    if format == "xlsx":
        content = build_report_workbook(report, generated_at=generated_at)
    else:
        content = build_report_pdf(report, generated_at=generated_at)
    filename = report_export_filename(report, format)
    return Response(
        content=content,
        media_type=REPORT_EXPORT_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/advisor/status")
def advisor_status(user: User = Depends(get_current_user)) -> dict:
    del user
    client = CodexAdvisorClient()
    result = client.status()
    ready = bool(result.payload and result.payload.get("ready"))
    return {
        "local_engine": True,
        "codex_configured": client.configured,
        "codex_ready": ready,
        "codex_authenticated": bool(result.payload and result.payload.get("authenticated")),
        "model": result.payload.get("model") if result.payload else None,
        "error": result.error,
    }


@router.post("/advisor/chat")
def advisor_chat(
    payload: AdvisorRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Consultative, explanation-only chat over already-computed metrics.

    Role boundary (`docs/WORK_ORDER_ADMIN_READONLY_PROFILES.md`): like
    `POST /integrity/semantic-audit`, never deciding or altering a financial
    fact does not make this route read-only -- it is a `POST` that persists
    an `AuditEvent` of the question itself, which the Work Order's
    acceptance criteria treat as operational state. `_require_admin` runs
    first, exactly like every other mutating route.
    """

    _require_admin(user)
    conversation_message = _advisor_conversation_message(payload)
    target_month = _advisor_month(conversation_message)
    selected_month = month_key(target_month)
    normalized = normalize_description(conversation_message)
    summary = dashboard(month=selected_month, user=user, db=db)
    profile = profile_for(db, user.household_id)
    intent = "help"
    status_name = "informative"
    metrics: dict[str, object] = {
        "month": selected_month,
        "snapshot_id": summary["snapshot_id"],
        "snapshot_checksum": summary["snapshot_checksum"],
        "integrity_status": summary["integrity_status"],
    }
    evidence: list[str] = []
    schedule_appendix = ""
    reflection_appendix = ""
    reflection_context = _advisor_reflection_context(payload)
    assumptions = [
        (f"{profile.investment_name} é a conta central de liquidez: recebe sobras e cobre déficits mensais"),
        "Aplicações e resgates não são receitas nem despesas de consumo",
        "O piso de segurança é uma meta; um déficit real pode consumi-lo e até zerar a liquidez",
        "Dados ainda não lançados não entram na análise",
        "O sistema não consulta o saldo atual do internet banking",
    ]

    purchase_words = {"COMPRA", "COMPRAR", "CUSTA", "ADQUIRIR"}
    if any(word in normalized.split() for word in purchase_words):
        intent = "purchase"
        purchase_amount = _advisor_amount(conversation_message)
        if purchase_amount is None:
            answer = (
                "Informe o valor da compra para eu analisar. Exemplo: "
                "‘Quero comprar algo de R$ 2.000 à vista; posso fazer essa compra?’"
            )
            status_name = "insufficient_data"
        else:
            metrics["purchase_amount"] = decimal_value(purchase_amount)
        if purchase_amount is not None and Decimal(str(summary["cash_cap"])) <= 0:
            answer = (
                f"Ainda não consigo avaliar a compra de {_brl(purchase_amount)} porque o teto "
                "mensal não está configurado. Preencha o perfil financeiro primeiro."
            )
            status_name = "insufficient_data"
        elif purchase_amount is not None:
            cash_cap = Decimal(str(summary["cash_cap"]))
            provisional_context = _advisor_purchase_context(
                conversation_message,
                purchase_amount,
                cash_cap,
            )
            payment = _advisor_payment(
                conversation_message,
                purchase_amount,
                assume_cash=provisional_context["analysis_depth"] == "quick",
            )
            if not payment["complete"]:
                answer = str(payment["question"])
                status_name = "insufficient_data"
            else:
                total_cost = Decimal(str(payment["total_cost"]))
                purchase_context = _advisor_purchase_context(
                    conversation_message,
                    purchase_amount,
                    cash_cap,
                    total_cost=total_cost,
                    installments=int(payment["installments"]),
                )
                forecast_data = forecast(user=user, db=db)
                commitment_schedule = _advisor_commitment_schedule(forecast_data)
                if purchase_context["show_commitment_schedule"]:
                    schedule_appendix = _advisor_commitment_appendix(commitment_schedule)
                card_installments_projected = money(
                    sum(
                        (Decimal(str(row["installments"])) for row in forecast_data["rows"]),
                        Decimal("0"),
                    )
                )
                obligations_projected = money(
                    sum(
                        (Decimal(str(row["obligations"])) for row in forecast_data["rows"]),
                        Decimal("0"),
                    )
                )
                remaining_before = Decimal(str(summary["remaining_cap"]))
                monthly_impact = Decimal(str(payment["monthly_payment"]))
                remaining_after = money(remaining_before - monthly_impact)
                minimum_projected = Decimal(str(forecast_data["summary"]["minimum_delayed"]))
                floor = Decimal(str(forecast_data["summary"]["emergency_floor"]))
                projection_margin = money(minimum_projected - floor)
                future_commitment = money(max(Decimal("0"), total_cost - monthly_impact))
                projection_margin_after = money(projection_margin - future_commitment)

                today = date.today()
                horizon = today + timedelta(days=180)
                obligation_models = db.scalars(
                    select(Obligation).where(
                        Obligation.household_id == user.household_id,
                        Obligation.active.is_(True),
                    )
                ).all()
                occurrences = sorted(
                    (
                        (due, item)
                        for item in obligation_models
                        for due in _obligation_dates(item)
                        if today <= due <= horizon
                    ),
                    key=lambda occurrence: (
                        occurrence[0],
                        occurrence[1].name.casefold(),
                        occurrence[1].id,
                    ),
                )
                upcoming_total = money(
                    sum((Decimal(item.amount) for _due, item in occurrences), Decimal("0"))
                )
                next_note = ""
                if occurrences:
                    next_due, next_item = occurrences[0]
                    next_note = (
                        f" A próxima obrigação cadastrada é {next_item.name}, de "
                        f"{_brl(next_item.amount)}, em {next_due:%d/%m/%Y}."
                    )
                payment_note = "à vista"
                if int(payment["installments"]) > 1:
                    payment_note = f"em {payment['installments']} parcelas de {_brl(monthly_impact)}" + (
                        f", com custo total estimado de {_brl(total_cost)}"
                        if Decimal(str(payment["monthly_interest_rate"])) > 0
                        else ", sem juros"
                    )

                is_quick_analysis = purchase_context["analysis_depth"] == "quick"
                context_label = str(purchase_context["label"])
                context_category = str(purchase_context["suggested_category"])
                month_label = f"{MONTH_LABELS_PT[target_month.month]} de {target_month.year}"
                if remaining_after < 0 and is_quick_analysis:
                    status_name = "not_recommended"
                    answer = (
                        f"A compra de {_brl(purchase_amount)} tem impacto baixo isoladamente, "
                        f"mas o teto de {month_label} já está ultrapassado em "
                        f"{_brl(abs(remaining_before))}. O problema não é este {context_label} "
                        "sozinho, e sim o conjunto de despesas do mês."
                    )
                elif remaining_after < 0:
                    status_name = "not_recommended"
                    answer = (
                        f"Minha recomendação é não fazer essa compra agora. O impacto de "
                        f"{_brl(monthly_impact)} {payment_note} ultrapassaria o teto disponível "
                        f"em {_brl(abs(remaining_after))}. Neste mês você já gastou "
                        f"{_brl(summary['spending'])} de um teto de {_brl(summary['cash_cap'])}."
                        f"{next_note}"
                    )
                elif (
                    not forecast_data["summary"]["viable"] or projection_margin_after < 0
                ) and is_quick_analysis:
                    status_name = "not_recommended"
                    answer = (
                        f"A compra de {_brl(purchase_amount)} é pequena isoladamente, mas a "
                        f"projeção do {profile.investment_name} já está abaixo do piso de segurança. "
                        "Antes de somar novas despesas, mesmo na categoria "
                        f"{context_category}, recomponha essa margem."
                    )
                elif not forecast_data["summary"]["viable"] or projection_margin_after < 0:
                    status_name = "not_recommended"
                    answer = (
                        f"Embora a compra de {_brl(purchase_amount)} {payment_note} caiba no teto "
                        "imediato, eu não a recomendo agora: o saldo projetado do "
                        f"{profile.investment_name} ficaria abaixo do piso de segurança em "
                        f"{_brl(abs(projection_margin_after))}. "
                        f"Restariam {_brl(remaining_after)} no teto após o primeiro impacto.{next_note}"
                    )
                elif is_quick_analysis:
                    status_name = "favorable"
                    assumed_note = (
                        " Considerei pagamento à vista por ser uma compra de baixo valor."
                        if payment.get("assumed_cash")
                        else ""
                    )
                    answer = (
                        f"A compra de {_brl(purchase_amount)} tem impacto baixo no orçamento de "
                        f"{month_label}: restariam {_brl(remaining_after)} no teto. Por se tratar "
                        f"de {context_label}, acompanhe a recorrência na categoria "
                        f"{context_category}.{assumed_note}"
                    )
                elif (
                    monthly_impact >= remaining_before * Decimal("0.50")
                    or projection_margin_after < total_cost
                ):
                    status_name = "caution"
                    answer = (
                        f"A compra de {_brl(purchase_amount)} {payment_note} cabe matematicamente, "
                        f"mas exige cautela: restariam {_brl(remaining_after)} no teto do mês e "
                        f"a margem conservadora do {profile.investment_name} acima do piso de "
                        "segurança, após as parcelas futuras, seria "
                        f"{_brl(max(Decimal('0'), projection_margin_after))}.{next_note}"
                    )
                else:
                    status_name = "favorable"
                    answer = (
                        f"A compra de {_brl(purchase_amount)} {payment_note} cabe no cenário atual: "
                        f"restariam {_brl(remaining_after)} no teto do mês. Depois dos compromissos "
                        "futuros dessa compra, a projeção conservadora do "
                        f"{profile.investment_name} ainda ficaria acima do piso de segurança por "
                        f"{_brl(projection_margin_after)}.{next_note}"
                    )
                answer += " A análise não inclui gastos que ainda não foram lançados."
                reflection_appendix = _advisor_purchase_reflection_appendix(
                    status_name,
                    purchase_context,
                    answered=reflection_context is not None,
                )
                metrics.update(
                    {
                        "purchase_amount": decimal_value(purchase_amount),
                        "payment": payment,
                        "remaining_before": decimal_value(remaining_before),
                        "remaining_after": decimal_value(remaining_after),
                        "projection_margin_before": decimal_value(projection_margin),
                        "projection_margin_after": decimal_value(projection_margin_after),
                        "liquidity_name": profile.investment_name,
                        "liquidity_balance": summary["liquidity_balance"],
                        "liquidity_starting_balance": summary["liquidity_starting_balance"],
                        "liquidity_uncovered_deficit": summary["liquidity_uncovered_deficit"],
                        "liquidity_available": summary["liquidity_available"],
                        "obligations_next_180_days": decimal_value(upcoming_total),
                        "card_installments_in_projection": decimal_value(card_installments_projected),
                        "obligations_in_projection": decimal_value(obligations_projected),
                        "commitment_schedule": commitment_schedule,
                        "purchase_context": purchase_context,
                        "show_commitment_schedule": purchase_context["show_commitment_schedule"],
                        "purchase_reflection_answered": reflection_context is not None,
                    }
                )
                evidence = [
                    f"Gasto do mês: {_brl(summary['spending'])}",
                    f"Teto disponível antes da compra: {_brl(remaining_before)}",
                    f"Impacto mensal da compra: {_brl(monthly_impact)}",
                    f"Margem conservadora após compromissos: {_brl(projection_margin_after)}",
                    (
                        f"Saldo do {profile.investment_name} após o fechamento do mês: "
                        f"{_brl(summary['liquidity_balance'])}"
                    ),
                    f"Déficit atual sem cobertura: {_brl(summary['liquidity_uncovered_deficit'])}",
                    f"Obrigações cadastradas nos próximos 180 dias: {_brl(upcoming_total)}",
                    f"Parcelas futuras dos cartões na projeção: {_brl(card_installments_projected)}",
                ]
    elif any(word in normalized for word in ("OBRIGAC", "VENCIMENTO", "VENCE", "PARCELA")):
        intent = "obligations"
        forecast_data = forecast(user=user, db=db)
        commitment_schedule = _advisor_commitment_schedule(forecast_data)
        schedule_appendix = _advisor_commitment_appendix(commitment_schedule)
        metrics.update(
            {
                "commitment_schedule": commitment_schedule,
                "card_installments_in_projection": decimal_value(
                    money(
                        sum(
                            (Decimal(str(row["installments"])) for row in forecast_data["rows"]),
                            Decimal("0"),
                        )
                    )
                ),
                "obligations_in_projection": decimal_value(
                    money(
                        sum(
                            (Decimal(str(row["obligations"])) for row in forecast_data["rows"]),
                            Decimal("0"),
                        )
                    )
                ),
            }
        )
        items = _obligation_rows(db, user.household_id)
        upcoming = [item for item in items if item["days_until_due"] >= 0][:5]
        if upcoming:
            lines = [
                f"{item['name']}: {_brl(item['amount'])} em {item['next_due_date']:%d/%m/%Y} "
                f"({item['alert_label'].lower()})"
                for item in upcoming
            ]
            answer = "Próximas obrigações cadastradas:\n- " + "\n- ".join(lines)
            evidence = lines
        else:
            answer = "Não encontrei obrigações futuras ativas cadastradas."
    elif any(word in normalized for word in ("ECONOMIZAR", "ECONOMIA", "CORTAR", "CORTE")):
        intent = "cuts"
        plan = cut_plan(month=selected_month, user=user, db=db)
        top = plan["recommendations"][:3]
        if top:
            lines = [
                f"{item['category']}: reduzir inicialmente {_brl(item['suggested_cut'])} por mês"
                for item in top
            ]
            answer = (
                f"A meta inicial conservadora é {_brl(plan['potential_monthly_savings'])} por mês.\n- "
                + "\n- ".join(lines)
            )
            evidence = lines
        else:
            answer = "Não há recomendação de corte calculada para o período selecionado."
    elif any(word in normalized for word in ("PRIVILEG", "RESERVA", "LIQUIDEZ", "INVESTIMENTO", "APLICACAO")):
        intent = "liquidity"
        available = Decimal(str(summary["liquidity_available"]))
        flow = Decimal(str(summary["liquidity_flow"]))
        starting = Decimal(str(summary["liquidity_starting_balance"]))
        closing = Decimal(str(summary["liquidity_closing_balance"]))
        withdrawal = Decimal(str(summary["liquidity_withdrawal"]))
        deposit = Decimal(str(summary["liquidity_deposit"]))
        uncovered = Decimal(str(summary["liquidity_uncovered_deficit"]))
        if uncovered > 0:
            answer = (
                f"O resultado negativo de {_brl(abs(flow))} consome todo o saldo informado "
                f"de {_brl(starting)} do {profile.investment_name}. O saldo estimado após o "
                f"fechamento fica em {_brl(closing)} e ainda restam {_brl(uncovered)} sem "
                "cobertura. O piso de segurança foi rompido; ele é uma meta de alerta, não "
                "dinheiro bloqueado."
            )
        elif withdrawal > 0:
            floor_note = (
                f"ainda {_brl(available)} acima do piso"
                if available >= 0
                else f"{_brl(abs(available))} abaixo do piso"
            )
            answer = (
                f"O déficit de {_brl(abs(flow))} é retirado do {profile.investment_name}. "
                f"Partindo do saldo informado de {_brl(starting)}, o saldo estimado após o "
                f"fechamento é {_brl(closing)}, ficando {floor_note}."
            )
        elif deposit > 0:
            answer = (
                f"A sobra de {_brl(deposit)} é destinada ao {profile.investment_name}. "
                f"Partindo do saldo informado de {_brl(starting)}, o saldo estimado após o "
                f"fechamento é {_brl(closing)}."
            )
        else:
            answer = (
                f"O mês está equilibrado e o saldo estimado do {profile.investment_name} "
                f"permanece em {_brl(closing)}."
            )
        answer += (
            " O cálculo usa o saldo informado no sistema e não consulta o Itaú em tempo real. "
            "Aplicações e resgates não são contados como renda ou gasto."
        )
        metrics.update(
            {
                "liquidity_name": profile.investment_name,
                "liquidity_starting_balance": decimal_value(starting),
                "liquidity_balance": decimal_value(closing),
                "liquidity_available": decimal_value(available),
                "liquidity_flow": decimal_value(flow),
                "liquidity_withdrawal": decimal_value(withdrawal),
                "liquidity_uncovered_deficit": decimal_value(uncovered),
            }
        )
        evidence = [
            f"Saldo informado inicial: {_brl(starting)}",
            f"Saldo estimado após fechamento: {_brl(closing)}",
            f"Piso de segurança: {_brl(profile.emergency_floor)}",
            f"Resultado operacional do mês: {_brl(flow)}",
            f"Retirada do Privilège: {_brl(withdrawal)}",
            f"Déficit sem cobertura: {_brl(uncovered)}",
        ]
    elif any(word in normalized for word in ("ENTROU", "RECEITA", "RECEBI", "ENTRADA")):
        intent = "cash_in"
        sources = [item for item in summary["cash_flow_by_account"] if item["cash_in"] > 0]
        detail = (
            "; ".join(f"{item['account']}: {_brl(item['cash_in'])}" for item in sources)
            or "nenhuma conta com receita identificada"
        )
        answer = (
            f"Entraram {_brl(summary['cash_in'])} em receitas reais em {selected_month}. "
            f"Resgates e estornos não entram nesse total. Detalhamento: {detail}."
        )
        evidence = [detail]
    elif any(word in normalized for word in ("SAIU", "BANCO", "CARTAO", "SAIDA")):
        intent = "cash_out"
        sources = [item for item in summary["cash_flow_by_account"] if item["cash_out"] > 0]
        detail = (
            "; ".join(f"{item['account']}: {_brl(item['cash_out'])}" for item in sources)
            or "nenhuma saída identificada"
        )
        answer = (
            f"Em {selected_month}, as saídas registradas nas contas bancárias somaram "
            f"{_brl(summary['bank_cash_out'])}; as compras nos cartões somaram "
            f"{_brl(summary['card_spending'])}. O total de despesas e compromissos foi "
            f"{_brl(summary['cash_out'])}. Aplicações, resgates, transferências internas e "
            f"pagamento de fatura foram excluídos para evitar dupla contagem. Detalhamento: {detail}."
        )
        evidence = [detail]
    elif any(word in normalized for word in ("GASTEI", "GASTO", "GASTOS")):
        intent = "spending"
        answer = (
            f"O gasto considerado no teto em {selected_month} é {_brl(summary['spending'])}. "
            f"O saldo do teto é {_brl(summary['remaining_cap'])}."
        )
        evidence = [
            f"Gasto consolidado: {_brl(summary['spending'])}",
            f"Teto restante: {_brl(summary['remaining_cap'])}",
        ]
    else:
        answer = (
            "Posso analisar uma compra, informar entradas e saídas por conta, listar obrigações "
            "próximas, mostrar o gasto do mês ou indicar os principais cortes. Por exemplo: "
            "‘Posso comprar algo de R$ 2.000 à vista?’"
        )

    provider = "local"
    provider_model = None
    if status_name != "insufficient_data" and intent != "help":
        codex_payload = {
            "question": payload.message,
            "conversation_history": [item.model_dump() for item in payload.history[-8:]],
            "selected_month": selected_month,
            "financial_summary": {
                "snapshot_id": summary["snapshot_id"],
                "snapshot_checksum": summary["snapshot_checksum"],
                "integrity_status": summary["integrity_status"],
                "spending": summary["spending"],
                "cash_in": summary["cash_in"],
                "cash_out": summary["cash_out"],
                "bank_cash_out": summary["bank_cash_out"],
                "card_spending": summary["card_spending"],
                "cash_cap": summary["cash_cap"],
                "remaining_cap": summary["remaining_cap"],
                "liquidity_name": summary["liquidity_name"],
                "liquidity_balance": summary["liquidity_balance"],
                "liquidity_available": summary["liquidity_available"],
                "liquidity_flow": summary["liquidity_flow"],
                "emergency_floor": summary["emergency_floor"],
                "review_items": summary["review_count"],
                "duplicates_ignored": summary["duplicates_ignored"],
                "top_categories": summary["category_spending"][:5],
                "forecast": forecast(user=user, db=db)["summary"],
            },
            "deterministic_result": {
                "intent": intent,
                "verdict": status_name,
                "answer": answer,
                "metrics": metrics,
                "evidence": evidence,
                "assumptions": assumptions,
            },
        }
        candidate = CodexAdvisorClient().analyze(codex_payload).payload
        candidate_verdict = candidate.get("verdict") if candidate else None
        # PR 6 / INV-021: the deterministic verdict computed above is final.
        # Codex may only *restate* it in nicer prose -- it can never move it,
        # not even to a more conservative one. Requiring an exact match
        # (rather than the previous "more conservative is allowed" order)
        # closes the exact gap flagged in
        # docs/INTEGRITY_IMPLEMENTATION_PLAN.md section 2.4/10.5: a Codex
        # response was previously able to override `status_name` whenever it
        # claimed a stricter verdict than the local engine, even though the
        # documented contract said the verdict "remains exactly the same".
        # `status_name` itself is never reassigned from `candidate` below.
        verdict_matches = candidate_verdict == status_name
        if (
            candidate
            and verdict_matches
            and isinstance(candidate.get("answer"), str)
            and candidate["answer"].strip()
        ):
            answer = candidate["answer"].strip()
            evidence = [str(item) for item in candidate.get("evidence", evidence)][:6]
            assumptions = [str(item) for item in candidate.get("assumptions", assumptions)][:6]
            provider = "codex"
            provider_model = candidate.get("model")

    appendices = [item for item in (reflection_appendix, schedule_appendix) if item]
    if appendices:
        answer = f"{answer.rstrip()}\n\n" + "\n\n".join(appendices)

    audit(
        db,
        user,
        "advisor.question",
        "financial_profile",
        profile.id,
        {"intent": intent, "status": status_name, "provider": provider, **metrics},
    )
    db.commit()
    return {
        "answer": answer,
        "status": status_name,
        "intent": intent,
        "metrics": metrics,
        "provider": provider,
        "model": provider_model,
        "evidence": evidence,
        "assumptions": assumptions,
        "snapshot_id": summary["snapshot_id"],
        "snapshot_checksum": summary["snapshot_checksum"],
        "integrity_status": summary["integrity_status"],
    }

# FAMILY_FINANCE_CURRENT_CONFIRMED_LIQUIDITY_V16_1
@router.get("/liquidity/current-confirmed")
def current_confirmed_liquidity(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = _privilege_account(db, user.household_id)
    observation = _latest_active_balance_observation(
        db,
        household_id=user.household_id,
        account_id=account.id,
    )
    if observation is None:
        raise HTTPException(
            status_code=404,
            detail="Saldo confirmado ativo do Privilège DI não encontrado.",
        )
    return {
        "account_id": account.id,
        "name": account.name,
        "amount": decimal_value(money(observation.amount)),
        "as_of_date": observation.as_of_date.isoformat(),
        "observation_id": observation.id,
    }