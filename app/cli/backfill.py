"""Idempotent, resumable backfill for the Financial Integrity Engine.

Reprocesses existing households against the deterministic services already
used by the live import/API pipeline -- it never re-implements any
financial rule of its own. Concretely, for every targeted household it:

1. records an `unknown` `DocumentReconciliation` for every `Document` that
   has none yet (`app.services.reconciliation.unknown_reconciliation` +
   `persist_reconciliation`) -- it never invents declared/reconstructed
   totals for a document whose original parsed evidence is gone;
2. runs `app.services.duplicates.discover_transaction_duplicates` for every
   `Transaction` not yet examined by any duplicate pass -- it only ever
   creates/updates the derived `DuplicateGroup`/`DuplicateGroupMember`
   evidence rows, immediately visible through `GET /duplicate-groups` for
   human review; it never writes to the `Transaction` rows themselves (see
   below);
3. rebuilds the canonical `FinancialSnapshot` for every period in range
   (`app.services.financial_snapshots.build_snapshot`, already idempotent by
   checksum) and, per period, evaluates the INV-014 baseline checks through
   `execute_integrity_run(trigger=BACKFILL)`;
4. evaluates one household-wide (`scope=global`) integrity run at the end,
   the same scope `consolidated_integrity_status` reads by default.

What it never does, by construction (see docs/WORK_ORDER_PR8_...):

- it never writes to `Transaction`, `Document`, `PayrollRecord`,
  `Commission` or `Obligation` at all -- including the duplicate
  classification columns (`canonical_status`/`possible_duplicate`/
  `excluded`/`duplicate_group_id`): those are the system's *applied*
  classification of already-published financial history, and changing them
  changes which source is counted as canonical, so only a genuinely new
  transaction arriving through the live pipeline
  (`app.services.duplicates.register_transaction_duplicates`) or an
  explicit human decision (`resolve_duplicate_group`) may change them --
  never an automatic historical reprocessing side effect;
- it never fabricates an `AccountBalanceObservation` or an "as of" date for
  a legacy investment balance -- `build_snapshot`'s own `_opening_balance`
  fallback already leaves that evidence untrusted (`balance_evidence_trusted:
  false`) when no observation exists, and this command does not touch that
  path at all;
- it never resolves, acknowledges or auto-fixes an `IntegrityFinding` --
  only `execute_integrity_run`'s own deterministic supersede-on-PASS logic
  (a real re-evaluation, not this command) can move a finding.

Idempotent and resumable by construction, not by a separate resume
checkpoint: every step above is itself either a no-op on unchanged input
(`build_snapshot` returns the current row unchanged when its checksum
matches) or scoped to only the rows that still need it (documents with no
reconciliation yet, transactions with no duplicate group yet). Re-running
the whole command after a partial failure -- or twice in a row -- reproduces
the same end state without duplicating any derived row; the only rows that
legitimately grow across reruns are the audit trail rows themselves
(`IntegrityRun`, `BackfillRun`), by design (see `IntegrityRun`'s own
reincidence handling in `app.services.financial_integrity`).

`--dry-run` runs every step above against the real session, produces the
same report, and then rolls the whole transaction back instead of
committing -- so it exercises the exact same code paths a real run does
(same queries, same service calls) without persisting anything, and reports
what a real run would change ("Execução controlada... sem auto-fix
destrutivo"). The `BackfillRun` manifest for a dry run is still recorded,
on its own separate connection, after the rollback -- see `BackfillRun`'s
docstring in `app/models.py`.
"""

from __future__ import annotations

import argparse
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    BackfillRun,
    Document,
    DocumentReconciliation,
    DuplicateGroup,
    DuplicateGroupMember,
    FinancialSnapshot,
    Household,
    IntegrityFinding,
    Transaction,
)
from app.services.duplicates import discover_transaction_duplicates
from app.services.finance import add_months, month_key
from app.services.financial_integrity import (
    ACTIVE_FINDING_STATUSES,
    IntegrityRunScope,
    IntegrityRunTrigger,
    build_baseline_checks,
    execute_integrity_run,
)
from app.services.financial_invariants import FINANCIAL_RULES_VERSION
from app.services.financial_snapshots import build_snapshot
from app.services.reconciliation import persist_reconciliation, unknown_reconciliation


def _app_version() -> str:
    with suppress(PackageNotFoundError):
        return version("family-financial-planning")
    return "unknown"


