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
    DocumentReconciliation,
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
        if document_type == "financial_plan_workbook" and transaction.canonical_status != "supporting"
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
        target = add_months(item.due_date.replace(day=1), (occurrence + 1) * item.recurrence_months)
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
        if profile_name and profile_name in normalize_description(f"{account.institution} {account.name}")
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
        if previous_snapshot is not None:
            previous_start = datetime.strptime(previous_snapshot.period, "%Y-%m").date()
            statement = statement.where(
                AccountBalanceObservation.as_of_date > previous_start,
                AccountBalanceObservation.as_of_date <= period_start,
            )
        else:
            statement = statement.where(AccountBalanceObservation.as_of_date <= period_start)
        candidates = tuple(
            db.scalars(
                statement.order_by(
                    AccountBalanceObservation.as_of_date.desc(),
                    AccountBalanceObservation.created_at.desc(),
                )
            ).all()
        )
        observation = next(
            (item for item in candidates if _observation_is_trusted(db, item)),
            None,
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
    if previous_snapshot is not None and previous_snapshot.payload.get("balance_evidence_trusted", False):
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


def _observation_is_trusted(db: Session, observation: AccountBalanceObservation) -> bool:
    if observation.source == "manual_confirmed" and observation.confidence >= Decimal("1"):
        return True
    if observation.document_id is None or observation.confidence < Decimal("1"):
        return False
    return bool(
        db.scalar(
            select(DocumentReconciliation.id).where(
                DocumentReconciliation.household_id == observation.household_id,
                DocumentReconciliation.document_id == observation.document_id,
                DocumentReconciliation.status == "reconciled",
            )
        )
    )


def _source_hash(sources: list[SnapshotSource], profile: FinancialProfile) -> str:
    # `money(...)` before `str(...)` on every Decimal here, not a bare
    # `str(profile.monthly_cash_cap)` -- an un-round-tripped Python-side
    # Decimal (e.g. the column's `default=0`, still `Decimal("0")` in the
    # same session that created the row) and a freshly `SELECT`ed one from
    # PostgreSQL/SQLite (`Decimal("0.00")`, scaled per `Numeric(14, 2)`)
    # otherwise stringify to different text for the exact same value. That
    # made `build_snapshot`'s checksum -- and therefore `trust_monthly_close`
    # -- spuriously see "changed data" purely from which session first
    # touched `profile`, not from any real mutation. See the regression this
    # broke in `tests/test_monthly_close_api.py`.
    payload = {
        "profile": {
            "id": profile.id,
            "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
            "cash_cap": str(money(profile.monthly_cash_cap)),
            "floor": str(money(profile.emergency_floor)),
        },
        "sources": [
            {
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "document_id": item.document_id,
                "source_role": item.source_role,
                "rule_id": item.rule_id,
                "metric_key": item.metric_key,
                "contribution": str(money(item.contribution)) if item.contribution is not None else None,
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
    if previous_snapshot is not None and Decimal(previous_snapshot.closing_uncovered_deficit) > 0:
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
            transaction.transaction_type in {"expense", "refund"} and amount < 0 and not transaction.excluded
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
    return (
        payload,
        sources,
        {
            "checksum": checksum,
            "integrity": integrity,
            "observed_balance": observed_balance,
        },
    )


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
    version = (
        int(
            db.scalar(
                select(func.coalesce(func.max(FinancialSnapshot.version), 0)).where(
                    FinancialSnapshot.household_id == household_id,
                    FinancialSnapshot.period == period,
                    FinancialSnapshot.snapshot_kind == "actual",
                )
            )
            or 0
        )
        + 1
    )
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
            key: (int(payload[key]) if key == "source_count" else Decimal(str(payload[key])))
            for key in (
                "operating_income",
                "operating_expenses",
                "operating_result",
                "bank_cash_in",
                "bank_cash_out",
                "bank_cash_result",
                "investments",
                "redemptions",
                "internal_transfers",
                "card_spend",
                "card_payments",
                "refunds",
                "opening_liquidity_balance",
                "investment_yield",
                "liquidity_used",
                "closing_liquidity_balance",
                "opening_uncovered_deficit",
                "closing_uncovered_deficit",
                "safety_floor",
                "distance_to_floor",
                "budget_cap",
                "budget_usage",
                "budget_remaining",
                "commitments",
                "projected_balance",
                "source_count",
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


def liquidity_transition_facts(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """Derive INV-005/INV-006 facts for an already-built snapshot.

    `settle_liquidity` (the engine) zeroes the opening balance and pays down any
    prior uncovered deficit before a positive result can rebuild liquidity, so a
    period that carries debt cannot be replayed through the invariants' own
    independent `calculate_liquidity_transition(opening, monthly_result)` -- that
    function has no debt parameter at all.

    A period that starts with `opening_uncovered_deficit > 0` always closes with
    `opening_liquidity_balance == 0` (the engine forces it), and the two-step
    "pay debt, then deposit the remainder" transition is algebraically identical
    to a single step of `calculate_liquidity_transition(0, operating_result +
    investment_yield - opening_uncovered_deficit)`: whatever is left over after
    netting the period's result and yield against the carried debt is exactly
    what either formula clamps at zero. Folding debt into the result this way
    reproduces the engine's own closing/uncovered/liquidity_used figures only
    when every max/min clamp in `settle_liquidity` was applied correctly, so a
    regression in its debt-priority branch still surfaces as a mismatch here.
    A period with no carried debt is unaffected: the fold is a no-op because
    `opening_uncovered_deficit` is zero.
    """

    debt = money(snapshot.opening_uncovered_deficit)
    opening = Decimal("0.00") if debt > 0 else money(snapshot.opening_liquidity_balance)
    monthly_result = money(money(snapshot.operating_result) + money(snapshot.investment_yield) - debt)
    return {
        "opening_liquidity_balance": opening,
        "monthly_operating_result": monthly_result,
        "closing_liquidity_balance": money(snapshot.closing_liquidity_balance),
        "uncovered_deficit": money(snapshot.closing_uncovered_deficit),
        "liquidity_used": money(snapshot.liquidity_used),
    }


def snapshot_lineage_facts(db: Session, snapshot: FinancialSnapshot) -> dict[str, Any]:
    """Derive INV-022 facts from the lineage rows persisted for `snapshot`.

    Every row `build_snapshot` folded into the snapshot -- the opening evidence,
    active obligations and every selected transaction -- was already persisted
    to `financial_snapshot_lineage`, so this reads that trail back instead of
    recomputing `_collect()`'s selection a second time.

    `document_required` is left `False`: manual, non-imported transactions are
    a supported source in this schema, so a period with no imported document at
    all is not itself a lineage violation. Enforcing "every aggregate must cite
    a document" is a separate, not-yet-normative decision left for a future
    slice; see the PR's Technical Challenge / residual notes.
    """

    lineage_rows = tuple(
        db.scalars(
            select(FinancialSnapshotLineage).where(
                FinancialSnapshotLineage.snapshot_id == snapshot.id
            )
        ).all()
    )
    source_ids = [f"{row.entity_type}:{row.entity_id}" for row in lineage_rows]
    transaction_ids = sorted(
        {row.entity_id for row in lineage_rows if row.entity_type == "transaction"}
    )
    document_ids = sorted({row.document_id for row in lineage_rows if row.document_id})
    rule_ids = sorted({row.rule_id for row in lineage_rows if row.rule_id})
    account_ids: set[str] = set()
    category_ids: set[str] = set()
    if transaction_ids:
        for account_id, category_id in db.execute(
            select(Transaction.account_id, Transaction.category_id).where(
                Transaction.id.in_(transaction_ids)
            )
        ).all():
            if account_id:
                account_ids.add(account_id)
            if category_id:
                category_ids.add(category_id)
    return {
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "transaction_ids": transaction_ids,
        "document_ids": document_ids,
        "document_required": False,
        "rule_ids": rule_ids,
        "account_ids": sorted(account_ids),
        "category_ids": sorted(category_ids),
        "calculation_version": snapshot.calculation_version,
        "lineage_period": snapshot.period,
    }


def _snapshot_monetary_fields(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """The canonical monetary field selection off `snapshot`, keyed the way
    `financial_engine_values`, `dashboard_values` and `report_values` compare
    them for INV-019/INV-020. No arithmetic happens here -- this only reads
    columns already computed by the Financial Engine (`build_snapshot`); it
    is not itself "the formula", so `dashboard_monetary_dataset` and
    `report_month_monetary_dataset` sharing it is not duplicated financial
    logic, only a shared attribute selector.
    """

    return {
        "operating_income": money(snapshot.operating_income),
        "operating_expenses": money(snapshot.operating_expenses),
        "operating_result": money(snapshot.operating_result),
        "bank_cash_out": money(snapshot.bank_cash_out),
        "card_spend": money(snapshot.card_spend),
        "closing_liquidity_balance": money(snapshot.closing_liquidity_balance),
        "closing_uncovered_deficit": money(snapshot.closing_uncovered_deficit),
        "budget_cap": money(snapshot.budget_cap),
        "budget_remaining": money(snapshot.budget_remaining),
    }


def _profile_monetary_fields(profile: FinancialProfile) -> dict[str, Decimal]:
    """The canonical monetary field selection off `profile` that `/dashboard`
    also publishes.

    `investment_balance` is not produced by `build_snapshot` -- it is a
    running balance `FinancialProfile` carries directly, mutated by
    transaction/transfer/commission endpoints as they happen -- so it cannot
    live in `_snapshot_monetary_fields` (which only reads columns the
    Financial Engine wrote onto a `FinancialSnapshot` row). It gets its own
    single-field selector instead, shared by `dashboard_monetary_dataset` and
    the INV-019 fact builder below, so both read the exact same attribute.
    """

    return {"investment_balance": money(profile.investment_balance)}


def dashboard_monetary_dataset(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, Decimal]:
    """The exact monetary dataset `GET /dashboard` publishes for `snapshot`.

    `app/api.py`'s `dashboard()` calls this function -- not a copy of it --
    to build every monetary field in its response, `investment_balance`
    included (previously read inline straight off `profile`, uncovered by
    any invariant -- see the engineering review on PR 7). It is also the
    function `dashboard_and_report_consistency_facts()` calls to fill
    `dashboard_values`, so an INV-019 fact is genuine evidence about the
    endpoint's own output: if `dashboard()` ever stops calling this function
    (a hardcoded override, a stale cache, a parallel formula), its published
    numbers diverge from what this function returns, and INV-019 starts
    failing against the real gap.
    """

    return {**_snapshot_monetary_fields(snapshot), **_profile_monetary_fields(profile)}


def report_month_monetary_dataset(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """The exact monetary dataset `GET /reports` publishes for one month's
    `snapshot` row. Same contract as `dashboard_monetary_dataset`, kept as a
    separate function (rather than one shared call site) because `/dashboard`
    and `/reports` are independent consumers that could legitimately diverge
    in what they choose to publish for a period; today they publish the same
    fields, and this function is the one both `reports()` and
    `dashboard_and_report_consistency_facts()`'s `report_values` call.
    """

    return _snapshot_monetary_fields(snapshot)


def savings_rate_from_totals(total_cash_in: Decimal, total_cash_out: Decimal) -> Decimal:
    """The exact `savings_rate` formula `GET /reports` publishes for a window.

    Previously computed inline inside `reports()` only, with zero INV-020
    coverage (see the engineering review on PR 7: "reports independently
    calculates savings_rate"). Extracted as a pure function so `reports()`
    and `dashboard_and_report_consistency_facts()` call the identical
    computation instead of `reports()` keeping its own private copy. For a
    one-month window (the shape `dashboard_and_report_consistency_facts`
    checks, matching a monthly close's single closed period) this reduces to
    that month's own `operating_income`/`operating_expenses`, so it is not a
    parallel financial formula -- it is the one formula `reports()` already
    used, now shared instead of duplicated.
    """

    if total_cash_in <= 0:
        return Decimal("0.00")
    return money(((total_cash_in - total_cash_out) / total_cash_in) * Decimal("100"))


def _savings_rate_engine_truth(operating_income: Decimal, operating_expenses: Decimal) -> Decimal:
    """INV-020's own, independently-coded `savings_rate` expectation.

    Deliberately does **not** call `savings_rate_from_totals` -- the function
    `reports()` calls to publish the figure -- for the same reason INV-018
    evaluates the Projection Engine against an independently-coded
    Projection Validator instead of comparing a value to itself: if the
    "expected" side called the exact function under test, a regression in
    that function (or in what `reports()` passes into it) would move both
    sides identically and the check would always PASS by construction. The
    formula is intentionally identical to `savings_rate_from_totals`' today;
    what matters is that it is a second, independent implementation.
    """

    if operating_income <= 0:
        return Decimal("0.00")
    return money(((operating_income - operating_expenses) / operating_income) * Decimal("100"))


def dashboard_and_report_consistency_facts(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, dict[str, dict[str, Decimal] | Decimal]]:
    """Derive INV-019/INV-020 facts (`trusted_for_reports`) for `snapshot`.

    Returns one independent fact set per invariant (`"dashboard"` for
    INV-019, `"report"` for INV-020) instead of one shared
    `financial_engine_values`, because `/dashboard` and `/reports` do not
    publish exactly the same field set for a period: `/dashboard` adds
    `investment_balance` (a live `FinancialProfile` balance, not a snapshot
    column) and a one-month `/reports` window adds `savings_rate`. Every
    field in both sets is produced by calling the same functions
    `app/api.py`'s `dashboard()`/`reports()` call to build their own
    responses (see those functions' docstrings and
    `dashboard_monetary_dataset`/`savings_rate_from_totals` above). Today all
    sides agree because there genuinely is one computation path per field; if
    a consumer starts computing any of these fields a different way, the
    function it was supposed to call no longer matches what it actually
    returns, and the corresponding check fails against that real gap instead
    of a hardcoded copy. See `tests/test_financial_snapshots.py` for
    regressions that patch the publication path (not the invariant) for each
    covered field and prove FAIL.
    """

    from app.services.financial_invariants import MONEY_TOLERANCE

    engine_values = _snapshot_monetary_fields(snapshot)
    dashboard_values = dict(dashboard_monetary_dataset(snapshot, profile=profile))
    engine_savings_rate = _savings_rate_engine_truth(
        Decimal(snapshot.operating_income), Decimal(snapshot.operating_expenses)
    )
    report_values = {
        **dict(report_month_monetary_dataset(snapshot)),
        "savings_rate": savings_rate_from_totals(
            engine_values["operating_income"], engine_values["operating_expenses"]
        ),
    }
    return {
        "dashboard": {
            "financial_engine_values": {**engine_values, **_profile_monetary_fields(profile)},
            "dashboard_values": dashboard_values,
            "monetary_tolerance": MONEY_TOLERANCE,
        },
        "report": {
            "financial_engine_values": {**engine_values, "savings_rate": engine_savings_rate},
            "report_values": report_values,
            "monetary_tolerance": MONEY_TOLERANCE,
        },
    }


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
