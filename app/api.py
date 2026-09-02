import json
import re
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import get_db
from app.models import (
    Account,
    AccountBalanceObservation,
    AuditEvent,
    CaptureDraft,
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
    CommissionRequest,
    DuplicateResolutionRequest,
    IntegrityRunRequest,
    LoginRequest,
    ManualTransactionRequest,
    ObligationRequest,
    PayrollRequest,
    ProfileRequest,
    SetupRequest,
    TransactionUpdate,
    UserCreateRequest,
)
from app.security import (
    clear_session_cookie,
    get_current_user,
    hash_password,
    set_session_cookie,
    verify_password,
)
from app.services.classification_learning import (
    accept_classification_rule,
    classify_with_local_rules,
    record_confirmed_correction,
    serialize_classification_rule,
)
from app.services.classifier import normalize_description
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
    build_forecast,
    commission_net,
    money,
    month_key,
    monthly_net_rate,
)
from app.services.financial_integrity import (
    IntegrityCheck,
    IntegrityRunScope,
    IntegrityRunTrigger,
    build_baseline_checks,
    consolidated_integrity_status,
    execute_integrity_run,
    serialize_finding,
    serialize_run,
)
from app.services.financial_invariants import InvariantContext, InvariantScope
from app.services.financial_snapshots import (
    build_snapshot,
    liquidity_transition_facts,
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
from app.services.projection_engine import PROJECTION_CALCULATION_VERSION
from app.services.projection_validator import PROJECTION_TOLERANCE, validate_projection
from app.services.reconciliation import (
    persist_reconciliation,
    reconcile_parsed_document,
    serialize_reconciliation,
    unknown_reconciliation,
)
from app.services.smart_capture import CaptureParseError, preview_capture

router = APIRouter(prefix="/api")
settings = get_settings()

DEFAULT_CATEGORIES = (
    ("Conciliação", "#64748B", None, False),
    ("Transferência patrimonial", "#64748B", None, False),
    ("Transferência interna", "#64748B", None, False),
    ("Repasses a confirmar", "#F59E0B", None, False),
    ("Reembolsos e estornos", "#22C55E", None, False),
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
    period: object = transaction.booked_at
    if account_type == "credit_card":
        period = (transaction.booked_at.year, transaction.booked_at.month)
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
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
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
        if (
            transaction.transaction_type == "income"
            and amount > 0
            and not transaction.excluded
            and category_name == "Receitas"
        ):
            item["cash_in"] += amount
        elif transaction.transaction_type == "refund" and amount > 0:
            item["refunds"] += amount
        elif transaction.transaction_type in {"expense", "refund"} and amount < 0:
            item["gross_out"] += abs(amount)

    serialized = []
    for item in accounts.values():
        cash_in = money(item["cash_in"])
        refunds = money(item["refunds"])
        cash_out = money(max(Decimal("0"), item["gross_out"] - refunds))
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


def _obligation_rows(db: Session, household_id: str, reference: date | None = None) -> list[dict]:
    rows = db.scalars(
        select(Obligation)
        .where(Obligation.household_id == household_id, Obligation.active.is_(True))
        .order_by(Obligation.due_date)
    ).all()
    result = []
    for item in rows:
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
                **timing,
            }
        )
    result.sort(key=lambda item: item["next_due_date"])
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
    if installments == 1 or monthly_rate == 0:
        monthly_payment = money(purchase_amount / installments)
        total_cost = money(monthly_payment * installments)
    else:
        factor = Decimal("1") - (Decimal("1") + monthly_rate) ** (-installments)
        monthly_payment = money(purchase_amount * monthly_rate / factor)
        total_cost = money(monthly_payment * installments)
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
    set_session_cookie(response, user.id)
    return {
        "configured": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "username": user.username,
            "is_admin": user.is_admin,
        },
    }


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username.lower(), User.active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    set_session_cookie(response, user.id)
    audit(db, user, "auth.login")
    db.commit()
    return {
        "id": user.id,
        "name": user.name,
        "username": user.username,
        "is_admin": user.is_admin,
    }