@dataclass(slots=True)
class HouseholdBackfillReport:
    household_id: str
    household_name: str
    periods: list[str] = field(default_factory=list)
    documents_reconciled_unknown: int = 0
    duplicate_candidates_examined: int = 0
    snapshots_touched: int = 0
    period_integrity_runs: int = 0
    global_integrity_run_id: str | None = None
    open_findings_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "household_name": self.household_name,
            "periods": self.periods,
            "documents_reconciled_unknown": self.documents_reconciled_unknown,
            "duplicate_candidates_examined": self.duplicate_candidates_examined,
            "snapshots_touched": self.snapshots_touched,
            "period_integrity_runs": self.period_integrity_runs,
            "global_integrity_run_id": self.global_integrity_run_id,
            "open_findings_after": self.open_findings_after,
        }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reprocessa famílias existentes contra o Financial Integrity Engine "
            "(reconciliação, duplicidades, snapshots canônicos e integrity runs). "
            "Idempotente e retomável: pode ser executado novamente com segurança."
        )
    )
    parser.add_argument(
        "--household",
        action="append",
        dest="households",
        help="Nome exato de uma família a processar (repetível). Sem esta opção, processa todas.",
    )
    parser.add_argument("--from", dest="period_from", help="Competência inicial AAAA-MM (inclusive).")
    parser.add_argument("--to", dest="period_to", help="Competência final AAAA-MM (inclusive).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Executa todo o processamento e reporta o que mudaria, sem persistir alterações.",
    )
    return parser.parse_args(argv)


