import json
import re
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    Account,
    AuditEvent,
    Category,
    Commission,
    Document,
    FinancialProfile,
    Household,
    Obligation,
    PayrollRecord,
    ReviewItem,
    Transaction,
    User,
)
from app.schemas import (
    AccountRequest,
    AdvisorRequest,
    CommissionRequest,
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
from app.services.classifier import classify, normalize_description
from app.services.crypto import EncryptedDocumentStore
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
from app.services.importer import (
    file_sha256,
    parse_document,
    parse_payroll_pdf,
    transaction_fingerprint,
)

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
    }
    result: list[tuple[Transaction, str]] = []
    ignored: list[Transaction] = []
    for transaction, category_name, account_type, document_type in raw_rows:
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
    """Separate actual income/outgoings from patrimonial transfers and reconciliations."""
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
        if transaction.transaction_type == "income" and amount > 0:
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
                "refunds": decimal_value(refunds),
                "net": decimal_value(money(cash_in - cash_out)),
            }
        )
    serialized.sort(key=lambda item: (item["account_type"], item["account"]))
    return {
        "cash_in": decimal_value(
            money(sum((Decimal(str(item["cash_in"])) for item in serialized), Decimal("0")))
        ),
        "cash_out": decimal_value(
            money(sum((Decimal(str(item["cash_out"])) for item in serialized), Decimal("0")))
        ),
        "accounts": serialized,
    }


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
    number = r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    patterns = (
        rf"(?:custa|custando|valor(?:\s+de)?|compra(?:\s+de)?|pagar)\s*(?:r\$\s*)?{number}\s*(mil|k)?",
        rf"r\$\s*{number}\s*(mil|k)?",
        rf"{number}\s*(mil|k)\b",
    )
    match = next((candidate for pattern in patterns if (candidate := re.search(pattern, lowered))), None)
    if not match:
        return None
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
) -> None:
    db.add(
        AuditEvent(
            household_id=user.household_id,
            user_id=user.id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
        )
    )


def category_for(db: Session, household_id: str, name: str) -> Category:
    category = db.scalar(select(Category).where(Category.household_id == household_id, Category.name == name))
    if category:
        return category
    category = Category(household_id=household_id, name=name)
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


