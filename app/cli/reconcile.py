"""Real-data go-live reconciliation report -- P0 #87, October Go-Live Slice 8.

Work Order: `docs/WORK_ORDER_OCTOBER_GO_LIVE_SLICE_8.md` §1 ("Inventário
real e reconciliação") and `docs/OCTOBER_GO_LIVE_CONFLICT_MATRIX.md` §4
("comando de reconciliação real (mesmo padrão do `app.cli.backfill`:
idempotente, `--dry-run`, nunca escreve em
`Transaction`/`Document`/`PayrollRecord`/`Commission`/`Obligation`; só faz
insert aditivo em `card_invoices`/observações novas)").

This command never implements a second financial engine -- see
`docs/OCTOBER_GO_LIVE_REBASELINE.md` §12 and the Work Order's "Proibições"
("não criar novo motor financeiro paralelo"). For every targeted
household/period it:

1. rebuilds the canonical `FinancialSnapshot`
   (`app.services.financial_snapshots.build_snapshot`, already idempotent by
   checksum -- the exact function `GET /dashboard`/`GET /reports` call, and
   the same one `app.cli.backfill` uses);
2. reads back `snapshot.payload["reconciliation"]` -- the confirmed vs.
   reconstructed balance, divergence and status rebaseline §5.1 requires --
   and reports it; it never adjusts it;
3. evaluates INV-023 ("resultado realizado exige evidência de movimento")
   and INV-024 ("saldo confirmado é soberano") against that same snapshot,
   through the exact same pure evaluator
   (`app.services.invariant_registry.evaluate_invariant`) that
   `GET /forecast`/`POST /monthly-closes/{period}/run` already use for these
   two checks -- a **read-only** evaluation here: no `IntegrityRun`/
   `IntegrityFinding` is persisted by this command, so it never competes
   with or duplicates the gate those endpoints already enforce; it only
   reports the same verdict for this report's benefit;
4. for every active credit-card account, resyncs the current competence
   (`get_or_sync_invoice`, the module's own single mutating, additive-only,
   idempotent entrypoint -- claims still-unclaimed expense transactions of
   that exact account+competence, exactly as every other invoice-touching
   endpoint already does) and reports its divergence (`invoice_divergence`)
   -- rebaseline §6.4;
5. summarizes the household's inventory (accounts by type, obligations by
   status, investments and their current value) -- Work Order §1.

What this command never does, by construction:

- it never writes to `Transaction`, `Document`, `PayrollRecord`,
  `Commission` or `Obligation`;
- it never persists an `IntegrityRun`/`IntegrityFinding`;
- it never fabricates a correcting entry, synthetic transfer, or adjustment
  for a divergence it finds -- `not_reconciled`/`unreconciled_unexplained`/
  `FAIL` are reported exactly as computed, for human review.

`--dry-run` runs every step above against the real session and then rolls
back instead of committing, so any `FinancialSnapshot`/`CardInvoice` row
this run would otherwise persist never does -- same contract as
`app.cli.backfill --dry-run`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Account, Household, Obligation
from app.services.card_invoice_lifecycle import CardInvoiceError, get_or_sync_invoice, invoice_divergence
from app.services.finance import add_months, month_key
from app.services.financial_invariants import InvariantContext, InvariantScope
from app.services.financial_snapshots import (
    build_snapshot,
    realized_balance_sovereignty_facts,
    realized_liquidity_evidence_facts,
)
from app.services.invariant_registry import evaluate_invariant
from app.services.investments import investments_summary
from app.services.reconciliation import RECONCILIATION_TOLERANCE

CREDIT_CARD_ACCOUNT_TYPE = "credit_card"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)))


@dataclass(slots=True)
class PeriodReconciliation:
    period: str
    observed_balance: float | None
    observed_balance_as_of: str | None
    reconstructed_balance_at_observation: float | None
    reconciliation_divergence: float
    closing_liquidity_balance: float
    status: str
    inv023_status: str
    inv023_message: str
    inv024_status: str
    inv024_message: str


@dataclass(slots=True)
class InvoiceReconciliation:
    account_id: str
    account_name: str
    competence: str
    invoice_status: str
    declared_total: float | None
    expected_total: float
    divergence_status: str
    difference: float | None
    explanations: tuple[dict[str, Any], ...]


@dataclass(slots=True)
class HouseholdReconciliationReport:
    household_id: str
    household_name: str
    periods: list[PeriodReconciliation] = field(default_factory=list)
    invoices: list[InvoiceReconciliation] = field(default_factory=list)
    accounts_by_type: dict[str, int] = field(default_factory=dict)
    obligations_by_status: dict[str, int] = field(default_factory=dict)
    investments_total_current_value: float = 0.0
    investments_count: int = 0
    unresolved_divergences: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "household_name": self.household_name,
            "periods": [asdict(item) for item in self.periods],
            "invoices": [asdict(item) for item in self.invoices],
            "accounts_by_type": self.accounts_by_type,
            "obligations_by_status": self.obligations_by_status,
            "investments_total_current_value": self.investments_total_current_value,
            "investments_count": self.investments_count,
            "unresolved_divergences": self.unresolved_divergences,
        }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Relatório real de reconciliação de go-live: saldo confirmado x "
            "reconstruído, divergência de fatura e inventário por família. "
            "Nunca fabrica ajuste; nunca escreve em Transaction/Document/"
            "PayrollRecord/Commission/Obligation. Idempotente."
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
        help="Executa todo o processamento e reporta o que encontraria, sem persistir alterações.",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        help="Caminho para também salvar o relatório em JSON.",
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


def _periods_for_household(
    db: Session, household_id: str, *, period_from: str | None, period_to: str | None
) -> list[str]:
    """Same bounds contract as `app.cli.backfill._period_range` (kept as an
    independent, small, non-financial date-range helper here rather than a
    cross-module private import -- there is no financial rule in this
    function to risk diverging on)."""

    from app.models import Transaction

    if period_from and period_to:
        start = date.fromisoformat(f"{period_from}-01")
        end = date.fromisoformat(f"{period_to}-01")
    else:
        earliest = db.scalar(
            select(Transaction.booked_at)
            .where(Transaction.household_id == household_id)
            .order_by(Transaction.booked_at.asc())
            .limit(1)
        )
        latest = db.scalar(
            select(Transaction.booked_at)
            .where(Transaction.household_id == household_id)
            .order_by(Transaction.booked_at.desc())
            .limit(1)
        )
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


def _reconcile_period(db: Session, *, household: Household, period: str) -> PeriodReconciliation:
    snapshot = build_snapshot(db, household_id=household.id, period=period, generated_by=None)
    reconciliation = dict(snapshot.payload.get("reconciliation") or {})
    observed = reconciliation.get("observed_balance")
    divergence = Decimal(str(reconciliation.get("reconciliation_divergence") or 0))
    if observed is None:
        status = "sem_observacao_no_periodo"
    elif abs(divergence) <= RECONCILIATION_TOLERANCE:
        status = "reconciliado"
    else:
        status = "divergente"

    inv023 = evaluate_invariant(
        "INV-023",
        InvariantContext(
            facts=realized_liquidity_evidence_facts(snapshot),
            scope=InvariantScope.PERIOD,
            entity_type="household",
            entity_id=household.id,
            period=period,
        ),
    )
    inv024 = evaluate_invariant(
        "INV-024",
        InvariantContext(
            facts=realized_balance_sovereignty_facts(snapshot),
            scope=InvariantScope.PERIOD,
            entity_type="household",
            entity_id=household.id,
            period=period,
        ),
    )
    return PeriodReconciliation(
        period=period,
        observed_balance=_num(observed),
        observed_balance_as_of=reconciliation.get("observed_balance_as_of"),
        reconstructed_balance_at_observation=_num(reconciliation.get("reconstructed_balance_at_observation")),
        reconciliation_divergence=_num(divergence) or 0.0,
        closing_liquidity_balance=_num(snapshot.closing_liquidity_balance) or 0.0,
        status=status,
        inv023_status=inv023.status.value if hasattr(inv023.status, "value") else str(inv023.status),
        inv023_message=inv023.message,
        inv024_status=inv024.status.value if hasattr(inv024.status, "value") else str(inv024.status),
        inv024_message=inv024.message,
    )


def _reconcile_card_invoices(
    db: Session, *, household: Household, as_of: date
) -> list[InvoiceReconciliation]:
    cards = db.scalars(
        select(Account).where(
            Account.household_id == household.id,
            Account.account_type == CREDIT_CARD_ACCOUNT_TYPE,
            Account.active.is_(True),
        )
    ).all()
    results: list[InvoiceReconciliation] = []
    for card in cards:
        if card.card_closing_day is None or card.card_due_day is None:
            continue
        competence = month_key(as_of)
        try:
            invoice = get_or_sync_invoice(
                db, household_id=household.id, account=card, competence=competence, as_of=as_of
            )
            divergence = invoice_divergence(db, household_id=household.id, invoice_id=invoice.id)
        except (CardInvoiceError, LookupError):
            continue
        results.append(
            InvoiceReconciliation(
                account_id=card.id,
                account_name=card.name,
                competence=competence,
                invoice_status=invoice.status,
                declared_total=_num(divergence.declared_total),
                expected_total=_num(divergence.expected_total) or 0.0,
                divergence_status=divergence.status,
                difference=_num(divergence.difference),
                explanations=divergence.explanations,
            )
        )
    return results


def reconcile_household(
    db: Session, household: Household, *, period_from: str | None, period_to: str | None, as_of: date
) -> HouseholdReconciliationReport:
    report = HouseholdReconciliationReport(household_id=household.id, household_name=household.name)
    periods = _periods_for_household(db, household.id, period_from=period_from, period_to=period_to)
    report.periods = [_reconcile_period(db, household=household, period=period) for period in periods]
    report.invoices = _reconcile_card_invoices(db, household=household, as_of=as_of)

    accounts = db.scalars(
        select(Account).where(Account.household_id == household.id, Account.active.is_(True))
    ).all()
    accounts_by_type: dict[str, int] = {}
    for account in accounts:
        accounts_by_type[account.account_type] = accounts_by_type.get(account.account_type, 0) + 1
    report.accounts_by_type = accounts_by_type

    obligations = db.scalars(
        select(Obligation).where(Obligation.household_id == household.id, Obligation.active.is_(True))
    ).all()
    obligations_by_status: dict[str, int] = {}
    for obligation in obligations:
        obligations_by_status[obligation.status] = obligations_by_status.get(obligation.status, 0) + 1
    report.obligations_by_status = obligations_by_status

    investments = investments_summary(db, household_id=household.id)
    report.investments_total_current_value = _num(investments["total_current_value"]) or 0.0
    report.investments_count = len(investments["investments"])

    report.unresolved_divergences = sum(
        1 for period in report.periods if period.status == "divergente"
    ) + sum(
        1
        for invoice in report.invoices
        if invoice.divergence_status in ("unreconciled_explained", "unreconciled_unexplained")
    )
    return report


def run_reconciliation(
    db: Session,
    *,
    household_names: list[str] | None,
    period_from: str | None,
    period_to: str | None,
    as_of: date,
) -> dict[str, Any]:
    households = _target_households(db, household_names)
    reports = [
        reconcile_household(db, household, period_from=period_from, period_to=period_to, as_of=as_of)
        for household in households
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "households_processed": len(households),
        "households": [item.to_dict() for item in reports],
        "total_unresolved_divergences": sum(item.unresolved_divergences for item in reports),
    }


def _print_report(summary: dict[str, Any], *, dry_run: bool) -> None:
    mode = "DRY-RUN (nada foi persistido)" if dry_run else "EXECUÇÃO REAL"
    print(f"Reconciliação de go-live concluída [{mode}]")
    print(f"Famílias processadas: {summary['households_processed']}")
    for household in summary["households"]:
        print(f"\n== {household['household_name']} ==")
        print(
            "Contas: "
            + ", ".join(f"{count}x {kind}" for kind, count in sorted(household["accounts_by_type"].items()))
            or "Contas: nenhuma"
        )
        print(
            "Obrigações: "
            + ", ".join(f"{count}x {status}" for status, count in sorted(household["obligations_by_status"].items()))
            or "Obrigações: nenhuma"
        )
        print(
            f"Investimentos: {household['investments_count']} "
            f"(valor de hoje total: R$ {household['investments_total_current_value']:.2f})"
        )
        for period in household["periods"]:
            if period["observed_balance"] is None:
                print(f"  {period['period']}: sem saldo confirmado no período")
                continue
            print(
                f"  {period['period']}: saldo confirmado R$ {period['observed_balance']:.2f} | "
                f"reconstruído R$ {period['reconstructed_balance_at_observation']:.2f} | "
                f"diferença R$ {period['reconciliation_divergence']:.2f} | {period['status'].upper()} | "
                f"INV-023={period['inv023_status']} INV-024={period['inv024_status']}"
            )
        for invoice in household["invoices"]:
            print(
                f"  Fatura {invoice['account_name']} ({invoice['competence']}): "
                f"{invoice['invoice_status']} | {invoice['divergence_status'].upper()}"
                + (f" | diferença R$ {invoice['difference']:.2f}" if invoice["difference"] else "")
            )
        if household["unresolved_divergences"]:
            print(f"  ATENÇÃO: {household['unresolved_divergences']} divergência(s) não resolvida(s).")
    print(f"\nTotal de divergências não resolvidas: {summary['total_unresolved_divergences']}")


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    period_from = _parse_period(args.period_from, flag="--from") if args.period_from else None
    period_to = _parse_period(args.period_to, flag="--to") if args.period_to else None
    as_of = date.today()

    with SessionLocal() as db:
        try:
            summary = run_reconciliation(
                db,
                household_names=args.households,
                period_from=period_from,
                period_to=period_to,
                as_of=as_of,
            )
        except Exception as exc:  # noqa: BLE001 - reported below, then re-raised
            db.rollback()
            print(f"Reconciliação falhou: {exc}")
            return 1

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    summary["dry_run"] = args.dry_run
    _print_report(summary, dry_run=args.dry_run)
    if args.output_path:
        with open(args.output_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(f"\nRelatório salvo em {args.output_path}")
    return 1 if summary["total_unresolved_divergences"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
