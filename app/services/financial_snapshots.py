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

    `opening_liquidity_balance`, `distance_to_floor` and `liquidity_used`
    were added post-review (PR 7, Round 5): `/dashboard` was publishing
    `liquidity_starting_balance`/`liquidity_available`/`liquidity_withdrawal`
    straight off these same three columns inline in `app/api.py`'s
    `dashboard()`, outside any function INV-019 observed -- an endpoint-only
    mapping bug on any of the three could corrupt the real response without
    the invariant, blind to these columns, ever noticing.
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
        "opening_liquidity_balance": money(snapshot.opening_liquidity_balance),
        "distance_to_floor": money(snapshot.distance_to_floor),
        "liquidity_used": money(snapshot.liquidity_used),
    }


def _profile_monetary_fields(profile: FinancialProfile) -> dict[str, Decimal]:
    """The canonical monetary field selection off `profile` for the
    `dashboard_monetary_dataset` **publication** side only.

    `investment_balance` and `emergency_floor` are not produced by
    `build_snapshot` -- they are plain `FinancialProfile` columns
    (`investment_balance` a running balance mutated by transaction/transfer/
    commission endpoints as they happen; `emergency_floor` the household's
    configured safety-floor target) -- so neither lives in
    `_snapshot_monetary_fields` (which only reads columns the Financial
    Engine wrote onto a `FinancialSnapshot` row).

    `emergency_floor` was added post-review (PR 7, Round 6): `/dashboard`
    was publishing it straight off `profile.emergency_floor` inline in
    `app/api.py`'s `dashboard()`, outside anything INV-019 observed -- see
    the engineering review on PR 7, Round 6: "canonical snapshot outputs
    ... such as ... emergency_floor ... remain shaped separately".

    Round 7: this used to also be called by `_dashboard_financial_engine_truth`
    below for the "expected" side, on the theory that plain attribute
    selection (not response-shaping logic) was safe to share. The
    engineering review on PR 7, Round 7 rejected that: a bug confined to
    this one selector (the wrong attribute, a swapped key) would move the
    publication and "expected" sides identically, and INV-019 would keep
    passing regardless of what `/dashboard` actually published --
    circularity by a different name. `_dashboard_financial_engine_truth`
    now reads `profile.investment_balance`/`profile.emergency_floor`
    directly instead of calling this function -- see its docstring.
    """

    return {
        "investment_balance": money(profile.investment_balance),
        "emergency_floor": money(profile.emergency_floor),
    }


def food_benefits_from_profile(profile: FinancialProfile) -> Decimal:
    """The exact `food_benefits` formula `GET /dashboard` publishes: the sum
    of the household's monthly food allowance and its accrued meal
    allowance (`meal_allowance_daily * workdays_month`) -- both plain
    `FinancialProfile` columns, not a Financial Engine/snapshot output.

    Added post-review (PR 7, Round 6): `dashboard()` used to compute this
    sum inline as a private local (`benefit = profile.food_allowance + ...`)
    and publish it straight off that local, outside anything INV-019
    observed -- see the engineering review on PR 7, Round 6: "canonical
    snapshot outputs ... such as ... food_benefits ... remain shaped
    separately". `dashboard_monetary_dataset`/`dashboard_monetary_publication`
    now call this function for `dashboard()` to consume verbatim.

    Deliberately not called by `_dashboard_financial_engine_truth` below --
    see that function's docstring for why the "expected" side re-implements
    this same two-field sum independently instead.
    """

    return money(profile.food_allowance + profile.meal_allowance_daily * profile.workdays_month)


