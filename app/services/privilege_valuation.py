"""Deterministic daily fund valuation from official CVM quotas (issue #85,
`docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`).

Two responsibilities, kept deliberately separate:

1. `ingest_latest_quote` -- fetch (via `app.services.cvm_client.CvmClient`)
   and idempotently persist the fund's latest official quota into the
   global `FundReferenceQuote` table. Global, not household-scoped: the
   quota CVM publishes for a fund/date is the same fact for every household
   holding that fund.
2. `run_valuation_for_household` -- combine the latest ingested quote with a
   household's latest `FundUnitPosition` evidence into one immutable,
   reproducible `FundValuation` snapshot.

**No LLM/Codex/Claude authority over any number here** -- this module is
pure deterministic arithmetic over already-persisted evidence, exactly the
Work Order's "Financial architecture" requirement.

**Never fabricates evidence.** `run_valuation_for_household` only *reads*
`FundUnitPosition` (the latest non-invalidated row); it never creates,
infers, or adjusts one. A household with no recorded position evidence
yet gets `FundValuationOutcome(status="no_position_evidence")` -- never a
guessed `units_held`. Application/redemption events, and the initial
units-held bootstrap, are written exclusively via
`record_position_snapshot`/`record_position_movement` below, each requiring
explicit evidence, never invented by this module or by the daily worker.

**Never touches `AccountBalanceObservation`, `Investment`, or
`InvestmentValuation`.** See `app.models.FundValuation`'s docstring for why
this is a deliberately scoped, additive, display/reconciliation-only slice:
the confirmed Privilège balance stays exclusively governed by
`AccountBalanceObservation` (rebaseline INV-024, "saldo confirmado
soberano"); this module's `observed_balance`/`reconciliation_diff` fields
are a read-only copy for display and audit, supplied by the caller (which
already has access to the confirmed observation via `app.api`'s
`_privilege_account`/`_latest_active_balance_observation`), never written
back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, AccountBalanceObservation, FundReferenceQuote, FundUnitPosition, FundValuation
from app.services.classifier import normalize_description
from app.services.cvm_client import CvmClient, CvmResult, normalize_fund_cnpj
from app.services.finance import money

VALUATION_VERSION = "v1"

# Issue #85 scopes this slice to exactly one fund -- "Do not anticipate...
# a second financial engine or unrelated refactor". A future fund would add
# a row to a small tracked-funds list/table, not change this constant's
# shape; not built preemptively now. Shared by `app.api` (read routes) and
# `app.cli.cvm_valuation_worker` (the daily ingestion pass) so both always
# agree on which fund this slice covers.
TRACKED_FUND_CNPJ = normalize_fund_cnpj("26.199.519/0001-34")  # Itaú Privilège RF Referenciado DI

POSITION_EVIDENCE_SNAPSHOT_TYPES = ("statement_position", "confirmed_balance_reconciliation")
POSITION_EVIDENCE_MOVEMENT_TYPES = ("application", "redemption")
POSITION_EVIDENCE_TYPES = POSITION_EVIDENCE_SNAPSHOT_TYPES + POSITION_EVIDENCE_MOVEMENT_TYPES

PERCENT_QUANTUM = Decimal("0.000001")


class PrivilegeValuationError(ValueError):
    """Raised for a caller-input error (invalid evidence request) -- never
    for a CVM/network failure, which is always reported through a result
    dataclass instead, never an exception (matches `CvmClient`)."""


@dataclass(frozen=True)
class FundValuationOutcome:
    status: str  # "ok" | "already_valued" | "no_quote_available" | "no_position_evidence"
    valuation: FundValuation | None = None
    detail: str | None = None


def resolve_privilege_account(db: Session, *, household_id: str) -> Account | None:
    """Best-effort resolution of the household's Privilège DI `Account`,
    for stamping `FundValuation.account_id` and reading the confirmed
    balance for display/reconciliation only -- deliberately never raises
    on ambiguity (unlike `app.api._privilege_account`, which 422s because
    its callers need a *definite* account to fund/redeem against). A
    valuation is still worth recording without a resolved account (it is
    still a real fact about the fund/household), so this returns `None`
    on "no investment account yet" or "ambiguous, more than one candidate"
    alike -- the caller (the daily worker) treats either as "no observed
    balance available for reconciliation this pass", never a fatal error.

    Deliberately duplicates `app.api._privilege_account`'s exact matching
    heuristic (account_type == "investment" + institution/name containing
    "PRIVILEGE") rather than importing it -- `app.cli.*` workers already
    avoid importing from `app.api` to prevent a circular import (see
    `app.cli.notification_worker`'s own local `_audit` reimplementation for
    the established precedent), and this heuristic is small enough that
    duplicating it here costs less than a cross-module dependency."""

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
        account
        for account in investments
        if "PRIVILEGE" in normalize_description(f"{account.institution} {account.name}")
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and len(investments) == 1:
        return investments[0]
    return None


def resolve_observed_balance(
    db: Session, *, household_id: str, account_id: str
) -> tuple[Decimal | None, date | None]:
    """Latest confirmed, non-superseded `AccountBalanceObservation` for the
    resolved account -- read-only, purely for display/reconciliation
    snapshotting onto a new `FundValuation` row. Never mutated, never
    treated as this module's own source of truth (see module docstring)."""

    observation = db.scalar(
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
    if observation is None:
        return None, None
    return money(observation.amount), observation.as_of_date


def ingest_latest_quote(
    db: Session,
    *,
    fund_cnpj: str,
    client: CvmClient | None = None,
    reference_month: date | None = None,
    now_utc: datetime | None = None,
) -> CvmResult:
    """Fetch the fund's latest official quota and idempotently persist it
    into `FundReferenceQuote`. Never raises, never partially writes: either
    a `CvmResult` with a populated `quote` and the row durably committed by
    the caller's transaction, or a `CvmResult` with `error`/`error_code` set
    and no database write attempted at all (Work Order: "CVM unavailable/
    malformed -> retain last valid state; no destructive write").

    Idempotent by construction: `fund_reference_quotes`'s unique constraint
    on `(fund_cnpj, quota_reference_date)` is the actual guarantee, not
    application discipline -- a concurrent/duplicate ingestion for the same
    fund/date loses the `IntegrityError` race harmlessly and this function
    returns the quote value it attempted to insert (already present in the
    table either way), matching
    `app.services.notification_scheduler.discover_due_deliveries`'s
    `begin_nested()`/`IntegrityError` discipline for its own unique
    constraint.
    """

    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    result = (client or CvmClient()).latest_quote(fund_cnpj_digits, reference_month=reference_month)
    if result.quote is None:
        return result

    quote = result.quote
    existing = db.scalar(
        select(FundReferenceQuote).where(
            FundReferenceQuote.fund_cnpj == quote.fund_cnpj,
            FundReferenceQuote.quota_reference_date == quote.quota_reference_date,
        )
    )
    if existing is not None:
        return result

    try:
        with db.begin_nested():
            db.add(
                FundReferenceQuote(
                    fund_cnpj=quote.fund_cnpj,
                    quota_reference_date=quote.quota_reference_date,
                    quota_value=quote.quota_value,
                    net_worth=quote.net_worth,
                    provider="cvm_dados_abertos",
                    source_url=quote.source_url,
                    ingestion_batch=quote.ingestion_batch,
                    retrieved_at=now_utc or datetime.now(UTC),
                )
            )
            db.flush()
    except IntegrityError:
        # Lost the race to a concurrent ingestion for the same fund/date --
        # the unique constraint is what actually matters; the row exists
        # either way.
        pass
    return result


def latest_quote_for_fund(db: Session, *, fund_cnpj: str) -> FundReferenceQuote | None:
    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    return db.scalar(
        select(FundReferenceQuote)
        .where(FundReferenceQuote.fund_cnpj == fund_cnpj_digits)
        .order_by(FundReferenceQuote.quota_reference_date.desc(), FundReferenceQuote.created_at.desc())
        .limit(1)
    )


def latest_position_for_household(
    db: Session, *, household_id: str, fund_cnpj: str
) -> FundUnitPosition | None:
    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    return db.scalar(
        select(FundUnitPosition)
        .where(
            FundUnitPosition.household_id == household_id,
            FundUnitPosition.fund_cnpj == fund_cnpj_digits,
            FundUnitPosition.invalidated_at.is_(None),
        )
        .order_by(FundUnitPosition.effective_date.desc(), FundUnitPosition.created_at.desc())
        .limit(1)
    )


def latest_valuation_for_household(
    db: Session, *, household_id: str, fund_cnpj: str
) -> FundValuation | None:
    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    return db.scalar(
        select(FundValuation)
        .where(
            FundValuation.household_id == household_id,
            FundValuation.fund_cnpj == fund_cnpj_digits,
            FundValuation.invalidated_at.is_(None),
        )
        .order_by(FundValuation.quota_reference_date.desc(), FundValuation.created_at.desc())
        .limit(1)
    )


def compute_gross_value(*, units_held: Decimal, quota_value: Decimal) -> Decimal:
    """`estimated_gross_value = units_held × latest_official_quota` (issue
    #85's "Financial architecture" section), rounded to cents only at this
    final step -- `units_held`/`quota_value` themselves are never rounded
    before this multiplication."""

    return money(units_held * quota_value)


def _variation_pct(current: Decimal, previous: Decimal) -> Decimal | None:
    if previous == 0:
        return None
    return ((current - previous) / previous).quantize(PERCENT_QUANTUM)


def run_valuation_for_household(
    db: Session,
    *,
    household_id: str,
    fund_cnpj: str,
    account_id: str | None = None,
    observed_balance: Decimal | None = None,
    observed_balance_as_of: date | None = None,
) -> FundValuationOutcome:
    """Idempotent per-household valuation step: combines the latest ingested
    `FundReferenceQuote` with the household's latest `FundUnitPosition` into
    one `FundValuation` row. A rerun for a household/fund whose latest quote
    reference date already has a valuation is a no-op returning
    `status="already_valued"` -- Work Order: "Idempotent reruns... must not
    create duplicate financial facts", enforced here at the application
    level *and* by `fund_valuations`' unique constraint as the actual
    guarantee against a concurrent duplicate.

    `observed_balance`/`observed_balance_as_of` are an optional, purely
    informational snapshot the caller may supply (e.g. from
    `app.api._latest_active_balance_observation` for the resolved Privilège
    account) -- never read or written by this function beyond copying them
    onto the new row for display/reconciliation; this function never
    queries `AccountBalanceObservation` itself, keeping this service
    decoupled from `app.api`'s Privilège-account-resolution heuristics.
    """

    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    quote = latest_quote_for_fund(db, fund_cnpj=fund_cnpj_digits)
    if quote is None:
        return FundValuationOutcome(status="no_quote_available")

    position = latest_position_for_household(db, household_id=household_id, fund_cnpj=fund_cnpj_digits)
    if position is None:
        return FundValuationOutcome(status="no_position_evidence")

    existing = db.scalar(
        select(FundValuation).where(
            FundValuation.household_id == household_id,
            FundValuation.fund_cnpj == fund_cnpj_digits,
            FundValuation.quota_reference_date == quote.quota_reference_date,
        )
    )
    if existing is not None:
        return FundValuationOutcome(status="already_valued", valuation=existing)

    previous = latest_valuation_for_household(db, household_id=household_id, fund_cnpj=fund_cnpj_digits)
    gross_value = compute_gross_value(units_held=position.units_held, quota_value=quote.quota_value)
    previous_gross_value = previous.gross_value if previous is not None else None
    variation_amount = (
        money(gross_value - previous_gross_value) if previous_gross_value is not None else None
    )
    variation_pct = (
        _variation_pct(gross_value, previous_gross_value)
        if previous_gross_value is not None
        else None
    )
    reconciliation_diff = (
        money(gross_value - observed_balance) if observed_balance is not None else None
    )

    valuation = FundValuation(
        household_id=household_id,
        fund_cnpj=fund_cnpj_digits,
        account_id=account_id,
        quote_id=quote.id,
        position_id=position.id,
        quota_reference_date=quote.quota_reference_date,
        quota_value=quote.quota_value,
        units_held=position.units_held,
        gross_value=gross_value,
        previous_gross_value=previous_gross_value,
        variation_amount=variation_amount,
        variation_pct=variation_pct,
        observed_balance=observed_balance,
        observed_balance_as_of=observed_balance_as_of,
        reconciliation_diff=reconciliation_diff,
        provider=quote.provider,
        retrieved_at=quote.retrieved_at,
        valuation_version=VALUATION_VERSION,
        status="ok",
        trace_id=str(uuid.uuid4()),
    )
    try:
        with db.begin_nested():
            db.add(valuation)
            db.flush()
    except IntegrityError:
        # Lost the race to a concurrent valuation run for the same
        # household/fund/reference-date -- re-read the row a concurrent
        # transaction just committed instead of raising.
        existing = db.scalar(
            select(FundValuation).where(
                FundValuation.household_id == household_id,
                FundValuation.fund_cnpj == fund_cnpj_digits,
                FundValuation.quota_reference_date == quote.quota_reference_date,
            )
        )
        return FundValuationOutcome(status="already_valued", valuation=existing)

    return FundValuationOutcome(status="ok", valuation=valuation)


def record_position_snapshot(
    db: Session,
    *,
    household_id: str,
    fund_cnpj: str,
    units_held: Decimal,
    effective_date: date,
    evidence_type: str,
    evidence_document_id: str | None = None,
    evidence_balance_observation_id: str | None = None,
    note: str | None = None,
    recorded_by: str | None = None,
) -> FundUnitPosition:
    """Bootstrap or correct the household's `units_held` from a direct
    evidence snapshot (Work Order "Quantity-of-units bootstrap": an actual
    statement quantity, or a user-confirmed balance matched against an
    official quota for the *same* reference date). Never inferred from a
    balance and a quota belonging to different reference dates -- the
    caller is responsible for that same-reference-date check before calling
    this function; this function only records the evidence it is given."""

    if evidence_type not in POSITION_EVIDENCE_SNAPSHOT_TYPES:
        raise PrivilegeValuationError(
            f"evidence_type inválido para um snapshot de posição: {evidence_type!r} "
            f"(esperado um de {POSITION_EVIDENCE_SNAPSHOT_TYPES!r})"
        )
    if units_held < 0:
        raise PrivilegeValuationError("units_held não pode ser negativo")
    if evidence_type == "statement_position" and evidence_document_id is None:
        raise PrivilegeValuationError("statement_position exige evidence_document_id")
    if evidence_type == "confirmed_balance_reconciliation" and evidence_balance_observation_id is None:
        raise PrivilegeValuationError(
            "confirmed_balance_reconciliation exige evidence_balance_observation_id"
        )

    position = FundUnitPosition(
        household_id=household_id,
        fund_cnpj=normalize_fund_cnpj(fund_cnpj),
        units_held=units_held,
        effective_date=effective_date,
        evidence_type=evidence_type,
        evidence_document_id=evidence_document_id,
        evidence_balance_observation_id=evidence_balance_observation_id,
        note=note,
        recorded_by=recorded_by,
        trace_id=str(uuid.uuid4()),
    )
    db.add(position)
    db.flush()
    return position


def record_position_movement(
    db: Session,
    *,
    household_id: str,
    fund_cnpj: str,
    delta_units: Decimal,
    effective_date: date,
    evidence_type: str,
    note: str | None = None,
    recorded_by: str | None = None,
) -> FundUnitPosition:
    """Record a known application (`evidence_type="application"`,
    `delta_units > 0`) or redemption (`evidence_type="redemption"`,
    `delta_units < 0`) against the household's current position. Requires a
    prior position (statement or reconciliation) to apply the delta against
    -- there is no bootstrap-from-a-movement path, matching the Work
    Order's "Never infer units... without explicit reconciliation" and
    "Applications/redemptions alter units... [never] create a fake bank
    transaction merely to make the valuation match" (this function only
    ever touches `fund_unit_positions`, never `transactions`/
    `account_balance_observations`)."""

    if evidence_type not in POSITION_EVIDENCE_MOVEMENT_TYPES:
        raise PrivilegeValuationError(
            f"evidence_type inválido para um movimento de posição: {evidence_type!r} "
            f"(esperado um de {POSITION_EVIDENCE_MOVEMENT_TYPES!r})"
        )
    if evidence_type == "application" and delta_units <= 0:
        raise PrivilegeValuationError("application exige delta_units positivo")
    if evidence_type == "redemption" and delta_units >= 0:
        raise PrivilegeValuationError("redemption exige delta_units negativo")

    fund_cnpj_digits = normalize_fund_cnpj(fund_cnpj)
    previous = latest_position_for_household(db, household_id=household_id, fund_cnpj=fund_cnpj_digits)
    if previous is None:
        raise PrivilegeValuationError(
            "Não é possível registrar um movimento sem uma posição anterior "
            "(statement_position/confirmed_balance_reconciliation) para este fundo"
        )
    new_units_held = previous.units_held + delta_units
    if new_units_held < 0:
        raise PrivilegeValuationError(
            "Movimento resultaria em quantidade de cotas negativa; verifique o valor informado"
        )

    position = FundUnitPosition(
        household_id=household_id,
        fund_cnpj=fund_cnpj_digits,
        units_held=new_units_held,
        effective_date=effective_date,
        evidence_type=evidence_type,
        delta_units=delta_units,
        note=note,
        recorded_by=recorded_by,
        trace_id=str(uuid.uuid4()),
    )
    db.add(position)
    db.flush()
    return position