@router.post("/auth/logout")
def logout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    clear_session_cookie(response)
    audit(db, user, "auth.logout")
    db.commit()
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "name": user.name, "username": user.username, "is_admin": user.is_admin}


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
            .order_by(IntegrityFinding.severity.desc(), IntegrityFinding.created_at)
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
    if status_filter:
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


@router.post("/imports", status_code=201)
async def import_document(
    account_id: str | None = Form(default=None),
    document_type: str = Form(..., pattern="^(bank_statement|credit_card|payroll)$"),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = None
    if account_id:
        account = db.scalar(
            select(Account).where(Account.id == account_id, Account.household_id == user.household_id)
        )
    if document_type != "payroll" and not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    payload = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(payload) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Arquivo maior que {settings.max_upload_mb} MB")
    digest = file_sha256(payload)
    if db.scalar(
        select(Document.id).where(Document.household_id == user.household_id, Document.sha256 == digest)
    ):
        raise HTTPException(status_code=409, detail="Este arquivo já foi importado")
    document = Document(
        household_id=user.household_id,
        account_id=account.id if account else None,
        original_name=file.filename or "documento",
        document_type=document_type,
        sha256=digest,
        encrypted_path="pending",
    )
    db.add(document)
    db.flush()
    document.encrypted_path = EncryptedDocumentStore().save(document.id, payload)
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


@router.post("/captures/preview", status_code=201)
async def create_capture_preview(
    text: str | None = Form(default=None),
    account_id: str | None = Form(default=None),
    document_type: str = Form(
        default="auto",
        pattern="^(auto|receipt|boleto|bank_statement|credit_card|payroll)$",
    ),
    file: UploadFile | None = File(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
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

    try:
        analysis = await run_in_threadpool(
            preview_capture,
            text=text,
            filename=filename,
            content_type=content_type,
            payload=payload,
            requested_type=document_type,
            account_type=account.account_type if account else None,
        )
    except CaptureParseError as exc:
        source_type = "text"
        if file:
            if (content_type or "").startswith("audio/"):
                source_type = "audio"
            elif (content_type or "").startswith("image/"):
                source_type = "image"
            else:
                source_type = "document"
        capture = CaptureDraft(
            household_id=user.household_id,
            user_id=user.id,
            document_id=document.id if document else None,
            source_type=source_type,
            detected_type=document_type,
            status="needs_input",
            processor="local_rules",
            original_text=(text or "")[:10000] or None,
            proposal_json="[]",
            confidence=Decimal("0"),
            notes=str(exc),
        )
        db.add(capture)
        if document:
            document.status = "capture_needs_input"
            document.notes = str(exc)
        db.flush()
        audit(
            db,
            user,
            "capture.needs_input",
            "capture_draft",
            capture.id,
            {"source_type": source_type, "reason": str(exc)},
        )
        db.commit()
        return _capture_response(capture)

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
    capture = CaptureDraft(
        household_id=user.household_id,
        user_id=user.id,
        document_id=document.id if document else None,
        source_type=analysis["source_type"],
        detected_type=analysis["detected_type"],
        status="preview",
        processor=analysis["processor"],
        original_text=(text or "")[:10000] or None,
        extracted_text=analysis.get("extracted_text") or None,
        proposal_json=json.dumps(proposals, ensure_ascii=False),
        confidence=Decimal(str(analysis["confidence"])),
        notes="; ".join(dict.fromkeys(warnings))[:4000] or None,
    )
    db.add(capture)
    if document:
        document.document_type = (
            analysis["detected_type"]
            if analysis["detected_type"] in {"bank_statement", "credit_card", "payroll"}
            else "smart_capture"
        )
        document.status = "capture_preview"
    db.flush()
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
    audit(db, user, "capture.cancel", "capture_draft", item.id)
    db.commit()
    return {"ok": True}


@router.post("/captures/{capture_id}/confirm")
def confirm_capture(
    capture_id: str,
    payload: CaptureConfirmRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
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
            investment_movement: str | None = None
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
                category = category_for(db, user.household_id, "Receitas")
            elif movement_type in {"investment", "redemption"}:
                transaction_type = "transfer"
                excluded = True
                category = category_for(db, user.household_id, "Transferência patrimonial")
                investment_movement = movement_type
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
                competence=proposal.booked_at.strftime("%Y-%m"),
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
            transaction.reviewed = not duplicate
            if investment_movement and not duplicate:
                profile = profile_for(db, user.household_id)
                if investment_movement == "investment":
                    profile.investment_balance += abs(proposal.amount)
                else:
                    profile.investment_balance = max(
                        Decimal("0"), profile.investment_balance - abs(proposal.amount)
                    )
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


@router.get("/transactions")
def transactions(
    month: str | None = None,
    review_only: bool = False,
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Transaction).where(Transaction.household_id == user.household_id)
    if month:
        start = _month_start(month)
        query = query.where(Transaction.booked_at >= start, Transaction.booked_at < add_months(start, 1))
    if review_only:
        query = query.where(Transaction.reviewed.is_(False))
    rows = db.scalars(
        query.order_by(Transaction.booked_at.desc(), Transaction.created_at.desc()).limit(min(limit, 1000))
    ).all()
    return [
        {
            "id": item.id,
            "date": item.booked_at,
            "description": item.description,
            "amount": decimal_value(item.amount),
            "type": item.transaction_type,
            "category_id": item.category_id,
            "category": item.category.name if item.category else "Revisar",
            "owner": item.owner_label,
            "account": item.account.name if item.account else "",
            "excluded": item.excluded,
            "possible_duplicate": item.possible_duplicate,
            "reviewed": item.reviewed,
            "confidence": decimal_value(item.confidence),
            "installment": (
                f"{item.installment_current}/{item.installment_total}"
                if item.installment_current and item.installment_total
                else None
            ),
            "manual": item.document_id is None,
        }
        for item in rows
    ]


@router.post("/transactions", status_code=201)
def create_manual_transaction(
    payload: ManualTransactionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
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
        profile = profile_for(db, user.household_id)
        profile.investment_balance += abs(payload.amount)
    elif payload.movement_type == "redemption":
        transaction_type = "transfer"
        amount = abs(payload.amount)
        excluded = True
        category = category_for(db, user.household_id, "Transferência patrimonial")
        profile = profile_for(db, user.household_id)
        profile.investment_balance = max(Decimal("0"), profile.investment_balance - abs(payload.amount))
    elif payload.movement_type == "refund":
        transaction_type = "refund"
        amount = abs(payload.amount)
        category = category_for(db, user.household_id, "Reembolsos e estornos")

    parsed = ParsedTransaction(
        booked_at=payload.booked_at,
        description=payload.description,
        amount=money(amount),
        source_line=1,
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
        fingerprint=transaction_fingerprint(account.id, parsed, account.owner_label),
        occurred_at=payload.booked_at,
        competence=payload.booked_at.strftime("%Y-%m"),
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
        },
    )
    db.commit()
    return {"id": transaction.id}


@router.patch("/transactions/{transaction_id}")
def update_transaction(
    transaction_id: str,
    payload: TransactionUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
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


@router.delete("/transactions/{transaction_id}")
def delete_manual_transaction(
    transaction_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    transaction = db.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.household_id == user.household_id,
        )
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado")
    if transaction.document_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Lançamentos importados não são apagados; use Ignorar para preservar a auditoria",
        )
    audit(
        db,
        user,
        "transaction.delete_manual",
        "transaction",
        transaction.id,
        {"description": transaction.description, "amount": str(transaction.amount)},
    )
    if (
        transaction.transaction_type == "transfer"
        and transaction.category
        and transaction.category.name == "Transferência patrimonial"
    ):
        profile = profile_for(db, user.household_id)
        if transaction.amount < 0:
            profile.investment_balance = max(
                Decimal("0"), profile.investment_balance - abs(transaction.amount)
            )
        elif transaction.amount > 0:
            profile.investment_balance += transaction.amount
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
        after_state={"status": rule.status, "active": rule.active},
        reason="Aceite explícito de regra após três correções consistentes",
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


@router.get("/obligations")
def obligations(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    return _obligation_rows(db, user.household_id)


@router.post("/obligations", status_code=201)
def create_obligation(
    payload: ObligationRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
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
    item = db.scalar(
        select(Obligation).where(
            Obligation.id == obligation_id,
            Obligation.household_id == user.household_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Compromisso não encontrado")
    item.active = False
    audit(db, user, "obligation.deactivate", "obligation", item.id, {"name": item.name})
    db.commit()
    return {"ok": True}


@router.get("/profile")
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    item = profile_for(db, user.household_id)
    db.commit()
    return {
        "monthly_salary_net": decimal_value(item.monthly_salary_net),
        "monthly_cash_cap": decimal_value(item.monthly_cash_cap),
        "emergency_floor": decimal_value(item.emergency_floor),
        "food_allowance": decimal_value(item.food_allowance),
        "meal_allowance_daily": decimal_value(item.meal_allowance_daily),
        "workdays_month": item.workdays_month,
        "investment_name": item.investment_name,
        "investment_balance": decimal_value(item.investment_balance),
        "investment_gross_annual_rate": decimal_value(item.investment_gross_annual_rate),
        "investment_income_tax_rate": decimal_value(item.investment_income_tax_rate),
        "projection_end": item.projection_end,
    }


@router.put("/profile")
def update_profile(
    payload: ProfileRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    item = profile_for(db, user.household_id)
    for field, value in payload.model_dump().items():
        setattr(item, field, value)
    audit(db, user, "profile.update", "financial_profile", item.id, payload.model_dump())
    db.commit()
    return {"ok": True}


def _forecast_obligations(items: list[Obligation]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for item in items:
        for occurrence in range(item.occurrence_count):
            due = add_months(item.due_date.replace(day=1), occurrence * item.recurrence_months)
            key = month_key(due)
            values[key] = values.get(key, Decimal("0")) + item.amount
            if item.recurrence_months == 0:
                break
    return values


def _future_installments(db: Session, household_id: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    rows = db.scalars(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.excluded.is_(False),
            Transaction.installment_current.is_not(None),
            Transaction.installment_total.is_not(None),
        )
    ).all()
    latest_by_series: dict[tuple, Transaction] = {}
    for item in rows:
        current = item.installment_current or 0
        total = item.installment_total or 0
        description = re.sub(
            r"(?:PARCELA\s*)?\d{1,2}\s*/\s*\d{1,2}",
            " ",
            item.description,
            flags=re.IGNORECASE,
        )
        origin = add_months(item.booked_at.replace(day=1), -(max(1, current) - 1))
        series = (
            item.account_id,
            item.card_last_four or "",
            normalize_description(description),
            money(abs(item.amount)),
            total,
            month_key(origin),
        )
        previous = latest_by_series.get(series)
        if previous is None or (item.booked_at, current) > (
            previous.booked_at,
            previous.installment_current or 0,
        ):
            latest_by_series[series] = item
    for item in latest_by_series.values():
        remaining = max(0, (item.installment_total or 0) - (item.installment_current or 0))
        for offset in range(1, remaining + 1):
            key = month_key(add_months(item.booked_at.replace(day=1), offset))
            values[key] = values.get(key, Decimal("0")) + abs(item.amount)
    return values


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
    obligations_rows = db.scalars(
        select(Obligation).where(Obligation.household_id == user.household_id, Obligation.active.is_(True))
    ).all()
    commission_rows = db.scalars(
        select(Commission).where(
            Commission.household_id == user.household_id, Commission.status != "cancelled"
        )
    ).all()
    payroll_rows = db.scalars(
        select(PayrollRecord).where(
            PayrollRecord.household_id == user.household_id, PayrollRecord.payroll_kind != "regular"
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
    projection_input = ForecastInput(
        start_month=add_months(date.today().replace(day=1), 1),
        end_month=end,
        starting_balance=Decimal(current_snapshot.closing_liquidity_balance),
        monthly_salary=profile.monthly_salary_net,
        monthly_cash_cap=profile.monthly_cash_cap,
        monthly_investment_rate=rate,
        obligations=_forecast_obligations(list(obligations_rows)),
        installments=_future_installments(db, user.household_id),
        payroll_extras=payroll_extras,
        commissions=tuple(commissions_input),
        starting_uncovered_deficit=Decimal(current_snapshot.closing_uncovered_deficit),
        safety_floor=Decimal(profile.emergency_floor),
    )
    rows = build_forecast(projection_input)
    validation = validate_projection(projection_input, rows)
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
    liquidity_facts = liquidity_transition_facts(current_snapshot)
    lineage_facts = snapshot_lineage_facts(db, current_snapshot)
    integrity_run, gate_results = execute_integrity_run(
        db,
        household_id=user.household_id,
        scope=IntegrityRunScope.PROJECTION,
        trigger=IntegrityRunTrigger.SYSTEM,
        checks=(
            IntegrityCheck(
                "INV-018",
                InvariantContext(
                    facts={
                        "financial_engine_values": validation.actual_values,
                        "projection_validator_values": validation.expected_values,
                        "monetary_tolerance": PROJECTION_TOLERANCE,
                    },
                    scope=InvariantScope.PROJECTION,
                    entity_type="projection",
                    entity_id=projection_identity,
                    period=current_period,
                ),
            ),
            IntegrityCheck(
                "INV-005",
                InvariantContext(
                    facts=liquidity_facts,
                    scope=InvariantScope.PROJECTION,
                    entity_type="projection",
                    entity_id=projection_identity,
                    period=current_period,
                ),
            ),
            IntegrityCheck(
                "INV-006",
                InvariantContext(
                    facts=liquidity_facts,
                    scope=InvariantScope.PROJECTION,
                    entity_type="projection",
                    entity_id=projection_identity,
                    period=current_period,
                ),
            ),
            IntegrityCheck(
                "INV-022",
                InvariantContext(
                    facts=lineage_facts,
                    scope=InvariantScope.PROJECTION,
                    entity_type="projection",
                    entity_id=projection_identity,
                    period=current_period,
                ),
            ),
        ),
        created_by=user.id,
        period=current_period,
        scope_entity_type="projection",
        scope_entity_id=projection_identity,
        calculation_version=PROJECTION_CALCULATION_VERSION,
    )
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
    future_commission = db.scalar(
        select(func.coalesce(func.sum(Commission.gross_amount), 0)).where(
            Commission.household_id == user.household_id,
            Commission.expected_date >= start,
            Commission.status != "cancelled",
        )
    )
    benefit = profile.food_allowance + profile.meal_allowance_daily * profile.workdays_month
    obligation_alerts = [
        item for item in _obligation_rows(db, user.household_id) if item["days_until_due"] <= 30
    ][:5]
    db.commit()
    cash_net = Decimal(snapshot.operating_result)
    return {
        "month": month_key(start),
        "snapshot_id": snapshot.id,
        "snapshot_checksum": snapshot.checksum,
        "integrity_status": snapshot.integrity_status,
        "trusted_for_reports": snapshot.trusted_for_reports,
        "spending": decimal_value(snapshot.operating_expenses),
        "cash_in": decimal_value(snapshot.operating_income),
        "cash_out": decimal_value(snapshot.operating_expenses),
        "bank_cash_out": decimal_value(snapshot.bank_cash_out),
        "card_spending": decimal_value(snapshot.card_spend),
        "cash_net": decimal_value(cash_net),
        "cash_flow_by_account": snapshot_payload["cash_flow_by_account"],
        "cash_cap": decimal_value(snapshot.budget_cap),
        "remaining_cap": decimal_value(snapshot.budget_remaining),
        "investment_balance": decimal_value(profile.investment_balance),
        "liquidity_name": profile.investment_name,
        "liquidity_starting_balance": decimal_value(snapshot.opening_liquidity_balance),
        "liquidity_balance": decimal_value(snapshot.closing_liquidity_balance),
        "liquidity_closing_balance": decimal_value(snapshot.closing_liquidity_balance),
        "liquidity_available": decimal_value(snapshot.distance_to_floor),
        "liquidity_deposit": decimal_value(snapshot_payload["liquidity_deposit"]),
        "liquidity_withdrawal": decimal_value(snapshot.liquidity_used),
        "liquidity_uncovered_deficit": decimal_value(snapshot.closing_uncovered_deficit),
        "liquidity_flow": decimal_value(cash_net),
        "liquidity_direction": ("deposit" if cash_net > 0 else "withdrawal" if cash_net < 0 else "balanced"),
        "emergency_floor": decimal_value(profile.emergency_floor),
        "food_benefits": decimal_value(benefit),
        "review_count": int(review_count or 0),
        "duplicates_ignored": snapshot_payload["duplicates_ignored"],
        "category_spending": snapshot_payload["category_spending"],
        "future_commissions_gross": decimal_value(future_commission),
        "obligation_alerts": obligation_alerts,
    }


@router.get("/reports")
def reports(
    end_month: str | None = None,
    months: int = 6,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Build an auditable comparison for one to twelve consecutive months."""
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
        for category in snapshot.payload.get("category_spending", []):
            name = str(category["category"])
            category_totals[name] = category_totals.get(name, Decimal("0")) + Decimal(
                str(category["amount"])
            )
        spending = money(snapshot.operating_expenses)
        cash_in = money(snapshot.operating_income)
        cash_out = money(snapshot.operating_expenses)
        bank_cash_out = money(snapshot.bank_cash_out)
        card_spending = money(snapshot.card_spend)
        cash_net = money(snapshot.operating_result)
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
                "spending": decimal_value(spending),
                "cash_in": decimal_value(cash_in),
                "cash_out": decimal_value(cash_out),
                "bank_cash_out": decimal_value(bank_cash_out),
                "card_spending": decimal_value(card_spending),
                "cash_net": decimal_value(cash_net),
                "cash_cap": decimal_value(profile.monthly_cash_cap),
                "remaining_cap": decimal_value(money(profile.monthly_cash_cap - spending)),
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
    categories = [
        {
            "category": name,
            "amount": decimal_value(money(amount)),
            "average": decimal_value(money(amount / average_denominator)),
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
    savings_rate = (
        money(((total_cash_in - total_cash_out) / total_cash_in) * Decimal("100"))
        if total_cash_in > 0
        else Decimal("0")
    )
    account_totals: dict[str, dict[str, object]] = {}
    for snapshot in report_snapshots:
        for account in snapshot.payload.get("cash_flow_by_account", []):
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
            "total_spending": decimal_value(total_spending),
            "average_spending": decimal_value(average_spending),
            "total_cash_in": decimal_value(total_cash_in),
            "total_cash_out": decimal_value(total_cash_out),
            "total_bank_cash_out": decimal_value(total_bank_cash_out),
            "total_card_spending": decimal_value(total_card_spending),
            "cash_net": decimal_value(total_result),
            "liquidity_name": profile.investment_name,
            "liquidity_starting_balance": decimal_value(first_snapshot.opening_liquidity_balance),
            "liquidity_balance": decimal_value(last_snapshot.closing_liquidity_balance),
            "liquidity_closing_balance": decimal_value(last_snapshot.closing_liquidity_balance),
            "emergency_floor": decimal_value(profile.emergency_floor),
            "liquidity_available": decimal_value(last_snapshot.distance_to_floor),
            "liquidity_deposit": decimal_value(total_liquidity_deposit),
            "liquidity_withdrawal": decimal_value(total_liquidity_used),
            "liquidity_uncovered_deficit": decimal_value(
                last_snapshot.closing_uncovered_deficit
            ),
            "liquidity_flow": decimal_value(total_result),
            "liquidity_direction": (
                "deposit"
                if total_cash_in > total_cash_out
                else "withdrawal"
                if total_cash_out > total_cash_in
                else "balanced"
            ),
            "savings_rate": decimal_value(savings_rate),
            "highest_month": highest_month["month"],
            "highest_spending": highest_month["spending"],
            "lowest_month": lowest_month["month"],
            "lowest_spending": lowest_month["spending"],
            "last_change_percentage": last_change,
        },
        "monthly": serialized_months,
        "categories": categories,
        "accounts": report_accounts,
    }


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
        verdict_order = {"favorable": 0, "caution": 1, "not_recommended": 2}
        candidate_verdict = candidate.get("verdict") if candidate else None
        verdict_allowed = candidate_verdict == status_name
        if status_name in verdict_order and candidate_verdict in verdict_order:
            verdict_allowed = verdict_order[candidate_verdict] >= verdict_order[status_name]
        if (
            candidate
            and verdict_allowed
            and isinstance(candidate.get("answer"), str)
            and candidate["answer"].strip()
        ):
            status_name = str(candidate_verdict)
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
