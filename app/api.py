import json
from datetime import UTC, date, datetime
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
    CommissionRequest,
    LoginRequest,
    ObligationRequest,
    PayrollRequest,
    ProfileRequest,
    SetupRequest,
    TransactionUpdate,
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
    return {"configured": True, "user": {"name": user.name, "username": user.username}}


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username.lower(), User.active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
    set_session_cookie(response, user.id)
    audit(db, user, "auth.login")
    db.commit()
    return {"name": user.name, "username": user.username}


@router.post("/auth/logout")
def logout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    clear_session_cookie(response)
    audit(db, user, "auth.logout")
    db.commit()
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "name": user.name, "username": user.username, "is_admin": user.is_admin}


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
        try:
            start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Mês deve usar o formato AAAA-MM") from exc
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
        }
        for item in rows
    ]


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


@router.get("/obligations")
def obligations(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(Obligation)
        .where(Obligation.household_id == user.household_id, Obligation.active.is_(True))
        .order_by(Obligation.due_date)
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "due_date": item.due_date,
            "amount": decimal_value(item.amount),
            "recurrence_months": item.recurrence_months,
            "occurrence_count": item.occurrence_count,
            "category": item.category,
        }
        for item in rows
    ]


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
    for item in rows:
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
            start_month=date.today().replace(day=1),
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
def cut_plan(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    end = add_months(date.today().replace(day=1), 1)
    start = add_months(end, -6)
    rows = db.execute(
        select(Transaction.booked_at, Transaction.amount, Category.name)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == user.household_id,
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
            Transaction.transaction_type.in_(("expense", "refund")),
            Transaction.excluded.is_(False),
        )
    ).all()
    covered = {(item.booked_at.year, item.booked_at.month) for item in rows}
    month_count = max(1, len(covered))
    totals: dict[str, Decimal] = {}
    for item in rows:
        totals[item.name] = totals.get(item.name, Decimal("0")) - Decimal(item.amount)
    categories_rows = db.scalars(
        select(Category).where(Category.household_id == user.household_id, Category.cash_cap.is_not(None))
    ).all()
    recommendations = []
    for category in categories_rows:
        average = money(max(Decimal("0"), totals.get(category.name, Decimal("0")) / month_count))
        target = money(category.cash_cap or 0)
        saving = money(max(Decimal("0"), average - target))
        if saving <= 0:
            continue
        if target == 0:
            rationale = "Pausar temporariamente e usar apenas VA/VR quando aplicável."
        elif category.essential:
            rationale = "Preservar o essencial e reduzir somente o excedente ao teto."
        else:
            rationale = "Definir limite semanal e interromper novas compras ao atingir o teto."
        recommendations.append(
            {
                "category": category.name,
                "average": decimal_value(average),
                "target": decimal_value(target),
                "suggested_cut": decimal_value(saving),
                "priority": "alta" if not category.essential or target == 0 else "média",
                "rationale": rationale,
            }
        )
    recommendations.sort(key=lambda item: (item["priority"] == "alta", item["suggested_cut"]), reverse=True)
    potential = money(sum((Decimal(str(item["suggested_cut"])) for item in recommendations), Decimal("0")))
    return {
        "window_start": start,
        "window_end": end,
        "covered_months": len(covered),
        "potential_monthly_savings": decimal_value(potential),
        "recommendations": recommendations,
    }


@router.get("/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    profile = profile_for(db, user.household_id)
    start = date.today().replace(day=1)
    end = add_months(start, 1)
    spending = db.scalar(
        select(func.coalesce(func.sum(-Transaction.amount), 0)).where(
            Transaction.household_id == user.household_id,
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
            Transaction.transaction_type.in_(("expense", "refund")),
            Transaction.excluded.is_(False),
        )
    )
    spending = max(Decimal("0"), Decimal(spending or 0))
    review_count = db.scalar(
        select(func.count(ReviewItem.id)).where(
            ReviewItem.household_id == user.household_id, ReviewItem.status == "open"
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
    return {
        "month": month_key(start),
        "spending": decimal_value(spending),
        "cash_cap": decimal_value(profile.monthly_cash_cap),
        "remaining_cap": decimal_value(max(Decimal("0"), profile.monthly_cash_cap - spending)),
        "investment_balance": decimal_value(profile.investment_balance),
        "emergency_floor": decimal_value(profile.emergency_floor),
        "food_benefits": decimal_value(benefit),
        "review_count": int(review_count or 0),
        "future_commissions_gross": decimal_value(future_commission),
    }
