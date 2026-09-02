"""Build and persist immutable, traceable financial snapshots."""

from __future__ import annotations

import hashlib
import json
import uuid
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountBalanceObservation,
    Category,
    Document,
    FinancialProfile,
    FinancialSnapshot,
    FinancialSnapshotLineage,
    Obligation,
    Transaction,
)
from app.services.classifier import normalize_description
from app.services.finance import add_months, money
from app.services.financial_engine import FinancialEngineInput, calculate_actual_snapshot
from app.services.financial_integrity import consolidated_integrity_status

TRANSFER_CATEGORIES = {"Transferência interna", "Transferência patrimonial", "Conciliação"}


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    entity_type: str
    entity_id: str
    document_id: str | None
    source_role: str
    rule_id: str
    metric_key: str
    contribution: Decimal | None


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


def consolidated_transactions(
    db: Session, household_id: str, start: date, end: date
) -> tuple[list[tuple[Transaction, str]], list[Transaction]]:
    """Select the same authoritative transaction copies used by the UI."""

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
    selected: list[tuple[Transaction, str]] = []
    ignored: list[Transaction] = []
    for transaction, category_name, account_type, document_type in raw_rows:
        if transaction.canonical_status == "supporting":
            ignored.append(transaction)
        elif transaction.canonical_status in {"canonical", "distinct"}:
            selected.append((transaction, str(category_name or "Revisar")))
        elif (
            document_type != "financial_plan_workbook"
            and _expense_signature(transaction, account_type) in workbook_signatures
        ):
            ignored.append(transaction)
        else:
            selected.append((transaction, str(category_name or "Revisar")))
    return selected, ignored


def _obligation_occurs_in(item: Obligation, start: date, end: date) -> bool:
    due = item.due_date
    for occurrence in range(max(1, item.occurrence_count)):
        if start <= due < end:
            return True
        if item.recurrence_months == 0:
            break
        target = add_months(
            item.due_date.replace(day=1), (occurrence + 1) * item.recurrence_months
        )
        due = target.replace(day=min(item.due_date.day, monthrange(target.year, target.month)[1]))
    return False


def _opening_balance(
    db: Session,
    household_id: str,
    profile: FinancialProfile,
    period_start: date,
    previous_snapshot: FinancialSnapshot | None,
) -> tuple[Decimal, SnapshotSource, bool]:
    investment_accounts = tuple(
        db.scalars(
            select(Account).where(
                Account.household_id == household_id,
                Account.active.is_(True),
                Account.account_type == "investment",
            )
        ).all()
    )
    profile_name = normalize_description(profile.investment_name)
    matching = tuple(
        account
        for account in investment_accounts
        if profile_name
        and profile_name
        in normalize_description(f"{account.institution} {account.name}")
    )
    eligible = matching or (investment_accounts if len(investment_accounts) == 1 else ())
    observation = None
    if eligible:
        statement = select(AccountBalanceObservation).where(
            AccountBalanceObservation.household_id == household_id,
            AccountBalanceObservation.account_id.in_([item.id for item in eligible]),
            AccountBalanceObservation.invalidated_at.is_(None),
            AccountBalanceObservation.superseded_by_id.is_(None),
        )
        statement = statement.where(
            AccountBalanceObservation.as_of_date == period_start
            if previous_snapshot is not None
            else AccountBalanceObservation.as_of_date <= period_start
        )
        observation = db.scalar(
            statement.order_by(
                AccountBalanceObservation.as_of_date.desc(),
                AccountBalanceObservation.created_at.desc(),
            ).limit(1)
        )
    if observation is not None:
        return (
            money(observation.amount),
            SnapshotSource(
                "account_balance_observation",
                observation.id,
                observation.document_id,
                "canonical",
                "OPENING-LIQUIDITY-OBSERVATION",
                "opening_liquidity_balance",
                money(observation.amount),
            ),
            True,
        )
    if previous_snapshot is not None and previous_snapshot.payload.get(
        "balance_evidence_trusted", False
    ):
        return (
            money(previous_snapshot.closing_liquidity_balance),
            SnapshotSource(
                "financial_snapshot",
                previous_snapshot.id,
                None,
                "canonical",
                "PRIOR-CLOSING-LIQUIDITY-CARRY",
                "opening_liquidity_balance",
                money(previous_snapshot.closing_liquidity_balance),
            ),
            True,
        )
    return (
        money(profile.investment_balance),
        SnapshotSource(
            "financial_profile",
            profile.id,
            None,
            "supporting",
            "OPENING-LIQUIDITY-LEGACY-FALLBACK",
            "opening_liquidity_balance",
            money(profile.investment_balance),
        ),
        False,
    )


