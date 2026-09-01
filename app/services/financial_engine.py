"""Deterministic Financial Engine: builds the canonical `FinancialSnapshot`.

PR 4 (docs/work-orders/PR4_CANONICAL_FINANCIAL_SNAPSHOT.md). This module owns
exactly one responsibility: turning the household's already-canonical sources
(transactions, categories, accounts, confirmed balance observations, and the
previous snapshot) into one auditable, immutable `FinancialSnapshot` row plus
its `FinancialSnapshotLineage` trail.

Design decisions worth a reviewer's attention (see the PR description for the
full rationale and evidence):

* Movement classification (`classify_movement`) reads only fields the importer
  and `app.services.duplicates` already populate -- `transaction_type`,
  `Category.name` (`Conciliação` / `Transferência patrimonial` /
  `Transferência interna`), `canonical_status` and `excluded`. No new
  heuristic or second precedence policy is introduced.
* `budget_usage` equals `operating_expenses` in this slice: both are driven by
  the same real-economic-expense aggregate, and FINANCIAL_INVARIANTS.md's
  INV-001..INV-004 already treat them as the same zero-effect set for
  transfers/card payments/investments. A separate per-category cash-cap
  breakdown (the "Plano de cortes" report) is untouched and out of scope.
* Probable-band (non-strong) duplicates stay counted, matching the documented
  and already-implemented precedent in docs/FINANCIAL_RULES.md and
  `app.services.duplicates.register_transaction_duplicates` (only a `strong`
  match flips `canonical_status` to `supporting`). INV-014 is intentionally
  NOT re-derived here: `financial_integrity.build_baseline_checks` already
  proves it from live data; running a second, engine-local duplicate policy
  would be exactly the "segunda política de precedência" the Work Order
  forbids.
* `investment_yield` (docs/FINANCIAL_RULES.md "Conta central de liquidez") is
  folded into the `monthly_result` fed to `calculate_liquidity_transition`,
  because that documented rule is a *current*, normative rule (not a
  discovery-phase note) and omitting it would make `closing_liquidity_balance`
  wrong.
* Full carry-forward of a prior `uncovered_deficit` (future surplus pays it
  down first -- INTEGRITY_IMPLEMENTATION_PLAN.md §7.3) is NOT implemented as
  an algorithmic offset here. `opening_uncovered_deficit` is persisted
  (chained from the previous snapshot) for transparency only; the transition
  itself uses the plain, already-tested `calculate_liquidity_transition`
  formula, matching INV-005/INV-006/INV-007 and the Work Order's literal
  "Regras financeiras obrigatórias" verbatim. Feeding a carry-forward-adjusted
  result into those frozen validators would make them fail on a mismatch that
  is an engine/validator disagreement, not a real defect -- implementing the
  offset for real means extending that tested contract and bumping
  `financial_rules_version`, which is a deliberate follow-up decision, not
  something to slip in silently.
* Opening liquidity balance is resolved only from a confirmed
  `AccountBalanceObservation` for `financial_profiles.central_liquidity_account_id`
  (new additive column) or chained from the previous period's own snapshot.
  `FinancialProfile.investment_balance` is never used as a silent fallback --
  it has no reliable `as_of_date` (docs/INTEGRITY_IMPLEMENTATION_PLAN.md §7.4).
  Missing evidence yields `integrity_status="incomplete"` with null liquidity
  fields, never a fabricated number.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.services.finance import monthly_net_rate
from app.services.financial_integrity import IntegrityCheck
from app.services.financial_invariants import (
    FINANCIAL_RULES_VERSION,
    InvariantContext,
    InvariantScope,
    calculate_liquidity_transition,
)

if TYPE_CHECKING:
    from app.models import FinancialProfile, FinancialSnapshot, FinancialSnapshotLineage

FINANCIAL_ENGINE_CALCULATION_VERSION = "2026.09.1-pr4-snapshot"

CARD_ACCOUNT_TYPE = "credit_card"
INVESTMENT_MOVEMENT_CATEGORY = "Transferência patrimonial"

CENT = Decimal("0.01")

MOVEMENT_ROLES = frozenset(
    {
        "operating_income",
        "operating_expense",
        "refund",
        "internal_transfer",
        "investment_application",
        "investment_redemption",
        "card_payment",
        "excluded_non_canonical",
        "excluded_manual",
        "unclassified",
    }
)

EXCLUDED_ROLES = frozenset({"excluded_non_canonical", "excluded_manual"})


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Pure classification and aggregation -- no DB, no FastAPI, no Advisor.
# ---------------------------------------------------------------------------


def classify_movement(
    *,
    transaction_type: str,
    amount: Decimal,
    category_name: str,
    canonical_status: str,
    excluded: bool,
) -> str:
    """Assign exactly one deterministic role to a transaction.

    A row proven non-canonical by the PR 3 duplicate engine (`canonical_status
    == "supporting"`) is excluded regardless of its `transaction_type`. Only
    after that gate do we read `transaction_type`/`category_name` to tell
    transfer-shaped movements apart, using the exact category vocabulary
    `app.services.plan_workbook._classification` already assigns at import
    time (`Conciliação`, `Transferência patrimonial`, `Transferência
    interna`).
    """

    if canonical_status == "supporting":
        return "excluded_non_canonical"
    if transaction_type == "transfer":
        if category_name == INVESTMENT_MOVEMENT_CATEGORY:
            return "investment_application" if amount < 0 else "investment_redemption"
        return "internal_transfer"
    if transaction_type == "reconciliation":
        return "card_payment"
    if transaction_type == "refund":
        return "refund"
    if transaction_type in {"income", "expense"}:
        if excluded:
            return "excluded_manual"
        return "operating_income" if transaction_type == "income" else "operating_expense"
    return "unclassified"


@dataclass(frozen=True, slots=True)
class MovementFact:
    """One transaction reduced to exactly what the engine aggregates or traces."""

    transaction_id: str
    amount: Decimal
    role: str
    account_type: str
    account_id: str | None
    category_id: str | None
    document_id: str | None
    competence: str


@dataclass(frozen=True, slots=True)
class SnapshotTotals:
    operating_income: Decimal
    operating_expenses: Decimal
    operating_result: Decimal
    budget_usage: Decimal
    bank_cash_in: Decimal
    bank_cash_out: Decimal
    bank_cash_result: Decimal
    card_spend: Decimal
    card_payments: Decimal
    investments: Decimal
    redemptions: Decimal
    internal_transfers: Decimal
    refunds: Decimal


def aggregate_movements(facts: Sequence[MovementFact]) -> SnapshotTotals:
    """Sum classified movements into the FinancialSnapshot's canonical fields.

    Refunds net against the expense channel they belong to (card vs bank) so
    `card_spend`/`bank_cash_out` and therefore `operating_expenses` already
    reflect INV-016 (estorno compensa despesa, sem virar receita). Transfers,
    investment movements and card payments never touch `operating_income`,
    `operating_expenses` or `budget_usage` (INV-001..INV-004).
    """

    card_expense_gross = Decimal("0.00")
    card_refunds = Decimal("0.00")
    bank_expense_gross = Decimal("0.00")
    bank_refunds = Decimal("0.00")
    operating_income = Decimal("0.00")
    bank_cash_in = Decimal("0.00")
    card_payments = Decimal("0.00")
    investments = Decimal("0.00")
    redemptions = Decimal("0.00")
    internal_transfers = Decimal("0.00")

    for fact in facts:
        amount = fact.amount
        is_card = fact.account_type == CARD_ACCOUNT_TYPE
        if fact.role == "operating_income":
            operating_income += amount
            if not is_card:
                bank_cash_in += amount
        elif fact.role == "operating_expense":
            if is_card:
                card_expense_gross += abs(amount)
            else:
                bank_expense_gross += abs(amount)
        elif fact.role == "refund":
            if is_card:
                card_refunds += amount
            else:
                bank_refunds += amount
        elif fact.role == "card_payment":
            # Only the bank-account leg paying down the statement is a real
            # bank cash outflow. A "payment received" line on the card
            # account itself (if a statement ever carries one) is descriptive
            # of the card's own balance, not a household bank movement.
            if not is_card and amount < 0:
                card_payments += abs(amount)
        elif fact.role == "investment_application":
            investments += abs(amount)
        elif fact.role == "investment_redemption":
            redemptions += amount
        elif fact.role == "internal_transfer":
            internal_transfers += abs(amount)
        # excluded_* / unclassified roles never contribute to any total.

    card_spend = _money(card_expense_gross - card_refunds)
    bank_direct_expense = _money(bank_expense_gross - bank_refunds)
    operating_expenses = _money(card_spend + bank_direct_expense)
    operating_income = _money(operating_income)
    bank_cash_in = _money(bank_cash_in)
    card_payments = _money(card_payments)
    bank_cash_out = _money(bank_direct_expense + card_payments)

    return SnapshotTotals(
        operating_income=operating_income,
        operating_expenses=operating_expenses,
        operating_result=_money(operating_income - operating_expenses),
        budget_usage=operating_expenses,
        bank_cash_in=bank_cash_in,
        bank_cash_out=bank_cash_out,
        bank_cash_result=_money(bank_cash_in - bank_cash_out),
        card_spend=card_spend,
        card_payments=card_payments,
        investments=_money(investments),
        redemptions=_money(redemptions),
        internal_transfers=_money(internal_transfers),
        refunds=_money(card_refunds + bank_refunds),
    )


@dataclass(frozen=True, slots=True)
class LiquiditySnapshot:
    investment_yield: Decimal | None
    monthly_result: Decimal | None
    liquidity_used: Decimal | None
    closing_balance: Decimal | None
    closing_uncovered_deficit: Decimal | None
    safety_floor: Decimal | None
    distance_to_floor: Decimal | None
    floor_breached: bool | None


def compute_liquidity(
    *,
    opening_balance: Decimal | None,
    operating_result: Decimal,
    monthly_investment_rate: Decimal,
    safety_floor: Decimal | None,
) -> LiquiditySnapshot:
    """Apply the canonical Privilège transition, with the documented yield.

    Reuses `calculate_liquidity_transition` (frozen/tested in PR 1) unchanged:
    `monthly_result = operating_result + investment_yield`, where the yield
    only accrues on a positive opening balance (docs/FINANCIAL_RULES.md "O
    rendimento mensal incide sobre o saldo inicial positivo do mês"), matching
    the same `max(0, opening) * rate` shape `app.services.finance.build_forecast`
    already uses for projections.
    """

    if opening_balance is None:
        return LiquiditySnapshot(
            investment_yield=None,
            monthly_result=None,
            liquidity_used=None,
            closing_balance=None,
            closing_uncovered_deficit=None,
            safety_floor=safety_floor,
            distance_to_floor=None,
            floor_breached=None,
        )

    investment_yield = _money(max(Decimal("0"), opening_balance) * monthly_investment_rate)
    monthly_result = _money(operating_result + investment_yield)
    transition = calculate_liquidity_transition(opening_balance, monthly_result)
    floor = safety_floor if safety_floor is not None else Decimal("0.00")
    return LiquiditySnapshot(
        investment_yield=investment_yield,
        monthly_result=monthly_result,
        liquidity_used=transition.liquidity_used,
        closing_balance=transition.closing_balance,
        closing_uncovered_deficit=transition.uncovered_deficit,
        safety_floor=safety_floor,
        distance_to_floor=_money(transition.closing_balance - floor),
        floor_breached=transition.closing_balance < floor,
    )


def _period_bounds(period: str) -> tuple[date, date]:
    _validate_period(period)
    year, month = (int(part) for part in period.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def _previous_period(period: str) -> str:
    _validate_period(period)
    year, month = (int(part) for part in period.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _validate_period(period: str) -> None:
    parts = period.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError("period must use YYYY-MM format")
    try:
        month = int(parts[1])
    except ValueError as exc:
        raise ValueError("period must use YYYY-MM format") from exc
    if not (1 <= month <= 12):
        raise ValueError("period must use YYYY-MM format")


# ---------------------------------------------------------------------------
# DB-facing construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinancialSnapshotBuild:
    household_id: str
    period: str
    snapshot_kind: str
    trace_id: str
    calculation_version: str
    financial_rules_version: str
    totals: SnapshotTotals
    liquidity: LiquiditySnapshot
    opening_liquidity_balance: Decimal | None
    opening_balance_source_type: str | None
    opening_balance_source_id: str | None
    opening_uncovered_deficit: Decimal
    integrity_status: str
    movement_facts: tuple[MovementFact, ...]
    generated_at: datetime


def build_financial_snapshot(
    db: Session,
    *,
    household_id: str,
    period: str,
    snapshot_kind: str = "actual",
    trace_id: str | None = None,
) -> FinancialSnapshotBuild:
    """Query the household's canonical sources and compute one snapshot.

    Selection uses `competence` (falling back to the `booked_at` month for
    legacy rows that predate it, exactly as `app.services.duplicates._competence`
    already does) rather than a raw `booked_at` range, so a card purchase
    booked in one calendar month but billed to another's statement is counted
    in its statement's competence (INV-017), not the purchase date's month.
    """

    from app.models import Account, Category, FinancialProfile, Transaction

    _validate_period(period)
    trace = trace_id or str(uuid.uuid4())
    start, end = _period_bounds(period)

    rows = db.execute(
        select(
            Transaction.id,
            Transaction.amount,
            Transaction.transaction_type,
            Transaction.canonical_status,
            Transaction.excluded,
            Transaction.competence,
            Transaction.booked_at,
            Transaction.account_id,
            Transaction.category_id,
            Transaction.document_id,
            Category.name,
            Account.account_type,
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            or_(
                Transaction.competence == period,
                and_(
                    Transaction.competence.is_(None),
                    Transaction.booked_at >= start,
                    Transaction.booked_at < end,
                ),
            ),
        )
    ).all()

    movement_facts = tuple(
        MovementFact(
            transaction_id=transaction_id,
            amount=_money(amount),
            role=classify_movement(
                transaction_type=transaction_type,
                amount=Decimal(amount),
                category_name=category_name or "Revisar",
                canonical_status=canonical_status,
                excluded=bool(excluded),
            ),
            account_type=account_type or "other",
            account_id=account_id,
            category_id=category_id,
            document_id=document_id,
            competence=competence or booked_at.strftime("%Y-%m"),
        )
        for (
            transaction_id,
            amount,
            transaction_type,
            canonical_status,
            excluded,
            competence,
            booked_at,
            account_id,
            category_id,
            document_id,
            category_name,
            account_type,
        ) in rows
    )

    totals = aggregate_movements(movement_facts)

    profile = db.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )
    monthly_rate = (
        monthly_net_rate(
            Decimal(profile.investment_gross_annual_rate),
            Decimal(profile.investment_income_tax_rate),
        )
        if profile is not None
        else Decimal("0")
    )
    safety_floor = _money(profile.emergency_floor) if profile is not None else None

    (
        opening_balance,
        opening_source_type,
        opening_source_id,
        opening_uncovered_deficit,
    ) = _resolve_opening_liquidity(db, household_id=household_id, profile=profile, period=period, start=start)

    liquidity = compute_liquidity(
        opening_balance=opening_balance,
        operating_result=totals.operating_result,
        monthly_investment_rate=monthly_rate,
        safety_floor=safety_floor,
    )

    return FinancialSnapshotBuild(
        household_id=household_id,
        period=period,
        snapshot_kind=snapshot_kind,
        trace_id=trace,
        calculation_version=FINANCIAL_ENGINE_CALCULATION_VERSION,
        financial_rules_version=FINANCIAL_RULES_VERSION,
        totals=totals,
        liquidity=liquidity,
        opening_liquidity_balance=opening_balance,
        opening_balance_source_type=opening_source_type,
        opening_balance_source_id=opening_source_id,
        opening_uncovered_deficit=opening_uncovered_deficit,
        integrity_status="complete" if opening_balance is not None else "incomplete",
        movement_facts=movement_facts,
        generated_at=datetime.now(UTC),
    )


def _resolve_opening_liquidity(
    db: Session,
    *,
    household_id: str,
    profile: FinancialProfile | None,
    period: str,
    start: date,
) -> tuple[Decimal | None, str | None, str | None, Decimal]:
    """Resolve the period's opening Privilège balance without fabricating one.

    A confirmed `AccountBalanceObservation` only re-anchors the opening
    balance when it actually describes *this exact point in time* -- dated
    `start` (an "opening" reading for this period) or the day before (a
    "closing" reading for the previous period, the same instant under a
    different label). An older observation is real evidence about an earlier
    balance, not about this period's opening: reusing it here would silently
    ignore every transaction between that date and `start`, which the engine
    has already accounted for via the snapshot chain. So priority is:
    (1) a confirmed, non-invalidated, non-superseded observation for the
    household's designated central liquidity account dated exactly at that
    boundary; (2) the previous period's own `current` snapshot's
    `closing_liquidity_balance`, chaining the engine forward; (3) unknown.
    `FinancialProfile.investment_balance` is never consulted here
    (docs/INTEGRITY_IMPLEMENTATION_PLAN.md §7.4).
    """

    from datetime import timedelta

    from app.models import AccountBalanceObservation
    from app.models import FinancialSnapshot as FinancialSnapshotModel

    if profile is not None and profile.central_liquidity_account_id:
        observation = db.scalar(
            select(AccountBalanceObservation)
            .where(
                AccountBalanceObservation.household_id == household_id,
                AccountBalanceObservation.account_id == profile.central_liquidity_account_id,
                AccountBalanceObservation.as_of_date.in_((start, start - timedelta(days=1))),
                AccountBalanceObservation.invalidated_at.is_(None),
                AccountBalanceObservation.superseded_by_id.is_(None),
            )
            .order_by(
                AccountBalanceObservation.as_of_date.desc(),
                AccountBalanceObservation.created_at.desc(),
            )
            .limit(1)
        )
        if observation is not None:
            return _money(observation.amount), "observation", observation.id, Decimal("0.00")

    previous_snapshot = db.scalar(
        select(FinancialSnapshotModel)
        .where(
            FinancialSnapshotModel.household_id == household_id,
            FinancialSnapshotModel.period == _previous_period(period),
            FinancialSnapshotModel.snapshot_kind == "actual",
            FinancialSnapshotModel.status == "current",
        )
        .limit(1)
    )
    if previous_snapshot is not None and previous_snapshot.closing_liquidity_balance is not None:
        carried_deficit = previous_snapshot.closing_uncovered_deficit or Decimal("0.00")
        return (
            _money(previous_snapshot.closing_liquidity_balance),
            "previous_snapshot",
            previous_snapshot.id,
            _money(carried_deficit),
        )

    return None, None, None, Decimal("0.00")


# ---------------------------------------------------------------------------
# Persistence: immutable snapshot + lineage, previous version superseded.
# ---------------------------------------------------------------------------


def persist_financial_snapshot(
    db: Session,
    build: FinancialSnapshotBuild,
    *,
    created_by: str | None = None,
) -> FinancialSnapshot:
    from app.models import FinancialSnapshot as FinancialSnapshotModel

    previous = db.scalar(
        select(FinancialSnapshotModel).where(
            FinancialSnapshotModel.household_id == build.household_id,
            FinancialSnapshotModel.period == build.period,
            FinancialSnapshotModel.snapshot_kind == build.snapshot_kind,
            FinancialSnapshotModel.status == "current",
        )
    )
    next_version = (previous.version + 1) if previous is not None else 1
    checksum = _checksum(build)

    with db.begin_nested():
        snapshot = FinancialSnapshotModel(
            household_id=build.household_id,
            period=build.period,
            snapshot_kind=build.snapshot_kind,
            version=next_version,
            status="current",
            integrity_status=build.integrity_status,
            financial_rules_version=build.financial_rules_version,
            calculation_version=build.calculation_version,
            trace_id=build.trace_id,
            checksum=checksum,
            operating_income=build.totals.operating_income,
            operating_expenses=build.totals.operating_expenses,
            operating_result=build.totals.operating_result,
            budget_usage=build.totals.budget_usage,
            bank_cash_in=build.totals.bank_cash_in,
            bank_cash_out=build.totals.bank_cash_out,
            bank_cash_result=build.totals.bank_cash_result,
            card_spend=build.totals.card_spend,
            card_payments=build.totals.card_payments,
            investments=build.totals.investments,
            redemptions=build.totals.redemptions,
            internal_transfers=build.totals.internal_transfers,
            refunds=build.totals.refunds,
            commitments=Decimal("0.00"),
            projected_balance=None,
            opening_liquidity_balance=build.opening_liquidity_balance,
            investment_yield=build.liquidity.investment_yield,
            liquidity_used=build.liquidity.liquidity_used,
            closing_liquidity_balance=build.liquidity.closing_balance,
            opening_uncovered_deficit=build.opening_uncovered_deficit,
            closing_uncovered_deficit=build.liquidity.closing_uncovered_deficit,
            safety_floor=build.liquidity.safety_floor,
            distance_to_floor=build.liquidity.distance_to_floor,
            floor_breached=build.liquidity.floor_breached,
            opening_balance_source_type=build.opening_balance_source_type,
            opening_balance_source_id=build.opening_balance_source_id,
            source_count=len({fact.transaction_id for fact in build.movement_facts}),
            supersedes_id=previous.id if previous is not None else None,
            generated_at=build.generated_at,
            created_by=created_by,
        )
        db.add(snapshot)
        db.flush()

        if previous is not None:
            previous.status = "superseded"
            previous.superseded_by_id = snapshot.id

        for lineage_row in _build_lineage_rows(build, snapshot.id):
            db.add(lineage_row)
        db.flush()

    return snapshot


def _checksum(build: FinancialSnapshotBuild) -> str:
    payload = {
        "household_id": build.household_id,
        "period": build.period,
        "snapshot_kind": build.snapshot_kind,
        "calculation_version": build.calculation_version,
        "financial_rules_version": build.financial_rules_version,
        "opening_liquidity_balance": _optional_str(build.opening_liquidity_balance),
        "opening_uncovered_deficit": str(build.opening_uncovered_deficit),
        "totals": {key: str(value) for key, value in asdict(build.totals).items()},
        "liquidity": {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(build.liquidity).items()
        },
        "source_count": len({fact.transaction_id for fact in build.movement_facts}),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _lineage_entries(fact: MovementFact) -> tuple[tuple[str, Decimal | None, str], ...]:
    """Return the `(metric_key, contribution, source_role)` rows one movement feeds."""

    is_card = fact.account_type == CARD_ACCOUNT_TYPE
    if fact.role == "operating_income":
        entries: list[tuple[str, Decimal | None, str]] = [("operating_income", fact.amount, "canonical")]
        if not is_card:
            entries.append(("bank_cash_in", fact.amount, "canonical"))
        return tuple(entries)
    if fact.role == "operating_expense":
        magnitude = abs(fact.amount)
        channel = "card_spend" if is_card else "bank_cash_out"
        return ((channel, magnitude, "canonical"), ("operating_expenses", magnitude, "canonical"))
    if fact.role == "refund":
        return (
            ("refunds", fact.amount, "canonical"),
            ("operating_expenses", _money(-fact.amount), "canonical"),
        )
    if fact.role == "card_payment":
        if not is_card and fact.amount < 0:
            magnitude = abs(fact.amount)
            return (
                ("card_payments", magnitude, "canonical"),
                ("bank_cash_out", magnitude, "canonical"),
            )
        return (("card_payment_reference", None, "excluded"),)
    if fact.role == "investment_application":
        return (("investments", abs(fact.amount), "canonical"),)
    if fact.role == "investment_redemption":
        return (("redemptions", fact.amount, "canonical"),)
    if fact.role == "internal_transfer":
        return (("internal_transfers", abs(fact.amount), "canonical"),)
    if fact.role in EXCLUDED_ROLES:
        source_role = "supporting" if fact.role == "excluded_non_canonical" else "excluded"
        return ((fact.role, None, source_role),)
    return (("unclassified", None, "excluded"),)


def _build_lineage_rows(
    build: FinancialSnapshotBuild, snapshot_id: str
) -> list[FinancialSnapshotLineage]:
    from app.models import FinancialSnapshotLineage as LineageModel

    rows: list[LineageModel] = []
    for fact in build.movement_facts:
        for metric_key, contribution, source_role in _lineage_entries(fact):
            rows.append(
                LineageModel(
                    household_id=build.household_id,
                    snapshot_id=snapshot_id,
                    metric_key=metric_key,
                    entity_type="transaction",
                    entity_id=fact.transaction_id,
                    document_id=fact.document_id,
                    rule_id=f"movement_role:{fact.role}",
                    contribution=_money(contribution) if contribution is not None else None,
                    source_role=source_role,
                    trace_id=build.trace_id,
                )
            )

    if build.opening_balance_source_type == "observation":
        rows.append(
            LineageModel(
                household_id=build.household_id,
                snapshot_id=snapshot_id,
                metric_key="opening_liquidity_balance",
                entity_type="account_balance_observation",
                entity_id=build.opening_balance_source_id,
                document_id=None,
                rule_id="liquidity:confirmed_observation",
                contribution=build.opening_liquidity_balance,
                source_role="reconciled",
                trace_id=build.trace_id,
            )
        )
    elif build.opening_balance_source_type == "previous_snapshot":
        rows.append(
            LineageModel(
                household_id=build.household_id,
                snapshot_id=snapshot_id,
                metric_key="opening_liquidity_balance",
                entity_type="financial_snapshot",
                entity_id=build.opening_balance_source_id,
                document_id=None,
                rule_id="liquidity:previous_snapshot_closing_balance",
                contribution=build.opening_liquidity_balance,
                source_role="canonical",
                trace_id=build.trace_id,
            )
        )
    return rows


def _optional_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Serialization for the API layer.
# ---------------------------------------------------------------------------


def serialize_financial_snapshot(snapshot: FinancialSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "household_id": snapshot.household_id,
        "period": snapshot.period,
        "snapshot_kind": snapshot.snapshot_kind,
        "version": snapshot.version,
        "status": snapshot.status,
        "integrity_status": snapshot.integrity_status,
        "financial_rules_version": snapshot.financial_rules_version,
        "calculation_version": snapshot.calculation_version,
        "trace_id": snapshot.trace_id,
        "checksum": snapshot.checksum,
        "operating_income": str(snapshot.operating_income),
        "operating_expenses": str(snapshot.operating_expenses),
        "operating_result": str(snapshot.operating_result),
        "budget_usage": str(snapshot.budget_usage),
        "bank_cash_in": str(snapshot.bank_cash_in),
        "bank_cash_out": str(snapshot.bank_cash_out),
        "bank_cash_result": str(snapshot.bank_cash_result),
        "card_spend": str(snapshot.card_spend),
        "card_payments": str(snapshot.card_payments),
        "investments": str(snapshot.investments),
        "redemptions": str(snapshot.redemptions),
        "internal_transfers": str(snapshot.internal_transfers),
        "refunds": str(snapshot.refunds),
        "commitments": str(snapshot.commitments),
        "projected_balance": _optional_str(snapshot.projected_balance),
        "opening_liquidity_balance": _optional_str(snapshot.opening_liquidity_balance),
        "investment_yield": _optional_str(snapshot.investment_yield),
        "liquidity_used": _optional_str(snapshot.liquidity_used),
        "closing_liquidity_balance": _optional_str(snapshot.closing_liquidity_balance),
        "opening_uncovered_deficit": _optional_str(snapshot.opening_uncovered_deficit),
        "closing_uncovered_deficit": _optional_str(snapshot.closing_uncovered_deficit),
        "safety_floor": _optional_str(snapshot.safety_floor),
        "distance_to_floor": _optional_str(snapshot.distance_to_floor),
        "floor_breached": snapshot.floor_breached,
        "opening_balance_source_type": snapshot.opening_balance_source_type,
        "opening_balance_source_id": snapshot.opening_balance_source_id,
        "source_count": snapshot.source_count,
        "supersedes_id": snapshot.supersedes_id,
        "superseded_by_id": snapshot.superseded_by_id,
        "generated_at": snapshot.generated_at.isoformat() if snapshot.generated_at else None,
        "created_by": snapshot.created_by,
    }


def serialize_lineage(rows: Sequence[FinancialSnapshotLineage]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "snapshot_id": row.snapshot_id,
            "metric_key": row.metric_key,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "document_id": row.document_id,
            "rule_id": row.rule_id,
            "contribution": _optional_str(row.contribution),
            "source_role": row.source_role,
            "trace_id": row.trace_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Integrity checks provable directly from a computed snapshot.
# ---------------------------------------------------------------------------


def _lineage_metric_totals(build: FinancialSnapshotBuild) -> dict[str, dict[str, Any]]:
    role_to_metric = {"operating_income": "operating_income", "operating_expense": "operating_expenses"}
    metrics: dict[str, dict[str, list[str] | set[str]]] = {}
    for fact in build.movement_facts:
        metric = role_to_metric.get(fact.role)
        if metric is None:
            continue
        info = metrics.setdefault(
            metric,
            {"transaction_ids": [], "document_ids": [], "account_ids": [], "category_ids": [], "rule_ids": set()},
        )
        info["transaction_ids"].append(fact.transaction_id)  # type: ignore[union-attr]
        if fact.document_id:
            info["document_ids"].append(fact.document_id)  # type: ignore[union-attr]
        if fact.account_id:
            info["account_ids"].append(fact.account_id)  # type: ignore[union-attr]
        if fact.category_id:
            info["category_ids"].append(fact.category_id)  # type: ignore[union-attr]
        info["rule_ids"].add(f"movement_role:{fact.role}")  # type: ignore[union-attr]

    result: dict[str, dict[str, Any]] = {}
    for metric, info in metrics.items():
        transaction_ids = tuple(dict.fromkeys(info["transaction_ids"]))
        result[metric] = {
            "source_count": len(transaction_ids),
            "transaction_ids": transaction_ids,
            "document_ids": tuple(dict.fromkeys(info["document_ids"])),
            "account_ids": tuple(dict.fromkeys(info["account_ids"])),
            "category_ids": tuple(dict.fromkeys(info["category_ids"])),
            "rule_ids": tuple(info["rule_ids"]),
        }
    return result


def build_snapshot_invariant_checks(build: FinancialSnapshotBuild) -> tuple[IntegrityCheck, ...]:
    """Deterministic checks provable directly from this computed snapshot.

    INV-014 is intentionally not rebuilt here -- see the module docstring.
    INV-013 (VA/VR) has no transaction-level representation in this schema
    (benefits are static `FinancialProfile` configuration, not movements), so
    it is not applicable to a transaction-driven snapshot and is left out
    rather than evaluated against facts that do not exist. INV-015 depends on
    the same PR 3 duplicate engine as INV-014 and is not re-checked here
    either; see the PR's Technical Challenge about that engine's coverage
    versus the legacy `app.api._expense_signature` workbook-precedence
    heuristic.
    """

    zero = Decimal("0.00")
    checks: list[IntegrityCheck] = []

    for fact in build.movement_facts:
        is_card = fact.account_type == CARD_ACCOUNT_TYPE
        if fact.role == "internal_transfer":
            checks.append(
                IntegrityCheck(
                    "INV-001",
                    InvariantContext(
                        facts={
                            "operating_income_effect": zero,
                            "operating_expense_effect": zero,
                            "budget_usage_effect": zero,
                            "operating_result_effect": zero,
                            "family_cash_effect": zero,
                        },
                        scope=InvariantScope.TRANSACTION,
                        entity_type="transaction",
                        entity_id=fact.transaction_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )
        elif fact.role == "card_payment" and not is_card and fact.amount < 0:
            checks.append(
                IntegrityCheck(
                    "INV-002",
                    InvariantContext(
                        facts={
                            "operating_income_effect": zero,
                            "operating_expense_effect": zero,
                            "budget_usage_effect": zero,
                            "operating_result_effect": zero,
                        },
                        scope=InvariantScope.TRANSACTION,
                        entity_type="transaction",
                        entity_id=fact.transaction_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )
        elif fact.role in {"investment_application", "investment_redemption"}:
            invariant_id = "INV-003" if fact.role == "investment_application" else "INV-004"
            checks.append(
                IntegrityCheck(
                    invariant_id,
                    InvariantContext(
                        facts={
                            "operating_income_effect": zero,
                            "operating_expense_effect": zero,
                            "budget_usage_effect": zero,
                            "operating_result_effect": zero,
                            "net_worth_effect": zero,
                        },
                        scope=InvariantScope.TRANSACTION,
                        entity_type="transaction",
                        entity_id=fact.transaction_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )
        elif fact.role == "refund":
            checks.append(
                IntegrityCheck(
                    "INV-016",
                    InvariantContext(
                        facts={
                            "refund_amount": abs(fact.amount),
                            "expense_reduction": abs(fact.amount),
                            "operating_income_effect": zero,
                        },
                        scope=InvariantScope.TRANSACTION,
                        entity_type="transaction",
                        entity_id=fact.transaction_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )
        elif fact.role == "operating_expense" and is_card:
            checks.append(
                IntegrityCheck(
                    "INV-017",
                    InvariantContext(
                        facts={
                            "canonical_competence": build.period,
                            "counted_competence": fact.competence,
                        },
                        scope=InvariantScope.TRANSACTION,
                        entity_type="transaction",
                        entity_id=fact.transaction_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )

    if build.liquidity.closing_balance is not None:
        liquidity_facts: dict[str, object] = {
            "opening_liquidity_balance": build.opening_liquidity_balance,
            "monthly_operating_result": build.liquidity.monthly_result,
            "closing_liquidity_balance": build.liquidity.closing_balance,
            "uncovered_deficit": build.liquidity.closing_uncovered_deficit,
            "liquidity_used": build.liquidity.liquidity_used,
        }
        entity_id = f"{build.period}:{build.snapshot_kind}"
        for invariant_id in ("INV-005", "INV-006"):
            checks.append(
                IntegrityCheck(
                    invariant_id,
                    InvariantContext(
                        facts=dict(liquidity_facts),
                        scope=InvariantScope.PERIOD,
                        entity_type="financial_snapshot",
                        entity_id=entity_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )
        if build.liquidity.safety_floor is not None:
            checks.append(
                IntegrityCheck(
                    "INV-007",
                    InvariantContext(
                        facts={**liquidity_facts, "safety_floor": build.liquidity.safety_floor},
                        scope=InvariantScope.PERIOD,
                        entity_type="financial_snapshot",
                        entity_id=entity_id,
                        period=build.period,
                        trace_id=build.trace_id,
                    ),
                )
            )

    for metric_key, info in _lineage_metric_totals(build).items():
        if info["source_count"] <= 0:
            continue
        checks.append(
            IntegrityCheck(
                "INV-022",
                InvariantContext(
                    facts={
                        "source_count": info["source_count"],
                        "source_ids": info["transaction_ids"],
                        "transaction_ids": info["transaction_ids"],
                        "document_ids": info["document_ids"],
                        "document_required": False,
                        "rule_ids": info["rule_ids"],
                        "account_ids": info["account_ids"],
                        "category_ids": info["category_ids"],
                        "calculation_version": build.calculation_version,
                        "lineage_period": build.period,
                    },
                    scope=InvariantScope.PERIOD,
                    entity_type="financial_snapshot",
                    entity_id=f"{metric_key}:{build.period}",
                    period=build.period,
                    trace_id=build.trace_id,
                ),
            )
        )

    return tuple(checks)
