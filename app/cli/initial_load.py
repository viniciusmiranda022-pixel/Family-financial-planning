"""Safe local bootstrap for a clean household database.

Real manifests and documents stay outside Git. Financial documents are applied
through the exact `/api/imports` implementation used by the UI so parsing,
classification, duplicate discovery, reconciliation, encryption and audit do
not acquire a second source of truth.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import import_document
from app.db import SessionLocal
from app.models import (
    Account,
    AccountBalanceObservation,
    AuditEvent,
    Document,
    FinancialProfile,
    Obligation,
    User,
)
from app.services.importer import file_sha256, parse_document_contract, parse_payroll_document
from app.services.plan_workbook import select_household
from app.services.reconciliation import reconcile_parsed_document


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountSeed(StrictModel):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    institution: str = Field(default="", max_length=80)
    account_type: Literal["checking", "credit_card", "investment", "cash"] = "checking"
    owner_label: str = Field(default="Família", max_length=80)
    last_four: str | None = Field(default=None, pattern=r"^\d{4}$")


class ProfileSeed(StrictModel):
    monthly_salary_net: Decimal | None = Field(default=None, ge=0)
    monthly_cash_cap: Decimal | None = Field(default=None, ge=0)
    emergency_floor: Decimal | None = Field(default=None, ge=0)
    food_allowance: Decimal | None = Field(default=None, ge=0)
    meal_allowance_daily: Decimal | None = Field(default=None, ge=0)
    workdays_month: int | None = Field(default=None, ge=0, le=31)
    # `investment_balance` is intentionally NOT a field here. It is the
    # mutable, non-reconciled counter `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
    # (section 1, row "Saldo do Privilège") identifies as the project's
    # single riskiest source of financial drift, slated for deprecation in
    # favor of `AccountBalanceObservation`. Accepting it here alongside
    # `balances[]` would let the initial load create two independent,
    # divergence-prone sources for the same account balance. Balance seeding
    # must go exclusively through `BalanceSeed` / `AccountBalanceObservation`.
    investment_name: str | None = Field(default=None, min_length=2, max_length=120)
    investment_gross_annual_rate: Decimal | None = Field(default=None, ge=0, le=1)
    investment_income_tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    projection_end: date | None = None


class BalanceSeed(StrictModel):
    account: str = Field(min_length=1, max_length=80)
    amount: Decimal
    as_of_date: date
    observation_type: Literal["opening", "closing", "point_in_time"] = "point_in_time"


class ObligationSeed(StrictModel):
    name: str = Field(min_length=2, max_length=200)
    due_date: date
    amount: Decimal = Field(gt=0)
    recurrence_months: int = Field(default=0, ge=0, le=120)
    occurrence_count: int = Field(default=1, ge=1, le=240)
    category: str = Field(default="general", max_length=60)


class DocumentSeed(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    document_type: Literal["bank_statement", "credit_card", "payroll"]
    account: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_account_reference(self) -> DocumentSeed:
        if self.document_type != "payroll" and not self.account:
            raise ValueError("Extratos e faturas exigem a chave da conta")
        if self.document_type == "payroll" and self.account:
            raise ValueError("Holerite não deve apontar para uma conta")
        return self


class InitialLoadManifest(StrictModel):
    version: Literal[1]
    load_id: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    accounts: list[AccountSeed] = Field(default_factory=list)
    profile: ProfileSeed | None = None
    balances: list[BalanceSeed] = Field(default_factory=list)
    obligations: list[ObligationSeed] = Field(default_factory=list)
    documents: list[DocumentSeed] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> InitialLoadManifest:
        keys = [item.key for item in self.accounts]
        if len(keys) != len(set(keys)):
            raise ValueError("Chaves de conta duplicadas no manifesto")
        known = set(keys)
        for balance in self.balances:
            if balance.account not in known:
                raise ValueError(f"Saldo referencia conta desconhecida: {balance.account}")
        for document in self.documents:
            if document.account and document.account not in known:
                raise ValueError(f"Documento referencia conta desconhecida: {document.account}")
        return self


PROFILE_FIELDS = (
    "monthly_salary_net",
    "monthly_cash_cap",
    "emergency_floor",
    "food_allowance",
    "meal_allowance_daily",
    "workdays_month",
    "investment_name",
    "investment_gross_annual_rate",
    "investment_income_tax_rate",
    "projection_end",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Carrega estrutura e documentos históricos locais sem duplicar fatos financeiros."
    )
    parser.add_argument("manifest", type=Path, help="Manifesto JSON local")
    parser.add_argument("--household", help="Nome exato da família quando houver mais de uma")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Valida e mostra prévia sem gravar")
    mode.add_argument("--apply", action="store_true", help="Aplica a carga confirmada")
    return parser.parse_args()


def load_manifest(path: Path) -> InitialLoadManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Manifesto não encontrado: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Manifesto JSON inválido: {exc}") from exc
    try:
        return InitialLoadManifest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _document_path(manifest_path: Path, item: DocumentSeed) -> Path:
    candidate = Path(item.path)
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ValueError(f"Documento não encontrado: {candidate}")
    return candidate


def _admin_user(db: Session, household_id: str) -> User:
    user = db.scalar(
        select(User)
        .where(
            User.household_id == household_id,
            User.is_admin.is_(True),
            User.active.is_(True),
        )
        .order_by(User.created_at, User.id)
    )
    if user is None:
        raise ValueError("A família precisa ter um administrador ativo antes da carga inicial")
    return user


def _audit(
    db: Session,
    *,
    household_id: str,
    user_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    load_id: str,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(
        AuditEvent(
            household_id=household_id,
            user_id=user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps({"load_id": load_id}, ensure_ascii=False),
            before_state=before,
            after_state=after,
            reason="Carga inicial confirmada pelo operador",
            trace_id=str(uuid.uuid4()),
            source="initial_load",
        )
    )


def _account_state(item: Account | AccountSeed) -> dict[str, object]:
    return {
        "name": item.name,
        "institution": item.institution,
        "account_type": item.account_type,
        "owner_label": item.owner_label,
        "last_four": item.last_four,
    }


def _resolve_accounts(
    db: Session,
    household_id: str,
    manifest: InitialLoadManifest,
    *,
    apply: bool,
    user_id: str,
) -> tuple[dict[str, Account], dict[str, int]]:
    resolved: dict[str, Account] = {}
    stats = {"created": 0, "skipped": 0}
    for seed in manifest.accounts:
        existing = db.scalar(
            select(Account).where(
                Account.household_id == household_id,
                Account.name == seed.name,
            )
        )
        if existing:
            if _account_state(existing) != _account_state(seed):
                raise ValueError(
                    f"Conflito na conta '{seed.name}': metadados existentes não serão sobrescritos"
                )
            resolved[seed.key] = existing
            stats["skipped"] += 1
            continue
        stats["created"] += 1
        if not apply:
            continue
        account = Account(
            household_id=household_id,
            name=seed.name,
            institution=seed.institution,
            account_type=seed.account_type,
            owner_label=seed.owner_label,
            last_four=seed.last_four,
        )
        db.add(account)
        db.flush()
        resolved[seed.key] = account
        _audit(
            db,
            household_id=household_id,
            user_id=user_id,
            event_type="initial_load.account.create",
            entity_type="account",
            entity_id=account.id,
            load_id=manifest.load_id,
            before=None,
            after=_account_state(account),
        )
    return resolved, stats


def _profile_changes(profile: FinancialProfile, seed: ProfileSeed | None) -> dict[str, object]:
    if seed is None:
        return {}
    supplied = seed.model_dump(exclude_none=True)
    return {
        field: value
        for field, value in supplied.items()
        if field in PROFILE_FIELDS and getattr(profile, field) != value
    }


def _apply_profile(
    db: Session,
    household_id: str,
    manifest: InitialLoadManifest,
    *,
    apply: bool,
    user_id: str,
) -> dict[str, object]:
    if manifest.profile is None:
        return {"created": False, "changes": {}}
    profile = db.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )
    created = profile is None
    if profile is None:
        profile = FinancialProfile(household_id=household_id)
        if apply:
            db.add(profile)
            db.flush()
    changes = _profile_changes(profile, manifest.profile)
    if not apply:
        return {"created": created, "changes": {k: str(v) for k, v in changes.items()}}
    if not changes and not created:
        return {"created": False, "changes": {}}
    before = {key: str(getattr(profile, key)) for key in changes}
    for key, value in changes.items():
        setattr(profile, key, value)
    db.flush()
    after = {key: str(getattr(profile, key)) for key in changes}
    _audit(
        db,
        household_id=household_id,
        user_id=user_id,
        event_type="initial_load.profile.apply",
        entity_type="financial_profile",
        entity_id=profile.id,
        load_id=manifest.load_id,
        before=before or None,
        after=after or {"created": True},
    )
    return {"created": created, "changes": after}


def _apply_obligations(
    db: Session,
    household_id: str,
    manifest: InitialLoadManifest,
    *,
    apply: bool,
    user_id: str,
) -> dict[str, int]:
    stats = {"created": 0, "skipped": 0}
    for seed in manifest.obligations:
        existing = db.scalar(
            select(Obligation).where(
                Obligation.household_id == household_id,
                Obligation.name == seed.name,
                Obligation.due_date == seed.due_date,
            )
        )
        expected = {
            "amount": seed.amount,
            "recurrence_months": seed.recurrence_months,
            "occurrence_count": seed.occurrence_count,
            "category": seed.category,
            "active": True,
        }
        if existing:
            actual = {key: getattr(existing, key) for key in expected}
            if actual != expected:
                raise ValueError(
                    f"Conflito na obrigação '{seed.name}' em {seed.due_date}: não será alterada"
                )
            stats["skipped"] += 1
            continue
        stats["created"] += 1
        if not apply:
            continue
        obligation = Obligation(
            household_id=household_id,
            name=seed.name,
            due_date=seed.due_date,
            amount=seed.amount,
            recurrence_months=seed.recurrence_months,
            occurrence_count=seed.occurrence_count,
            category=seed.category,
            active=True,
        )
        db.add(obligation)
        db.flush()
        _audit(
            db,
            household_id=household_id,
            user_id=user_id,
            event_type="initial_load.obligation.create",
            entity_type="obligation",
            entity_id=obligation.id,
            load_id=manifest.load_id,
            before=None,
            after={
                "name": obligation.name,
                "due_date": obligation.due_date.isoformat(),
                "amount": str(obligation.amount),
                "category": obligation.category,
            },
        )
    return stats


def _apply_balances(
    db: Session,
    household_id: str,
    manifest: InitialLoadManifest,
    accounts: dict[str, Account],
    *,
    apply: bool,
    user_id: str,
) -> dict[str, int]:
    stats = {"created": 0, "skipped": 0}
    for seed in manifest.balances:
        account = accounts.get(seed.account)
        if account is None:
            if apply:
                raise ValueError(f"Conta ainda não resolvida para saldo: {seed.account}")
            stats["created"] += 1
            continue
        existing = db.scalar(
            select(AccountBalanceObservation).where(
                AccountBalanceObservation.household_id == household_id,
                AccountBalanceObservation.account_id == account.id,
                AccountBalanceObservation.as_of_date == seed.as_of_date,
                AccountBalanceObservation.observation_type == seed.observation_type,
                AccountBalanceObservation.superseded_by_id.is_(None),
            )
        )
        if existing:
            # Idempotency must match the seed's own provenance contract, not
            # merely the amount: a lower-confidence/different-source fact
            # (e.g. `source="statement"` from reconciliation, see
            # app/services/reconciliation.py) that happens to share the same
            # amount/date/type is not evidence of a prior `manual_confirmed`
            # confirmation and must never be silently accepted as one.
            is_equivalent_seed = (
                existing.source == "manual_confirmed"
                and existing.confidence == Decimal("1.0000")
                and Decimal(existing.amount) == seed.amount
            )
            if not is_equivalent_seed:
                raise ValueError(
                    f"Conflito de saldo para '{account.name}' em {seed.as_of_date}: já existe "
                    f"observação ativa (source={existing.source}, confidence={existing.confidence}) "
                    "que não corresponde à confirmação manual exigida pela carga inicial; "
                    "use o fluxo explícito de supersessão"
                )
            stats["skipped"] += 1
            continue
        stats["created"] += 1
        if not apply:
            continue
        observation = AccountBalanceObservation(
            household_id=household_id,
            account_id=account.id,
            amount=seed.amount,
            as_of_date=seed.as_of_date,
            observation_type=seed.observation_type,
            source="manual_confirmed",
            document_id=None,
            confirmed_by=user_id,
            confidence=Decimal("1.0000"),
            trace_id=str(uuid.uuid4()),
        )
        db.add(observation)
        db.flush()
        _audit(
            db,
            household_id=household_id,
            user_id=user_id,
            event_type="initial_load.balance.confirm",
            entity_type="account_balance_observation",
            entity_id=observation.id,
            load_id=manifest.load_id,
            before=None,
            after={
                "account_id": account.id,
                "amount": str(observation.amount),
                "as_of_date": observation.as_of_date.isoformat(),
                "observation_type": observation.observation_type,
            },
        )
    return stats


def _preview_documents(
    db: Session,
    household_id: str,
    manifest_path: Path,
    manifest: InitialLoadManifest,
) -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    for seed in manifest.documents:
        path = _document_path(manifest_path, seed)
        payload = path.read_bytes()
        digest = file_sha256(payload)
        existing = db.scalar(
            select(Document.id).where(
                Document.household_id == household_id,
                Document.sha256 == digest,
            )
        )
        item: dict[str, object] = {
            "file": path.name,
            "document_type": seed.document_type,
            "status": "skipped_existing" if existing else "ready",
            "records": 0,
        }
        if existing:
            report.append(item)
            continue
        try:
            parsed = (
                parse_payroll_document(payload)
                if seed.document_type == "payroll"
                else parse_document_contract(path.name, payload, seed.document_type)
            )
            reconciliation = reconcile_parsed_document(parsed)
            item["records"] = 1 if parsed.payroll is not None else len(parsed.transactions)
            item["reconciliation"] = reconciliation.status
            # `--dry-run` only exercises parsing + reconciliation, which are
            # pure/deterministic. It deliberately never calls the official
            # `import_document` pipeline here (unlike `--apply`, see
            # `_apply_documents`) because that pipeline writes the encrypted
            # payload to disk via `EncryptedDocumentStore.save()` as an
            # unconditional side effect *before* any DB commit — a side
            # effect a later `db.rollback()` cannot undo. So classification
            # and duplicate detection (`classify_with_local_rules`,
            # `register_transaction_duplicates` in app/api.py) are only ever
            # evaluated during `--apply`. A document that parses and
            # reconciles cleanly here can still be routed to `ReviewItem`
            # during `--apply`; never report it as unconditionally "ready".
            item["status"] = "pending_full_validation"
            item["note"] = (
                "Duplicidade e classificação só são avaliadas durante --apply, "
                "pelo pipeline oficial de importação."
            )
        except ValueError as exc:
            item["status"] = "review_required"
            item["message"] = str(exc)
        report.append(item)
    return report


async def _apply_documents(
    db: Session,
    manifest_path: Path,
    manifest: InitialLoadManifest,
    accounts: dict[str, Account],
    user: User,
) -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    for seed in manifest.documents:
        path = _document_path(manifest_path, seed)
        payload = path.read_bytes()
        upload = UploadFile(file=io.BytesIO(payload), filename=path.name)
        account_id = accounts[seed.account].id if seed.account else None
        try:
            result = await import_document(
                account_id=account_id,
                document_type=seed.document_type,
                file=upload,
                user=user,
                db=db,
            )
            report.append(
                {
                    "file": path.name,
                    "document_type": seed.document_type,
                    "status": result.get("status", "imported"),
                    "records": result.get("records", 0),
                    "review_items": result.get("review_items", 0),
                    "reconciliation": (result.get("reconciliation") or {}).get("status"),
                }
            )
        except HTTPException as exc:
            if exc.status_code == 409 and exc.detail == "Este arquivo já foi importado":
                db.rollback()
                report.append(
                    {
                        "file": path.name,
                        "document_type": seed.document_type,
                        "status": "skipped_existing",
                        "records": 0,
                    }
                )
                continue
            raise
        finally:
            await upload.close()
    return report


def _summary(
    *,
    mode: str,
    manifest: InitialLoadManifest,
    accounts: dict[str, int],
    profile: dict[str, object],
    obligations: dict[str, int],
    balances: dict[str, int],
    documents: list[dict[str, object]],
) -> dict[str, object]:
    # "review_required" is a deterministic fact (parse failed / real pipeline
    # already routed it to review). "pending_full_validation" is dry-run's
    # honest "unknown yet" state — it must never be folded into
    # review_required, since that would overclaim in the opposite direction.
    review_statuses = {"review_required", "imported_with_review"}
    pending_statuses = {"pending_full_validation"}
    return {
        "mode": mode,
        "load_id": manifest.load_id,
        "accounts": accounts,
        "profile": profile,
        "obligations": obligations,
        "balances": balances,
        "documents": documents,
        "document_counts": {
            "total": len(documents),
            "review_required": sum(item.get("status") in review_statuses for item in documents),
            "pending_full_validation": sum(item.get("status") in pending_statuses for item in documents),
            "skipped_existing": sum(item.get("status") == "skipped_existing" for item in documents),
        },
    }


def main() -> int:
    args = _arguments()
    try:
        manifest = load_manifest(args.manifest)
        with SessionLocal() as db:
            household = select_household(db, args.household)
            user = _admin_user(db, household.id)
            accounts, account_stats = _resolve_accounts(
                db, household.id, manifest, apply=args.apply, user_id=user.id
            )
            profile_stats = _apply_profile(
                db, household.id, manifest, apply=args.apply, user_id=user.id
            )
            obligation_stats = _apply_obligations(
                db, household.id, manifest, apply=args.apply, user_id=user.id
            )
            balance_stats = _apply_balances(
                db,
                household.id,
                manifest,
                accounts,
                apply=args.apply,
                user_id=user.id,
            )
            if args.apply:
                db.commit()
                documents = asyncio.run(_apply_documents(db, args.manifest, manifest, accounts, user))
                mode = "apply"
            else:
                documents = _preview_documents(db, household.id, args.manifest, manifest)
                db.rollback()
                mode = "dry-run"
            print(
                json.dumps(
                    _summary(
                        mode=mode,
                        manifest=manifest,
                        accounts=account_stats,
                        profile=profile_stats,
                        obligations=obligation_stats,
                        balances=balance_stats,
                        documents=documents,
                    ),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        return 0
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        print(json.dumps({"status": "blocked", "error": detail}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