def _snapshot_payload_monetary_fields(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """`liquidity_deposit` is a genuine Financial Engine output
    (`FinancialEngineResult.liquidity_deposit`) that never got its own
    `FinancialSnapshot` column -- `build_snapshot` only carries it inside
    `payload` via `canonical_payload()`. It needs its own selector, separate
    from `_snapshot_monetary_fields` (scoped to the model's numeric
    columns), reading the exact same JSON attribute `/dashboard` already
    reads verbatim off `snapshot.payload` -- see the engineering review on
    PR 7, Round 5.
    """

    return {"liquidity_deposit": money(Decimal(str(snapshot.payload["liquidity_deposit"])))}


_ACCOUNT_CASH_FLOW_METRICS = ("cash_in", "cash_out", "bank_cash_out", "card_spending", "refunds", "net")


def account_cash_flow_rows(snapshot: FinancialSnapshot) -> list[dict[str, Any]]:
    """The exact `cash_flow_by_account` rows `GET /dashboard` publishes for
    `snapshot`. `_serialize_accounts` wrote this list once, inside
    `build_snapshot`/`_collect()`, directly into `snapshot.payload`; `/dashboard`
    now calls this function -- not `snapshot.payload` inline -- for its own
    `cash_flow_by_account` key, the same way it consumes
    `dashboard_monetary_publication` verbatim for its scalar fields (PR 7,
    Round 5). `_dashboard_account_cash_flow_publication_facts` below calls
    this same function to flatten the identical rows into the facts INV-019
    observes, so a regression in what this function returns changes the
    real HTTP response and the invariant fact together. The independent
    "expected" side, `_dashboard_account_cash_flow_engine_truth`, reads
    `snapshot.payload["cash_flow_by_account"]` directly instead of calling
    this function, for the same reason `_dashboard_financial_engine_truth`
    never calls `dashboard_monetary_publication`.
    """

    return list(snapshot.payload.get("cash_flow_by_account", []))


def _dashboard_account_cash_flow_publication_facts(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """Flatten `account_cash_flow_rows` into `cash_flow_by_account.<account_id>.<metric>`
    facts for `dashboard_values` -- the per-account monetary amounts
    `/dashboard` publishes verbatim under `cash_flow_by_account`, previously
    outside anything INV-019 observed (see the engineering review on PR 7,
    Round 5).

    Kept as its own hand-written loop, separate from
    `_dashboard_account_cash_flow_engine_truth` below, for the same reason
    `dashboard_monetary_publication` and `_dashboard_financial_engine_truth`
    each hand-write their own alias mapping instead of sharing one function:
    a bug introduced in *this* flattening specifically must not also appear
    on the independent "expected" side.
    """

    fields: dict[str, Decimal] = {}
    for row in account_cash_flow_rows(snapshot):
        account_key = str(row.get("account_id") or "unidentified")
        for metric in _ACCOUNT_CASH_FLOW_METRICS:
            fields[f"cash_flow_by_account.{account_key}.{metric}"] = money(Decimal(str(row.get(metric, 0))))
    return fields


def _dashboard_account_cash_flow_engine_truth(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """INV-019's independent "expected" side for `cash_flow_by_account`.

    Reads `snapshot.payload["cash_flow_by_account"]` directly through its
    own separately hand-written loop, instead of calling
    `_dashboard_account_cash_flow_publication_facts` -- see that function's
    docstring for why this is not shared code.
    """

    fields: dict[str, Decimal] = {}
    for row in snapshot.payload.get("cash_flow_by_account", []):
        account_key = str(row.get("account_id") or "unidentified")
        for metric in _ACCOUNT_CASH_FLOW_METRICS:
            fields[f"cash_flow_by_account.{account_key}.{metric}"] = money(Decimal(str(row.get(metric, 0))))
    return fields


def category_spending_rows(snapshot: FinancialSnapshot) -> list[dict[str, Any]]:
    """The exact `category_spending` rows `GET /dashboard` publishes for
    `snapshot`. Mirrors `account_cash_flow_rows` above for the same reason
    (PR 7, Round 6): `_serialize_accounts`'s sibling step in `build_snapshot`
    wrote this list once into `snapshot.payload`; `/dashboard` now calls
    this function -- not `snapshot.payload` inline -- for its own
    `category_spending` key, so `_dashboard_category_spending_publication_facts`
    below flattens the identical rows a regression in this function would
    change, and the independent "expected" side,
    `_dashboard_category_spending_engine_truth`, reads
    `snapshot.payload["category_spending"]` directly instead of calling this
    function -- see `account_cash_flow_rows`'s docstring for why.
    """

    return list(snapshot.payload.get("category_spending", []))


def _dashboard_category_spending_publication_facts(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """Flatten `category_spending_rows` into `category_spending.<category>.amount`
    facts for `dashboard_values` -- the per-category monetary amounts
    `/dashboard` publishes verbatim under `category_spending`, previously
    outside anything INV-019 observed (PR 7, Round 6 review: "category_spending
    contains monetary amounts nested and continues coming direct from
    snapshot.payload").
    """

    fields: dict[str, Decimal] = {}
    for row in category_spending_rows(snapshot):
        category_key = str(row.get("category") or "unidentified")
        fields[f"category_spending.{category_key}.amount"] = money(Decimal(str(row.get("amount", 0))))
    return fields


def _dashboard_category_spending_engine_truth(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """INV-019's independent "expected" side for `category_spending`. Reads
    `snapshot.payload["category_spending"]` directly through its own
    separately hand-written loop, instead of calling
    `_dashboard_category_spending_publication_facts` -- see that function's
    docstring for why this is not shared code.
    """

    fields: dict[str, Decimal] = {}
    for row in snapshot.payload.get("category_spending", []):
        category_key = str(row.get("category") or "unidentified")
        fields[f"category_spending.{category_key}.amount"] = money(Decimal(str(row.get("amount", 0))))
    return fields


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

    return {
        **_snapshot_monetary_fields(snapshot),
        **_profile_monetary_fields(profile),
        **_snapshot_payload_monetary_fields(snapshot),
        "food_benefits": food_benefits_from_profile(profile),
    }


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


def dashboard_monetary_publication(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, Decimal]:
    """The literal monetary key/value mapping `GET /dashboard` publishes in
    its JSON response for `snapshot` -- the actual response-shaping step
    (`cash_out`/`spending` both aliasing `operating_expenses`,
    `cash_cap`/`remaining_cap` aliasing `budget_cap`/`budget_remaining`,
    `cash_net` aliasing `operating_result`), not merely the pre-aliasing
    inputs `dashboard_monetary_dataset` reads off the snapshot/profile.

    Before this function existed, that key-aliasing lived inline inside
    `dashboard()`'s own `return {...}`, invisible to INV-019 no matter what
    `dashboard_monetary_dataset` returned -- a bug confined to that inline
    mapping (wrong key, stray offset, hardcoded override) could not be
    caught, because the invariant recomputed `dashboard_values` from
    `dashboard_monetary_dataset` again instead of observing what `dashboard()`
    actually published (see the engineering review on PR 7, Round 3:
    "the invariant never captures or compares the endpoint's actual
    serialized response"). `dashboard()` now returns this dict verbatim
    (merged with its non-monetary fields, only a uniform `decimal_value()`
    cast applied on top) instead of re-deriving each key inline, and
    `dashboard_and_report_consistency_facts()` calls this exact function for
    `dashboard_values` -- so a regression in this mapping changes the real
    HTTP response *and* is exactly what INV-019 observes.
    """

    monetary = dashboard_monetary_dataset(snapshot, profile=profile)
    return {
        "spending": monetary["operating_expenses"],
        "cash_in": monetary["operating_income"],
        "cash_out": monetary["operating_expenses"],
        "bank_cash_out": monetary["bank_cash_out"],
        "card_spending": monetary["card_spend"],
        "cash_net": monetary["operating_result"],
        "cash_cap": monetary["budget_cap"],
        "remaining_cap": monetary["budget_remaining"],
        "investment_balance": monetary["investment_balance"],
        "liquidity_balance": monetary["closing_liquidity_balance"],
        "liquidity_closing_balance": monetary["closing_liquidity_balance"],
        "liquidity_uncovered_deficit": monetary["closing_uncovered_deficit"],
        "liquidity_starting_balance": monetary["opening_liquidity_balance"],
        "liquidity_available": monetary["distance_to_floor"],
        "liquidity_deposit": monetary["liquidity_deposit"],
        "liquidity_withdrawal": monetary["liquidity_used"],
        # `liquidity_flow`/`emergency_floor`/`food_benefits` added post-review
        # (PR 7, Round 6): `dashboard()` used to build all three inline in its
        # own `return {...}` -- `liquidity_flow` aliasing the same `cash_net`
        # this dict already computes, `emergency_floor` straight off
        # `profile`, `food_benefits` from a private local -- outside anything
        # INV-019 observed. See the engineering review on PR 7, Round 6.
        "liquidity_flow": monetary["operating_result"],
        "emergency_floor": monetary["emergency_floor"],
        "food_benefits": monetary["food_benefits"],
    }


def _dashboard_financial_engine_truth(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, Decimal]:
    """INV-019's independent "expected" side, keyed under the exact
    publication names `dashboard_monetary_publication` uses.

    Deliberately reads `_snapshot_monetary_fields` directly -- plain
    attribute selection off `snapshot`, not the publication mapping -- for
    the same reason `_savings_rate_engine_truth` stays independent of
    `savings_rate_from_totals`: if the "expected" side called
    `dashboard_monetary_publication`, a regression in that function would
    move both sides identically and INV-019 would pass by construction
    regardless of what `/dashboard` actually publishes.

    `investment_balance`/`emergency_floor` are read directly off `profile`
    here (`money(profile.investment_balance)`/`money(profile.emergency_floor)`),
    not via `_profile_monetary_fields(profile)` -- see that function's
    docstring (PR 7, Round 7): `_profile_monetary_fields` also feeds the
    publication side (`dashboard_monetary_dataset`), so a bug confined to
    that one selector would move both sides identically and be invisible to
    INV-019, the same failure mode `independent_food_benefits` below already
    guards against for `food_benefits`.
    """

    engine = _snapshot_monetary_fields(snapshot)
    payload_fields = _snapshot_payload_monetary_fields(snapshot)
    independent_investment_balance = money(profile.investment_balance)
    independent_emergency_floor = money(profile.emergency_floor)
    # `food_benefits` is intentionally re-derived here with its own copy of
    # the formula, not `food_benefits_from_profile(profile)` -- see that
    # function's docstring, and `_savings_rate_engine_truth`'s, for why: a
    # regression confined to the shared formula must move only the
    # publication side, or this "expected" side would move identically and
    # the check would pass by construction regardless of what `/dashboard`
    # actually publishes.
    independent_food_benefits = money(
        profile.food_allowance + profile.meal_allowance_daily * profile.workdays_month
    )
    return {
        "spending": engine["operating_expenses"],
        "cash_in": engine["operating_income"],
        "cash_out": engine["operating_expenses"],
        "bank_cash_out": engine["bank_cash_out"],
        "card_spending": engine["card_spend"],
        "cash_net": engine["operating_result"],
        "cash_cap": engine["budget_cap"],
        "remaining_cap": engine["budget_remaining"],
        "investment_balance": independent_investment_balance,
        "liquidity_balance": engine["closing_liquidity_balance"],
        "liquidity_closing_balance": engine["closing_liquidity_balance"],
        "liquidity_uncovered_deficit": engine["closing_uncovered_deficit"],
        "liquidity_starting_balance": engine["opening_liquidity_balance"],
        "liquidity_available": engine["distance_to_floor"],
        "liquidity_deposit": payload_fields["liquidity_deposit"],
        "liquidity_withdrawal": engine["liquidity_used"],
        "liquidity_flow": engine["operating_result"],
        "emergency_floor": independent_emergency_floor,
        "food_benefits": independent_food_benefits,
    }


def report_month_monetary_publication(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """The literal monetary key/value mapping `GET /reports` publishes for
    one month row of `snapshot`. Same contract and rationale as
    `dashboard_monetary_publication` -- see its docstring -- kept as a
    separate function because `/dashboard` and `/reports` are independent
    consumers that could legitimately diverge in what they publish.
    """

    monetary = report_month_monetary_dataset(snapshot)
    return {
        "spending": monetary["operating_expenses"],
        "cash_in": monetary["operating_income"],
        "cash_out": monetary["operating_expenses"],
        "bank_cash_out": monetary["bank_cash_out"],
        "card_spending": monetary["card_spend"],
        "cash_net": monetary["operating_result"],
        "cash_cap": monetary["budget_cap"],
        "remaining_cap": monetary["budget_remaining"],
    }


def _report_financial_engine_truth(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """INV-020's independent "expected" side for the non-`savings_rate`
    fields, keyed under `report_month_monetary_publication`'s publication
    names. See `_dashboard_financial_engine_truth`'s docstring for why this
    reads `_snapshot_monetary_fields` directly instead of calling the
    publication function.
    """

    engine = _snapshot_monetary_fields(snapshot)
    return {
        "spending": engine["operating_expenses"],
        "cash_in": engine["operating_income"],
        "cash_out": engine["operating_expenses"],
        "bank_cash_out": engine["bank_cash_out"],
        "card_spending": engine["card_spend"],
        "cash_net": engine["operating_result"],
        "cash_cap": engine["budget_cap"],
        "remaining_cap": engine["budget_remaining"],
    }


def report_summary_monetary_publication(
    *,
    total_spending: Decimal,
    average_spending: Decimal,
    total_cash_in: Decimal,
    total_cash_out: Decimal,
    total_bank_cash_out: Decimal,
    total_card_spending: Decimal,
    cash_net: Decimal,
    liquidity_starting_balance: Decimal,
    liquidity_balance: Decimal,
    emergency_floor: Decimal,
    liquidity_available: Decimal,
    liquidity_deposit: Decimal,
    liquidity_withdrawal: Decimal,
    liquidity_uncovered_deficit: Decimal,
) -> dict[str, Decimal]:
    """The literal monetary contract `GET /reports` publishes inside
    `summary` -- every canonical monetary total/average/liquidity field the
    window's `summary` object carries, not `savings_rate` alone.

    Originally (PR 7, Round 6) covered only `savings_rate`: `reports()` used
    to compute `savings_rate = savings_rate_from_totals(...)` as a private
    local and assign `summary["savings_rate"]` from it as a separate
    literal in its own `return {...}`, with every other summary total
    (`total_spending`, `total_cash_in`, the liquidity fields, ...) built the
    same way -- a private local assigned straight into the literal, none of
    it observed by INV-020. A regression confined to any one of those
    assignments (a stale override, the wrong local, a value swapped in
    after the totals were computed) would corrupt the real response with
    nothing for the invariant to catch. See the engineering review on PR 7,
    Round 7: "all canonical monetary values derived from snapshot/profile
    that are published by /reports must be represented by side-effect-free
    publication builders consumed verbatim by the endpoint".

    `reports()` now spreads this dict verbatim into `summary` instead of
    assigning any of these keys itself, so there is no endpoint-only
    assembly step left between the already-computed totals/snapshot columns
    and the real HTTP response for INV-020 to be blind to.

    Every parameter is taken as already-computed (not read off a single
    `snapshot` here) because `summary` covers whatever window `/reports`
    was asked for (one to twelve months) -- `total_spending`/`total_cash_in`/
    etc. are `reports()`'s own per-window sums, `liquidity_starting_balance`/
    `liquidity_deposit`/etc. come from its own opening/closing snapshot
    selection and window totals ("Multi-month reports may compose the same
    per-month contract rather than inventing separate facts" -- the same
    engineering review). For the one-month window
    `dashboard_and_report_consistency_facts()` checks -- matching a monthly
    close's single closed period -- every parameter reduces to that one
    snapshot's own columns (no cross-month summation to offset them), so a
    bug confined to this assembly step still corrupts both sides together.
    """

    return {
        "total_spending": money(total_spending),
        "average_spending": money(average_spending),
        "total_cash_in": money(total_cash_in),
        "total_cash_out": money(total_cash_out),
        "total_bank_cash_out": money(total_bank_cash_out),
        "total_card_spending": money(total_card_spending),
        "cash_net": money(cash_net),
        "liquidity_starting_balance": money(liquidity_starting_balance),
        "liquidity_balance": money(liquidity_balance),
        "liquidity_closing_balance": money(liquidity_balance),
        "emergency_floor": money(emergency_floor),
        "liquidity_available": money(liquidity_available),
        "liquidity_deposit": money(liquidity_deposit),
        "liquidity_withdrawal": money(liquidity_withdrawal),
        "liquidity_uncovered_deficit": money(liquidity_uncovered_deficit),
        "liquidity_flow": money(cash_net),
        "savings_rate": savings_rate_from_totals(total_cash_in, total_cash_out),
    }


def _report_summary_engine_truth(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, Decimal]:
    """INV-020's independent "expected" side for `summary`, keyed under
    `report_summary_monetary_publication`'s publication names.

    Only ever evaluated for the one-month window
    `dashboard_and_report_consistency_facts()` checks (matching a monthly
    close's single closed period), so -- unlike the publication side, which
    must stay generic over `reports()`'s one-to-twelve-month window -- this
    reads `snapshot`/`profile` columns directly instead of taking
    pre-aggregated totals as parameters. See
    `_dashboard_financial_engine_truth`'s docstring for why this reads
    `_snapshot_monetary_fields` directly instead of calling the publication
    function, and `profile.emergency_floor` directly instead of
    `_profile_monetary_fields` (PR 7, Round 7).
    """

    engine = _snapshot_monetary_fields(snapshot)
    payload_fields = _snapshot_payload_monetary_fields(snapshot)
    independent_emergency_floor = money(profile.emergency_floor)
    engine_savings_rate = _savings_rate_engine_truth(
        Decimal(snapshot.operating_income), Decimal(snapshot.operating_expenses)
    )
    return {
        "total_spending": engine["operating_expenses"],
        "average_spending": engine["operating_expenses"],
        "total_cash_in": engine["operating_income"],
        "total_cash_out": engine["operating_expenses"],
        "total_bank_cash_out": engine["bank_cash_out"],
        "total_card_spending": engine["card_spend"],
        "cash_net": engine["operating_result"],
        "liquidity_starting_balance": engine["opening_liquidity_balance"],
        "liquidity_balance": engine["closing_liquidity_balance"],
        "liquidity_closing_balance": engine["closing_liquidity_balance"],
        "emergency_floor": independent_emergency_floor,
        "liquidity_available": engine["distance_to_floor"],
        "liquidity_deposit": payload_fields["liquidity_deposit"],
        "liquidity_withdrawal": engine["liquidity_used"],
        "liquidity_uncovered_deficit": engine["closing_uncovered_deficit"],
        "liquidity_flow": engine["operating_result"],
        "savings_rate": engine_savings_rate,
    }


def report_categories_publication_facts(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """Flatten `category_spending_rows(snapshot)` into `categories.<category>.amount`
    facts for `report_values` -- the per-category monetary amounts `/reports`
    aggregates into its `categories` array, previously outside anything
    INV-020 observed (PR 7, Round 7 review: "nested amounts of categories
    and cash_flow_by_account" still assembled outside the observed contract).

    `reports()` now sums this exact function's rows across its window
    instead of reading `snapshot.payload["category_spending"]` directly, so
    a regression in `category_spending_rows` -- the same function
    `/dashboard`/INV-019 already observe -- now surfaces in `/reports`/
    INV-020 too. For the one-month window `dashboard_and_report_consistency_facts()`
    checks (matching a monthly close's single closed period), summing across
    the window is a no-op over this one snapshot's own rows.
    """

    fields: dict[str, Decimal] = {}
    for row in category_spending_rows(snapshot):
        category_key = str(row.get("category") or "unidentified")
        fields[f"categories.{category_key}.amount"] = money(Decimal(str(row.get("amount", 0))))
    return fields


def _report_categories_engine_truth(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """INV-020's independent "expected" side for `categories`. Reads
    `snapshot.payload["category_spending"]` directly through its own
    separately hand-written loop, instead of calling
    `report_categories_publication_facts` -- mirrors
    `_dashboard_category_spending_engine_truth`; see that function's
    docstring for why this is not shared code.
    """

    fields: dict[str, Decimal] = {}
    for row in snapshot.payload.get("category_spending", []):
        category_key = str(row.get("category") or "unidentified")
        fields[f"categories.{category_key}.amount"] = money(Decimal(str(row.get("amount", 0))))
    return fields


def report_accounts_publication_facts(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """Flatten `account_cash_flow_rows(snapshot)` into `accounts.<account_id>.<metric>`
    facts for `report_values` -- the per-account monetary amounts `/reports`
    aggregates into its `accounts` array. Mirrors
    `report_categories_publication_facts` above for the same reason (PR 7,
    Round 7): `reports()` now sums this exact function's rows across its
    window instead of reading `snapshot.payload["cash_flow_by_account"]`
    directly.
    """

    fields: dict[str, Decimal] = {}
    for row in account_cash_flow_rows(snapshot):
        account_key = str(row.get("account_id") or "unidentified")
        for metric in _ACCOUNT_CASH_FLOW_METRICS:
            fields[f"accounts.{account_key}.{metric}"] = money(Decimal(str(row.get(metric, 0))))
    return fields


def _report_accounts_engine_truth(snapshot: FinancialSnapshot) -> dict[str, Decimal]:
    """INV-020's independent "expected" side for `accounts`. Reads
    `snapshot.payload["cash_flow_by_account"]` directly, instead of calling
    `report_accounts_publication_facts` -- mirrors
    `_dashboard_account_cash_flow_engine_truth`; see that function's
    docstring for why this is not shared code.
    """

    fields: dict[str, Decimal] = {}
    for row in snapshot.payload.get("cash_flow_by_account", []):
        account_key = str(row.get("account_id") or "unidentified")
        for metric in _ACCOUNT_CASH_FLOW_METRICS:
            fields[f"accounts.{account_key}.{metric}"] = money(Decimal(str(row.get(metric, 0))))
    return fields


def dashboard_and_report_consistency_facts(
    snapshot: FinancialSnapshot, *, profile: FinancialProfile
) -> dict[str, dict[str, dict[str, Decimal] | Decimal]]:
    """Derive INV-019/INV-020 facts (`trusted_for_reports`) for `snapshot`.

    Returns one independent fact set per invariant (`"dashboard"` for
    INV-019, `"report"` for INV-020) instead of one shared
    `financial_engine_values`, because `/dashboard` and `/reports` do not
    publish exactly the same field set for a period: `/dashboard` adds
    `investment_balance` (a live `FinancialProfile` balance, not a snapshot
    column) and a one-month `/reports` window adds `savings_rate`.

    `dashboard_values`/`report_values` come from
    `dashboard_monetary_publication`/`report_month_monetary_publication` --
    the exact functions `app/api.py`'s `dashboard()`/`reports()` call to
    build the monetary keys of their own responses, verbatim, with no
    per-field logic of their own left in the endpoint (see those functions'
    docstrings), plus `cash_flow_by_account.<account_id>.<metric>` facts
    flattened from the exact per-account rows `/dashboard` also publishes
    verbatim (`_dashboard_account_cash_flow_publication_facts`).
    `financial_engine_values` comes from
    `_dashboard_financial_engine_truth`/`_report_financial_engine_truth`
    (plus `_dashboard_account_cash_flow_engine_truth` for the per-account
    facts), which read the snapshot/profile columns directly and never call
    the publication functions, so a regression in either publication mapping
    is real evidence of a genuine divergence, not an artifact of comparing a
    value to itself. See `tests/test_financial_snapshots.py` for regressions
    that mutate each publication function (post-calculation, publication-only
    mutations included) and prove both the live endpoint and the invariant
    diverge together.

    `report_values` (PR 7, Round 7) also folds in the *complete* one-month
    `summary` contract via `report_summary_monetary_publication()` -- the
    exact function `reports()` now spreads verbatim into `summary`, covering
    every total/average/liquidity field, not `savings_rate` alone -- passing
    in `snapshot`'s own columns and `profile.emergency_floor` as the
    one-month window's inputs; see that function's docstring for why this
    reduces to `reports()`'s own per-window totals for `months=1`, matching
    a monthly close's single closed period.
    `financial_engine_values`'s matching keys stay independent
    (`_report_summary_engine_truth`, reading `snapshot`/`profile` columns
    directly, never `report_summary_monetary_publication`).

    `dashboard_values`/`financial_engine_values` also each fold in
    `category_spending.<category>.amount` facts (PR 7, Round 6), flattened
    from the exact per-category rows `/dashboard` publishes verbatim under
    `category_spending` (`_dashboard_category_spending_publication_facts`),
    the same pattern as `cash_flow_by_account.<account_id>.<metric>` above.
    `report_values`/`financial_engine_values` (PR 7, Round 7) mirror this
    for `/reports`' own `categories`/`accounts` arrays
    (`report_categories_publication_facts`/`report_accounts_publication_facts`
    on the publication side, `_report_categories_engine_truth`/
    `_report_accounts_engine_truth` on the independent side).
    """

    from app.services.financial_invariants import MONEY_TOLERANCE

    report_publication = report_month_monetary_publication(snapshot)
    report_summary_publication = report_summary_monetary_publication(
        total_spending=report_publication["spending"],
        average_spending=report_publication["spending"],
        total_cash_in=report_publication["cash_in"],
        total_cash_out=report_publication["cash_out"],
        total_bank_cash_out=report_publication["bank_cash_out"],
        total_card_spending=report_publication["card_spending"],
        cash_net=report_publication["cash_net"],
        liquidity_starting_balance=money(snapshot.opening_liquidity_balance),
        liquidity_balance=money(snapshot.closing_liquidity_balance),
        emergency_floor=money(profile.emergency_floor),
        liquidity_available=money(snapshot.distance_to_floor),
        liquidity_deposit=money(Decimal(str(snapshot.payload["liquidity_deposit"]))),
        liquidity_withdrawal=money(snapshot.liquidity_used),
        liquidity_uncovered_deficit=money(snapshot.closing_uncovered_deficit),
    )
    return {
        "dashboard": {
            "financial_engine_values": {
                **_dashboard_financial_engine_truth(snapshot, profile=profile),
                **_dashboard_account_cash_flow_engine_truth(snapshot),
                **_dashboard_category_spending_engine_truth(snapshot),
            },
            "dashboard_values": {
                **dashboard_monetary_publication(snapshot, profile=profile),
                **_dashboard_account_cash_flow_publication_facts(snapshot),
                **_dashboard_category_spending_publication_facts(snapshot),
            },
            "monetary_tolerance": MONEY_TOLERANCE,
        },
        "report": {
            "financial_engine_values": {
                **_report_financial_engine_truth(snapshot),
                **_report_summary_engine_truth(snapshot, profile=profile),
                **_report_categories_engine_truth(snapshot),
                **_report_accounts_engine_truth(snapshot),
            },
            "report_values": {
                **report_publication,
                **report_summary_publication,
                **report_categories_publication_facts(snapshot),
                **report_accounts_publication_facts(snapshot),
            },
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