def _source_hash(sources: list[SnapshotSource], profile: FinancialProfile) -> str:
    payload = {
        "profile": {
            "id": profile.id,
            "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
            "cash_cap": str(profile.monthly_cash_cap),
            "floor": str(profile.emergency_floor),
        },
        "sources": [
            {
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "document_id": item.document_id,
                "source_role": item.source_role,
                "rule_id": item.rule_id,
                "metric_key": item.metric_key,
                "contribution": str(item.contribution) if item.contribution is not None else None,
            }
            for item in sources
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _collect(
    db: Session, household_id: str, period: str
) -> tuple[dict[str, Any], list[SnapshotSource], dict[str, Any]]:
    start = datetime.strptime(period, "%Y-%m").date()
    end = add_months(start, 1)
    profile = db.scalar(select(FinancialProfile).where(FinancialProfile.household_id == household_id))
    if profile is None:
        raise ValueError("financial profile is required")
    previous_period = add_months(start, -1).strftime("%Y-%m")
    previous_snapshot = db.scalar(
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.period == previous_period,
            FinancialSnapshot.snapshot_kind == "actual",
            FinancialSnapshot.status == "current",
        )
        .order_by(FinancialSnapshot.period.desc(), FinancialSnapshot.version.desc())
        .limit(1)
    )
    rows, ignored = consolidated_transactions(db, household_id, start, end)
    opening, opening_source, observed_balance = _opening_balance(
        db, household_id, profile, start, previous_snapshot
    )
    sources = [opening_source]
    opening_uncovered_deficit = Decimal("0")
    if (
        previous_snapshot is not None
        and Decimal(previous_snapshot.closing_uncovered_deficit) > 0
    ):
        opening_uncovered_deficit = money(previous_snapshot.closing_uncovered_deficit)
        sources.append(
            SnapshotSource(
                "financial_snapshot",
                previous_snapshot.id,
                None,
                "canonical",
                "PRIOR-UNCOVERED-DEFICIT-CARRY",
                "opening_uncovered_deficit",
                opening_uncovered_deficit,
            )
        )
    totals = {
        "income": Decimal("0"),
        "expenses": Decimal("0"),
        "bank_in": Decimal("0"),
        "bank_out": Decimal("0"),
        "card_spend": Decimal("0"),
        "card_payments": Decimal("0"),
        "refunds": Decimal("0"),
        "investments": Decimal("0"),
        "redemptions": Decimal("0"),
        "internal_transfers": Decimal("0"),
        "bank_refunds": Decimal("0"),
        "card_refunds": Decimal("0"),
    }
    categories: dict[str, Decimal] = {}
    accounts: dict[str, dict[str, Any]] = {}
    pending_duplicates = 0
    for transaction, category_name in rows:
        amount = money(transaction.amount)
        role = "excluded" if transaction.excluded else "canonical"
        account_type = transaction.account.account_type if transaction.account else "other"
        metric = "source_count"
        contribution: Decimal | None = None
        if transaction.possible_duplicate and transaction.canonical_status == "unassigned":
            pending_duplicates += 1
            role = "excluded"
        elif category_name == "Transferência patrimonial":
            role = "canonical"
            metric = "investments" if amount < 0 else "redemptions"
            totals[metric] += abs(amount)
            contribution = abs(amount)
        elif category_name == "Transferência interna":
            role = "canonical"
            metric = "internal_transfers"
            totals[metric] += abs(amount)
            contribution = abs(amount)
        elif category_name == "Conciliação" or transaction.transaction_type == "reconciliation":
            role = "canonical"
            metric = "card_payments"
            totals[metric] += abs(amount)
            contribution = abs(amount)
        elif (
            transaction.transaction_type == "income"
            and amount > 0
            and not transaction.excluded
            and category_name == "Receitas"
        ):
            metric = "operating_income"
            totals["income"] += amount
            totals["bank_in"] += amount
            contribution = amount
        elif transaction.transaction_type == "refund" and amount > 0:
            metric = "refunds"
            totals["refunds"] += amount
            totals["card_refunds" if account_type == "credit_card" else "bank_refunds"] += amount
            categories[category_name] = categories.get(category_name, Decimal("0")) - amount
            contribution = amount
        elif (
            transaction.transaction_type in {"expense", "refund"}
            and amount < 0
            and not transaction.excluded
        ):
            contribution = abs(amount)
            totals["expenses"] += contribution
            categories[category_name] = categories.get(category_name, Decimal("0")) + contribution
            if account_type == "credit_card":
                metric = "card_spend"
                totals["card_spend"] += contribution
            else:
                metric = "bank_cash_out"
                totals["bank_out"] += contribution
        sources.append(
            SnapshotSource(
                "transaction",
                transaction.id,
                transaction.document_id,
                role,
                "CANONICAL-TRANSACTION-SELECTION",
                metric,
                contribution,
            )
        )
        if contribution is not None and metric in {
            "operating_income",
            "bank_cash_out",
            "card_spend",
            "refunds",
        }:
            key = transaction.account_id or "unidentified"
            account = accounts.setdefault(
                key,
                {
                    "account_id": transaction.account_id,
                    "account": transaction.account.name if transaction.account else "Sem conta identificada",
                    "institution": transaction.account.institution if transaction.account else "",
                    "account_type": account_type,
                    "cash_in": Decimal("0"),
                    "gross_out": Decimal("0"),
                    "refunds": Decimal("0"),
                },
            )
            if metric == "operating_income":
                account["cash_in"] += contribution
            elif metric == "refunds":
                account["refunds"] += contribution
            else:
                account["gross_out"] += contribution

    obligations = tuple(
        db.scalars(
            select(Obligation).where(
                Obligation.household_id == household_id,
                Obligation.active.is_(True),
            )
        ).all()
    )
    commitments = sum(
        (money(item.amount) for item in obligations if _obligation_occurs_in(item, start, end)),
        Decimal("0"),
    )
    for item in obligations:
        if _obligation_occurs_in(item, start, end):
            sources.append(
                SnapshotSource(
                    "obligation",
                    item.id,
                    None,
                    "canonical",
                    "ACTIVE-OBLIGATION-IN-PERIOD",
                    "commitments",
                    money(item.amount),
                )
            )

    result = calculate_actual_snapshot(
        FinancialEngineInput(
            period=period,
            operating_income=totals["income"],
            operating_expenses=max(Decimal("0"), totals["expenses"] - totals["refunds"]),
            bank_cash_in=totals["bank_in"],
            bank_cash_out=max(Decimal("0"), totals["bank_out"] - totals["bank_refunds"]),
            investments=totals["investments"],
            redemptions=totals["redemptions"],
            internal_transfers=totals["internal_transfers"],
            card_spend=max(Decimal("0"), totals["card_spend"] - totals["card_refunds"]),
            card_payments=totals["card_payments"],
            refunds=totals["refunds"],
            opening_liquidity_balance=opening,
            opening_uncovered_deficit=opening_uncovered_deficit,
            safety_floor=profile.emergency_floor,
            budget_cap=profile.monthly_cash_cap,
            commitments=commitments,
            source_count=len(rows),
        )
    )
    integrity = consolidated_integrity_status(db, household_id=household_id, period=period)
    payload = result.canonical_payload()
    payload.update(
        {
            "source_hash": _source_hash(sources, profile),
            "opening_balance_source": (
                "observation"
                if opening_source.entity_type == "account_balance_observation"
                else "prior_snapshot"
                if opening_source.entity_type == "financial_snapshot"
                else "legacy_profile"
            ),
            "balance_evidence_trusted": observed_balance,
            "duplicates_ignored": len(ignored) + pending_duplicates,
            "category_spending": [
                {"category": key, "amount": float(money(value))}
                for key, value in sorted(categories.items(), key=lambda item: item[1], reverse=True)
                if value > 0
            ],
            "cash_flow_by_account": _serialize_accounts(accounts),
        }
    )
    payload["integrity"] = {
        "status": integrity["status"],
        "trusted_for_reports": bool(integrity["trusted_for_reports"]),
        "trusted_for_projection": bool(integrity["trusted_for_projection"]),
        "open_findings": int(integrity["open_findings"]),
    }
    checksum = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload, sources, {
        "checksum": checksum,
        "integrity": integrity,
        "observed_balance": observed_balance,
    }


def _serialize_accounts(accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    serialized = []
    for account in accounts.values():
        cash_in = money(account["cash_in"])
        refunds = money(account["refunds"])
        cash_out = money(max(Decimal("0"), account["gross_out"] - refunds))
        account_type = account["account_type"]
        serialized.append(
            {
                **{key: account[key] for key in ("account_id", "account", "institution", "account_type")},
                "cash_in": float(cash_in),
                "cash_out": float(cash_out),
                "bank_cash_out": float(cash_out if account_type != "credit_card" else 0),
                "card_spending": float(cash_out if account_type == "credit_card" else 0),
                "refunds": float(refunds),
                "net": float(money(cash_in - cash_out)),
            }
        )
    return sorted(serialized, key=lambda item: (item["account_type"], item["account"]))


def build_snapshot(
    db: Session,
    *,
    household_id: str,
    period: str,
    generated_by: str | None = None,
    force: bool = False,
) -> FinancialSnapshot:
    _ensure_immediate_predecessor(
        db,
        household_id=household_id,
        period=period,
        generated_by=generated_by,
    )
    _lock_snapshot_key(db, household_id=household_id, period=period)
    payload, sources, metadata = _collect(db, household_id, period)
    current = db.scalar(
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.period == period,
            FinancialSnapshot.snapshot_kind == "actual",
            FinancialSnapshot.status == "current",
        )
        .order_by(FinancialSnapshot.version.desc())
        .limit(1)
    )
    if current is not None and current.checksum == metadata["checksum"] and not force:
        return current
    version = int(
        db.scalar(
            select(func.coalesce(func.max(FinancialSnapshot.version), 0)).where(
                FinancialSnapshot.household_id == household_id,
                FinancialSnapshot.period == period,
                FinancialSnapshot.snapshot_kind == "actual",
            )
        )
        or 0
    ) + 1
    trace_id = str(uuid.uuid4())
    integrity = metadata["integrity"]
    trusted_balance = bool(metadata["observed_balance"])
    snapshot = FinancialSnapshot(
        household_id=household_id,
        period=period,
        snapshot_kind="actual",
        version=version,
        payload=payload,
        checksum=metadata["checksum"],
        calculation_version=payload["calculation_version"],
        financial_rules_version=payload["financial_rules_version"],
        integrity_status=str(integrity["status"]),
        trusted_for_reports=trusted_balance and bool(integrity["trusted_for_reports"]),
        trusted_for_projection=trusted_balance and bool(integrity["trusted_for_projection"]),
        generated_by=generated_by,
        trace_id=trace_id,
        **{
            key: (
                int(payload[key])
                if key == "source_count"
                else Decimal(str(payload[key]))
            )
            for key in (
                "operating_income", "operating_expenses", "operating_result",
                "bank_cash_in", "bank_cash_out", "bank_cash_result", "investments",
                "redemptions", "internal_transfers", "card_spend", "card_payments",
                "refunds", "opening_liquidity_balance", "investment_yield",
                "liquidity_used", "closing_liquidity_balance",
                "opening_uncovered_deficit", "closing_uncovered_deficit", "safety_floor",
                "distance_to_floor", "budget_cap", "budget_usage", "budget_remaining",
                "commitments", "projected_balance", "source_count",
            )
        },
    )
    db.add(snapshot)
    db.flush()
    for source in sources:
        db.add(
            FinancialSnapshotLineage(
                household_id=household_id,
                snapshot_id=snapshot.id,
                metric_key=source.metric_key,
                entity_type=source.entity_type,
                entity_id=source.entity_id,
                document_id=source.document_id,
                rule_id=source.rule_id,
                contribution=source.contribution,
                source_role=source.source_role,
                trace_id=trace_id,
            )
        )
    if current is not None:
        current.status = "superseded"
        current.superseded_at = datetime.now(UTC)
        current.superseded_by_id = snapshot.id
    db.flush()
    return snapshot


def _lock_snapshot_key(db: Session, *, household_id: str, period: str) -> None:
    """Serialize same-period builds on PostgreSQL without touching source rows."""

    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext(f"financial-snapshot:{household_id}:{period}:actual")
                )
            )
        )