@router.get("/users")
def users(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    _require_admin(user)
    rows = db.scalars(
        select(User).where(User.household_id == user.household_id).order_by(User.name)
    ).all()
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
    item = db.scalar(
        select(User).where(User.id == user_id, User.household_id == user.household_id)
    )
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
            payroll = parse_payroll_pdf(payload)
        except ValueError as exc:
            document.status = "review_required"
            document.notes = str(exc)
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
        document.status = "imported"
        document.record_count = 1
        audit(db, user, "document.import", "document", document.id, {"records": 1})
        db.commit()
        return {"document_id": document.id, "status": document.status, "records": 1, "review_items": 0}

    try:
        parsed = parse_document(document.original_name, payload, document_type)
    except ValueError as exc:
        document.status = "review_required"
        document.notes = str(exc)
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
        return {"document_id": document.id, "status": document.status, "records": 0, "message": str(exc)}

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
        duplicate = bool(
            db.scalar(
                select(Transaction.id).where(
                    Transaction.household_id == user.household_id,
                    Transaction.fingerprint == fingerprint,
                )
            )
        )
        classification = classify(item.description, float(item.amount), internal_aliases)
        category = category_for(db, user.household_id, classification.category)
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
            confidence=Decimal(str(classification.confidence)),
            excluded=classification.excluded or duplicate,
            possible_duplicate=duplicate,
        )
        db.add(transaction)
        db.flush()
        if duplicate or classification.review_reason:
            reason = "possible_duplicate" if duplicate else "classification_low_confidence"
            details = (
                "Possível repetição de período já importado" if duplicate else classification.review_reason
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
    document.status = "imported_with_review" if review_count else "imported"
    document.record_count = imported
    audit(
        db,
        user,
        "document.import",
        "document",
        document.id,
        {"records": imported, "review_items": review_count, "sha256": digest},
    )
    db.commit()
    return {
        "document_id": document.id,
        "status": document.status,
        "records": imported,
        "review_items": review_count,
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
        profile.investment_balance = max(
            Decimal("0"), profile.investment_balance - abs(payload.amount)
        )
    elif payload.movement_type == "refund":
        transaction_type = "refund"
        amount = abs(payload.amount)
        category = category_for(db, user.household_id, "Reembolsos e estornos")

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
        fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        confidence=Decimal("1"),
        excluded=excluded,
        reviewed=True,
    )
    db.add(transaction)
    db.flush()
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
    audit(db, user, "transaction.update", "transaction", transaction.id, changes)
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
            "account": (
                _account_display(item.transaction.account) if item.transaction else ""
            ),
            "excluded": item.transaction.excluded if item.transaction else None,
            "possible_duplicate": (
                item.transaction.possible_duplicate if item.transaction else False
            ),
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
    rows = build_forecast(
        ForecastInput(
            start_month=add_months(date.today().replace(day=1), 1),
            end_month=end,
            starting_balance=profile.investment_balance,
            monthly_salary=profile.monthly_salary_net,
            monthly_cash_cap=profile.monthly_cash_cap,
            monthly_investment_rate=rate,
            obligations=_forecast_obligations(list(obligations_rows)),
            installments=_future_installments(db, user.household_id),
            payroll_extras=payroll_extras,
            commissions=tuple(commissions_input),
        )
    )
    serialized = [
        {key: decimal_value(value) if isinstance(value, Decimal) else value for key, value in row.items()}
        for row in rows
    ]
    delayed_balances = [row["balance_delayed"] for row in rows] or [0]
    return {
        "monthly_net_investment_rate": float(rate),
        "rows": serialized,
        "summary": {
            "final_delayed": delayed_balances[-1],
            "minimum_delayed": min(delayed_balances),
            "emergency_floor": decimal_value(profile.emergency_floor),
            "viable": min(delayed_balances) >= decimal_value(profile.emergency_floor),
        },
    }


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
            sum((-Decimal(transaction.amount) for transaction, _ in rows), Decimal("0"))
            / month_count,
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


@router.get("/dashboard")
def dashboard(
    month: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    profile = profile_for(db, user.household_id)
    start = _month_start(month)
    end = add_months(start, 1)
    expense_rows, duplicates_ignored = _consolidated_expenses(
        db,
        user.household_id,
        start,
        end,
    )
    spending = max(
        Decimal("0"),
        sum((-Decimal(transaction.amount) for transaction, _ in expense_rows), Decimal("0")),
    )
    movement_rows, _ignored_movements = _consolidated_transactions(
        db,
        user.household_id,
        start,
        end,
    )
    cash_flow = _operational_cash_flow(movement_rows)
    category_totals: dict[str, Decimal] = {}
    for transaction, category_name in expense_rows:
        category_totals[category_name] = category_totals.get(category_name, Decimal("0")) - Decimal(
            transaction.amount
        )
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
        item
        for item in _obligation_rows(db, user.household_id)
        if item["days_until_due"] <= 30
    ][:5]
    return {
        "month": month_key(start),
        "spending": decimal_value(spending),
        "cash_in": cash_flow["cash_in"],
        "cash_out": cash_flow["cash_out"],
        "cash_net": decimal_value(
            money(Decimal(str(cash_flow["cash_in"])) - Decimal(str(cash_flow["cash_out"])))
        ),
        "cash_flow_by_account": cash_flow["accounts"],
        "cash_cap": decimal_value(profile.monthly_cash_cap),
        "remaining_cap": decimal_value(money(profile.monthly_cash_cap - spending)),
        "investment_balance": decimal_value(profile.investment_balance),
        "emergency_floor": decimal_value(profile.emergency_floor),
        "food_benefits": decimal_value(benefit),
        "review_count": int(review_count or 0),
        "duplicates_ignored": duplicates_ignored,
        "category_spending": [
            {"category": name, "amount": decimal_value(max(Decimal("0"), amount))}
            for name, amount in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
            if amount > 0
        ],
        "future_commissions_gross": decimal_value(future_commission),
        "obligation_alerts": obligation_alerts,
    }


@router.post("/advisor/chat")
def advisor_chat(
    payload: AdvisorRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    target_month = _advisor_month(payload.message)
    selected_month = month_key(target_month)
    normalized = normalize_description(payload.message)
    summary = dashboard(month=selected_month, user=user, db=db)
    profile = profile_for(db, user.household_id)
    intent = "help"
    status_name = "informative"
    metrics: dict[str, object] = {"month": selected_month}

    purchase_words = {"COMPRA", "COMPRAR", "CUSTA", "POSSO", "ADQUIRIR"}
    if any(word in normalized.split() for word in purchase_words):
        intent = "purchase"
        purchase_amount = _advisor_amount(payload.message)
        if purchase_amount is None:
            answer = (
                "Informe o valor da compra para eu analisar. Exemplo: "
                "‘Quero comprar algo de R$ 2.000; posso fazer essa compra?’"
            )
            status_name = "insufficient_data"
        elif Decimal(str(summary["cash_cap"])) <= 0:
            answer = (
                f"Ainda não consigo avaliar a compra de {_brl(purchase_amount)} porque o teto "
                "mensal não está configurado. Preencha o perfil financeiro primeiro."
            )
            status_name = "insufficient_data"
        else:
            forecast_data = forecast(user=user, db=db)
            remaining_before = Decimal(str(summary["remaining_cap"]))
            remaining_after = money(remaining_before - purchase_amount)
            minimum_projected = Decimal(str(forecast_data["summary"]["minimum_delayed"]))
            floor = Decimal(str(forecast_data["summary"]["emergency_floor"]))
            projection_margin = money(minimum_projected - floor)

            today = date.today()
            horizon = today + timedelta(days=180)
            obligation_models = db.scalars(
                select(Obligation).where(
                    Obligation.household_id == user.household_id,
                    Obligation.active.is_(True),
                )
            ).all()
            occurrences = sorted(
                (due, item)
                for item in obligation_models
                for due in _obligation_dates(item)
                if today <= due <= horizon
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

            if remaining_after < 0:
                status_name = "not_recommended"
                answer = (
                    f"Minha recomendação é não fazer essa compra agora. O valor de "
                    f"{_brl(purchase_amount)} ultrapassaria o teto disponível em "
                    f"{_brl(abs(remaining_after))}. Neste mês você já gastou "
                    f"{_brl(summary['spending'])} de um teto de {_brl(summary['cash_cap'])}."
                    f"{next_note}"
                )
            elif not forecast_data["summary"]["viable"]:
                status_name = "not_recommended"
                answer = (
                    f"Embora a compra de {_brl(purchase_amount)} caiba no teto deste mês, "
                    "eu não a recomendo agora: a projeção conservadora já fica abaixo da "
                    f"reserva mínima em {_brl(abs(projection_margin))}. Restariam "
                    f"{_brl(remaining_after)} no teto após a compra.{next_note}"
                )
            elif purchase_amount > remaining_before * Decimal("0.50") or projection_margin < purchase_amount:
                status_name = "caution"
                answer = (
                    f"A compra de {_brl(purchase_amount)} cabe matematicamente, mas exige "
                    f"cautela: restariam {_brl(remaining_after)} no teto do mês e a margem "
                    f"mínima projetada acima da reserva é {_brl(max(Decimal('0'), projection_margin))}. "
                    f"Eu só faria se for necessária e sem usar a reserva investida.{next_note}"
                )
            else:
                status_name = "favorable"
                answer = (
                    f"Sim, a compra de {_brl(purchase_amount)} cabe no cenário atual, "
                    f"considerando pagamento à vista: restariam {_brl(remaining_after)} no "
                    f"teto do mês. A projeção conservadora continua acima da reserva mínima "
                    f"por {_brl(projection_margin)}.{next_note}"
                )
            answer += " A análise não considera juros de parcelamento nem gastos ainda não lançados."
            metrics.update(
                {
                    "purchase_amount": decimal_value(purchase_amount),
                    "remaining_before": decimal_value(remaining_before),
                    "remaining_after": decimal_value(remaining_after),
                    "projection_margin": decimal_value(projection_margin),
                    "obligations_next_180_days": decimal_value(upcoming_total),
                }
            )
    elif any(word in normalized for word in ("OBRIGAC", "VENCIMENTO", "VENCE", "PARCELA")):
        intent = "obligations"
        items = _obligation_rows(db, user.household_id)
        upcoming = [item for item in items if item["days_until_due"] >= 0][:5]
        if upcoming:
            lines = [
                f"{item['name']}: {_brl(item['amount'])} em {item['next_due_date']:%d/%m/%Y} "
                f"({item['alert_label'].lower()})"
                for item in upcoming
            ]
            answer = "Próximas obrigações cadastradas:\n- " + "\n- ".join(lines)
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
        else:
            answer = "Não há recomendação de corte calculada para o período selecionado."
    elif any(word in normalized for word in ("ENTROU", "RECEITA", "RECEBI", "ENTRADA")):
        intent = "cash_in"
        sources = [item for item in summary["cash_flow_by_account"] if item["cash_in"] > 0]
        detail = "; ".join(
            f"{item['account']}: {_brl(item['cash_in'])}" for item in sources
        ) or "nenhuma conta com receita identificada"
        answer = (
            f"Entraram {_brl(summary['cash_in'])} em receitas reais em {selected_month}. "
            f"Resgates e estornos não entram nesse total. Detalhamento: {detail}."
        )
    elif any(word in normalized for word in ("SAIU", "BANCO", "CARTAO", "SAIDA")):
        intent = "cash_out"
        sources = [item for item in summary["cash_flow_by_account"] if item["cash_out"] > 0]
        detail = "; ".join(
            f"{item['account']}: {_brl(item['cash_out'])}" for item in sources
        ) or "nenhuma saída identificada"
        answer = (
            f"Saíram {_brl(summary['cash_out'])} em despesas e pagamentos em {selected_month}. "
            f"Aplicações, resgates, transferências internas e pagamento de fatura foram excluídos. "
            f"Detalhamento: {detail}."
        )
    elif any(word in normalized for word in ("GASTEI", "GASTO", "GASTOS")):
        intent = "spending"
        answer = (
            f"O gasto considerado no teto em {selected_month} é {_brl(summary['spending'])}. "
            f"O saldo do teto é {_brl(summary['remaining_cap'])}."
        )
    else:
        answer = (
            "Posso analisar uma compra, informar entradas e saídas por conta, listar obrigações "
            "próximas, mostrar o gasto do mês ou indicar os principais cortes. Por exemplo: "
            "‘Posso comprar algo de R$ 2.000?’"
        )

    audit(
        db,
        user,
        "advisor.question",
        "financial_profile",
        profile.id,
        {"intent": intent, "status": status_name, **metrics},
    )
    db.commit()
    return {
        "answer": answer,
        "status": status_name,
        "intent": intent,
        "metrics": metrics,
        "assumptions": [
            "Compras são avaliadas à vista no mês selecionado",
            "A reserva investida não é tratada como renda disponível",
            "Dados ainda não lançados não entram na análise",
        ],
    }