def _parse_period(value: str, *, flag: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError(f"{flag} deve usar o formato AAAA-MM") from exc
    return parsed.strftime("%Y-%m")


def _target_households(db: Session, names: list[str] | None) -> list[Household]:
    query = select(Household).order_by(Household.created_at, Household.id)
    if names:
        query = query.where(Household.name.in_(names))
    households = list(db.scalars(query).all())
    if names:
        found = {household.name for household in households}
        missing = sorted(set(names) - found)
        if missing:
            raise ValueError(f"Família(s) não encontrada(s): {', '.join(missing)}")
    return households


def _period_range(
    db: Session, household_id: str, *, period_from: str | None, period_to: str | None
) -> list[str]:
    if period_from and period_to:
        start = date.fromisoformat(f"{period_from}-01")
        end = date.fromisoformat(f"{period_to}-01")
    else:
        bounds = db.execute(
            select(func.min(Transaction.booked_at), func.max(Transaction.booked_at)).where(
                Transaction.household_id == household_id
            )
        ).one()
        earliest, latest = bounds
        if earliest is None or latest is None:
            return []
        start = date.fromisoformat(f"{period_from}-01") if period_from else earliest.replace(day=1)
        end = date.fromisoformat(f"{period_to}-01") if period_to else latest.replace(day=1)
    if start > end:
        raise ValueError("--from não pode ser posterior a --to")
    periods = []
    cursor = start
    while cursor <= end:
        periods.append(month_key(cursor))
        cursor = add_months(cursor, 1)
    return periods


def _backfill_unreconciled_documents(db: Session, household: Household) -> int:
    """Record an explicit `unknown` reconciliation for every document that has none.

    Never re-derives declared/reconstructed totals: the original parsed
    evidence (`ParsedDocument`) only exists transiently at import time and is
    not retained, so any structured reconciliation this command produced
    would be fabricated. Recording `unknown` instead makes the *absence* of
    reconciliation evidence an explicit, auditable fact rather than a silent
    gap -- matching docs/FINANCIAL_RULES.md's "ausência de evidência =>
    unknown, nunca pass".
    """

    reconciled_subquery = select(DocumentReconciliation.document_id).where(
        DocumentReconciliation.document_id == Document.id
    )
    pending = db.scalars(
        select(Document).where(
            Document.household_id == household.id,
            ~reconciled_subquery.exists(),
        )
    ).all()
    for document in pending:
        parsed, result = unknown_reconciliation(
            document_type=document.document_type,
            parser_name="backfill",
            reason="legacy_document_without_retained_parsing_evidence",
        )
        persist_reconciliation(
            db,
            household_id=household.id,
            document_id=document.id,
            parsed=parsed,
            result=result,
        )
    return len(pending)


def _backfill_duplicate_classification(db: Session, household: Household) -> int:
    """Discover duplicate evidence for every transaction not yet examined by
    any duplicate pass, without mutating the transactions themselves.

    Reuses `app.services.duplicates.discover_transaction_duplicates` --
    built on the exact same deterministic `assess_duplicate` rule the live
    import pipeline uses -- so a legacy transaction is scored by the
    identical rule a newly imported one would be, never a parallel/
    duplicated implementation. Unlike the live pipeline's
    `register_transaction_duplicates`, this only creates/updates the
    derived `DuplicateGroup`/`DuplicateGroupMember` evidence: it never
    writes `canonical_status`/`possible_duplicate`/`excluded`/
    `duplicate_group_id` on an existing `Transaction` (see the module
    docstring and docs/WORK_ORDER_PR8_...).

    Because this command never sets `Transaction.duplicate_group_id`,
    "not yet examined" is tracked by the absence of a `DuplicateGroupMember`
    row for the transaction instead. A transaction already classified by the
    live pipeline is also already a group member, so it is correctly
    skipped here too -- idempotency does not depend on ever mutating the
    transaction.
    """

    already_examined = select(DuplicateGroupMember.transaction_id).where(
        DuplicateGroupMember.transaction_id == Transaction.id
    )
    pending = db.scalars(
        select(Transaction)
        .where(
            Transaction.household_id == household.id,
            ~already_examined.exists(),
        )
        .order_by(Transaction.booked_at, Transaction.id)
    ).all()
    for transaction in pending:
        discover_transaction_duplicates(db, transaction=transaction, household_id=household.id)
    return len(pending)


def _backfill_snapshots_and_period_integrity(
    db: Session, household: Household, periods: list[str]
) -> tuple[int, int]:
    """Evaluate every period's integrity findings *before* building any
    snapshot for the household, in two separate passes rather than one
    interleaved loop.

    `build_snapshot`'s checksum folds in `consolidated_integrity_status(...,
    period=...)` -- i.e. the *currently open* findings for that period. If a
    single pass built period P's snapshot and only then evaluated period P's
    duplicate/INV-014 finding, the snapshot built on this invocation would
    check itself against integrity evidence that does not exist yet, and a
    later invocation (which *would* see that already-open finding before
    building the same snapshot) would compute a different checksum and
    version -- never converging to a stable state after the first run and
    breaking idempotency. Settling every period's findings first makes what
    each snapshot in this same call observes identical to what any later
    call observes for the same underlying facts.
    """

    period_runs = 0
    for period in periods:
        checks = build_baseline_checks(
            db, household_id=household.id, scope=IntegrityRunScope.PERIOD, period=period
        )
        if checks:
            execute_integrity_run(
                db,
                household_id=household.id,
                scope=IntegrityRunScope.PERIOD,
                trigger=IntegrityRunTrigger.BACKFILL,
                checks=checks,
                period=period,
            )
            period_runs += 1

    snapshots_touched = 0
    for period in periods:
        build_snapshot(db, household_id=household.id, period=period, generated_by=None)
        snapshots_touched += 1
    return snapshots_touched, period_runs


def _backfill_global_integrity(db: Session, household: Household) -> str | None:
    checks = build_baseline_checks(db, household_id=household.id, scope=IntegrityRunScope.GLOBAL)
    if not checks:
        return None
    run, _results = execute_integrity_run(
        db,
        household_id=household.id,
        scope=IntegrityRunScope.GLOBAL,
        trigger=IntegrityRunTrigger.BACKFILL,
        checks=checks,
    )
    return run.id


def _open_findings_count(db: Session, household_id: str) -> int:
    return int(
        db.scalar(
            select(func.count(IntegrityFinding.id)).where(
                IntegrityFinding.household_id == household_id,
                IntegrityFinding.status.in_(ACTIVE_FINDING_STATUSES),
            )
        )
        or 0
    )


def process_household(
    db: Session, household: Household, *, period_from: str | None, period_to: str | None
) -> HouseholdBackfillReport:
    report = HouseholdBackfillReport(household_id=household.id, household_name=household.name)
    report.documents_reconciled_unknown = _backfill_unreconciled_documents(db, household)
    report.duplicate_candidates_examined = _backfill_duplicate_classification(db, household)
    periods = _period_range(db, household.id, period_from=period_from, period_to=period_to)
    report.periods = periods
    report.snapshots_touched, report.period_integrity_runs = _backfill_snapshots_and_period_integrity(
        db, household, periods
    )
    report.global_integrity_run_id = _backfill_global_integrity(db, household)
    report.open_findings_after = _open_findings_count(db, household.id)
    return report


def _state_counts(db: Session, household_ids: list[str]) -> dict[str, int]:
    if not household_ids:
        return {
            "financial_snapshots": 0,
            "document_reconciliations": 0,
            "duplicate_groups": 0,
        }
    return {
        "financial_snapshots": int(
            db.scalar(
                select(func.count(FinancialSnapshot.id)).where(
                    FinancialSnapshot.household_id.in_(household_ids)
                )
            )
            or 0
        ),
        "document_reconciliations": int(
            db.scalar(
                select(func.count(DocumentReconciliation.id)).where(
                    DocumentReconciliation.household_id.in_(household_ids)
                )
            )
            or 0
        ),
        "duplicate_groups": int(
            db.scalar(
                select(func.count(DuplicateGroup.id)).where(
                    DuplicateGroup.household_id.in_(household_ids)
                )
            )
            or 0
        ),
    }


def run_backfill(
    db: Session,
    *,
    household_names: list[str] | None,
    period_from: str | None,
    period_to: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    households = _target_households(db, household_names)
    household_ids = [household.id for household in households]
    before = _state_counts(db, household_ids)
    reports = [
        process_household(db, household, period_from=period_from, period_to=period_to)
        for household in households
    ]
    after = _state_counts(db, household_ids)
    return {
        "households_processed": len(households),
        "households": [item.to_dict() for item in reports],
        "state_before": before,
        "state_after": after,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    period_from = _parse_period(args.period_from, flag="--from") if args.period_from else None
    period_to = _parse_period(args.period_to, flag="--to") if args.period_to else None

    trace_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    timer_started = time.perf_counter()

    with SessionLocal() as db:
        try:
            summary = run_backfill(
                db,
                household_names=args.households,
                period_from=period_from,
                period_to=period_to,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - reported below, then re-raised
            db.rollback()
            _record_manifest(
                status="failed",
                dry_run=args.dry_run,
                household_names=args.households,
                period_from=period_from,
                period_to=period_to,
                started_at=started_at,
                duration_ms=max(0, round((time.perf_counter() - timer_started) * 1000)),
                summary=None,
                error_code=type(exc).__name__[:80],
                trace_id=trace_id,
            )
            print(f"Backfill falhou: {exc}")
            return 1

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

        _record_manifest(
            status="completed",
            dry_run=args.dry_run,
            household_names=args.households,
            period_from=period_from,
            period_to=period_to,
            started_at=started_at,
            duration_ms=max(0, round((time.perf_counter() - timer_started) * 1000)),
            summary=summary,
            error_code=None,
            trace_id=trace_id,
        )

    _print_summary(summary, dry_run=args.dry_run)
    return 0


def _record_manifest(
    *,
    status: str,
    dry_run: bool,
    household_names: list[str] | None,
    period_from: str | None,
    period_to: str | None,
    started_at: datetime,
    duration_ms: int,
    summary: dict[str, Any] | None,
    error_code: str | None,
    trace_id: str,
) -> None:
    """Persist the `BackfillRun` manifest on its own connection.

    A dry run rolls back everything else it touched (see `main`); the record
    that a dry run happened, and what it reported, is itself audit-worthy
    evidence and must survive that rollback -- so it is written here, after
    the working session's own commit/rollback has already resolved, on a
    fresh `SessionLocal()`.
    """

    with SessionLocal() as manifest_db:
        manifest_db.add(
            BackfillRun(
                status=status,
                dry_run=dry_run,
                household_scope={"household_names": household_names} if household_names else None,
                period_from=period_from,
                period_to=period_to,
                financial_rules_version=FINANCIAL_RULES_VERSION,
                calculation_version=FINANCIAL_RULES_VERSION,
                app_version=_app_version(),
                started_at=started_at,
                completed_at=datetime.now(UTC),
                duration_ms=duration_ms,
                summary=summary,
                error_code=error_code,
                trace_id=trace_id,
            )
        )
        manifest_db.commit()


def _print_summary(summary: dict[str, Any], *, dry_run: bool) -> None:
    mode = "DRY-RUN (nada foi persistido)" if dry_run else "EXECUÇÃO REAL"
    print(f"Backfill concluído [{mode}]")
    print(f"Famílias processadas: {summary['households_processed']}")
    for household in summary["households"]:
        print(
            f"- {household['household_name']}: "
            f"{len(household['periods'])} período(s), "
            f"{household['documents_reconciled_unknown']} documento(s) marcado(s) unknown, "
            f"{household['duplicate_candidates_examined']} transação(ões) examinada(s) para duplicidade, "
            f"{household['open_findings_after']} finding(s) aberto(s)"
        )
    print(f"Antes:  {summary['state_before']}")
    print(f"Depois: {summary['state_after']}")


if __name__ == "__main__":
    raise SystemExit(main())