def _ensure_immediate_predecessor(
    db: Session,
    *,
    household_id: str,
    period: str,
    generated_by: str | None,
) -> None:
    """Materialize every relevant prior state so results never depend on view order."""

    start = datetime.strptime(period, "%Y-%m").date()
    earliest_transaction = db.scalar(
        select(func.min(Transaction.booked_at)).where(Transaction.household_id == household_id)
    )
    earliest_observation = db.scalar(
        select(func.min(AccountBalanceObservation.as_of_date)).where(
            AccountBalanceObservation.household_id == household_id,
            AccountBalanceObservation.invalidated_at.is_(None),
        )
    )
    earliest_snapshot = db.scalar(
        select(func.min(FinancialSnapshot.period)).where(
            FinancialSnapshot.household_id == household_id,
            FinancialSnapshot.snapshot_kind == "actual",
        )
    )
    candidates = [
        value.strftime("%Y-%m") if isinstance(value, date) else value
        for value in (earliest_transaction, earliest_observation, earliest_snapshot)
        if value is not None
    ]
    if not candidates:
        return
    previous_period = add_months(start, -1).strftime("%Y-%m")
    if previous_period < min(candidates):
        return
    build_snapshot(
        db,
        household_id=household_id,
        period=previous_period,
        generated_by=generated_by,
    )


def serialize_snapshot(snapshot: FinancialSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "household_id": snapshot.household_id,
        "period": snapshot.period,
        "kind": snapshot.snapshot_kind,
        "version": snapshot.version,
        "status": snapshot.status,
        "checksum": snapshot.checksum,
        "calculation_version": snapshot.calculation_version,
        "financial_rules_version": snapshot.financial_rules_version,
        "integrity_status": snapshot.integrity_status,
        "trusted_for_reports": snapshot.trusted_for_reports,
        "trusted_for_projection": snapshot.trusted_for_projection,
        "generated_at": snapshot.generated_at.isoformat() if snapshot.generated_at else None,
        "trace_id": snapshot.trace_id,
        **dict(snapshot.payload),
    }
