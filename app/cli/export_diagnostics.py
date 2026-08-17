import argparse
import csv
import hashlib
import json
import tempfile
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import _consolidated_expenses, _expense_signature
from app.db import SessionLocal
from app.models import (
    Account,
    Category,
    Document,
    FinancialProfile,
    Household,
    ReviewItem,
    Transaction,
)
from app.services.finance import add_months, money
from app.services.plan_workbook import select_household

TRANSACTION_FIELDS = (
    "date",
    "description",
    "amount",
    "calculation_contribution",
    "transaction_type",
    "calculation_status",
    "excluded",
    "possible_duplicate",
    "reviewed",
    "category",
    "account",
    "institution",
    "account_type",
    "owner",
    "document_name",
    "document_type",
    "source_line",
    "confidence",
    "installment",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exporta um diagnóstico financeiro mensal sem credenciais ou documentos brutos."
    )
    parser.add_argument("month", help="Competência no formato AAAA-MM")
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics"))
    parser.add_argument("--household", help="Nome exato da família quando houver mais de uma")
    return parser.parse_args()


def _parse_month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError("Mês deve usar o formato AAAA-MM") from exc


def _category_totals(rows: list[tuple[Transaction, str]]) -> list[dict[str, object]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for transaction, category_name in rows:
        totals[category_name] -= Decimal(transaction.amount)
    return [
        {"category": category, "amount": float(money(amount))}
        for category, amount in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if amount != 0
    ]


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_diagnostic(
    db: Session,
    household: Household,
    month: date,
    output_dir: Path,
) -> Path:
    start = month.replace(day=1)
    end = add_months(start, 1)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)

    raw_rows = db.execute(
        select(
            Transaction,
            Category.name,
            Account.name,
            Account.institution,
            Account.account_type,
            Document.original_name,
            Document.document_type,
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Account, Account.id == Transaction.account_id)
        .outerjoin(Document, Document.id == Transaction.document_id)
        .where(
            Transaction.household_id == household.id,
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
        )
        .order_by(Transaction.booked_at, Transaction.created_at, Transaction.id)
    ).all()
    consolidated_rows, duplicates_ignored = _consolidated_expenses(db, household.id, start, end)
    consolidated_ids = {transaction.id for transaction, _category in consolidated_rows}

    raw_expenses = [
        (transaction, str(category_name or "Revisar"))
        for transaction, category_name, *_metadata in raw_rows
        if transaction.transaction_type in {"expense", "refund"} and not transaction.excluded
    ]
    raw_spending = max(
        Decimal("0"),
        sum((-Decimal(transaction.amount) for transaction, _ in raw_expenses), Decimal("0")),
    )
    consolidated_spending = max(
        Decimal("0"),
        sum((-Decimal(transaction.amount) for transaction, _ in consolidated_rows), Decimal("0")),
    )

    transaction_rows: list[dict[str, object]] = []
    signature_groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for (
        transaction,
        category_name,
        account_name,
        institution,
        account_type,
        document_name,
        document_type,
    ) in raw_rows:
        is_expense = transaction.transaction_type in {"expense", "refund"}
        if transaction.excluded:
            calculation_status = "excluded_by_rule"
        elif not is_expense:
            calculation_status = "income_transfer_or_reconciliation"
        elif transaction.id in consolidated_ids:
            calculation_status = "counted"
        else:
            calculation_status = "overlap_ignored"
        contribution = -Decimal(transaction.amount) if transaction.id in consolidated_ids else Decimal("0")
        transaction_row = {
            "date": transaction.booked_at.isoformat(),
            "description": transaction.description,
            "amount": float(money(transaction.amount)),
            "calculation_contribution": float(money(contribution)),
            "transaction_type": transaction.transaction_type,
            "calculation_status": calculation_status,
            "excluded": transaction.excluded,
            "possible_duplicate": transaction.possible_duplicate,
            "reviewed": transaction.reviewed,
            "category": str(category_name or "Revisar"),
            "account": str(account_name or ""),
            "institution": str(institution or ""),
            "account_type": str(account_type or ""),
            "owner": transaction.owner_label,
            "document_name": str(document_name or "Entrada manual"),
            "document_type": str(document_type or "manual"),
            "source_line": transaction.source_line or "",
            "confidence": float(transaction.confidence),
            "installment": (
                f"{transaction.installment_current}/{transaction.installment_total}"
                if transaction.installment_current and transaction.installment_total
                else ""
            ),
        }
        transaction_rows.append(transaction_row)
        if document_type == "financial_plan_workbook" or (is_expense and not transaction.excluded):
            signature = _expense_signature(transaction, str(account_type or ""))
            signature_groups[signature].append(
                {
                    "transaction_id": transaction.id,
                    "description": transaction.description,
                    "amount": float(money(transaction.amount)),
                    "account": str(account_name or ""),
                    "document": str(document_name or "Entrada manual"),
                    "document_type": str(document_type or "manual"),
                    "status": calculation_status,
                }
            )

    duplicate_rows: list[dict[str, object]] = []
    for signature, group in signature_groups.items():
        sources = {(item["account"], item["document"]) for item in group}
        if len(group) < 2 or len(sources) < 2:
            continue
        group_id = hashlib.sha256(repr(signature).encode()).hexdigest()[:12]
        duplicate_rows.append(
            {
                "group_id": group_id,
                "occurrences": len(group),
                "counted": sum(item["status"] == "counted" for item in group),
                "ignored": sum(item["status"] == "overlap_ignored" for item in group),
                "amount_each": group[0]["amount"],
                "descriptions": " | ".join(sorted({str(item["description"]) for item in group})),
                "accounts": " | ".join(sorted({str(item["account"]) for item in group})),
                "documents": " | ".join(sorted({str(item["document"]) for item in group})),
            }
        )

    document_ids = {transaction.document_id for transaction, *_ in raw_rows if transaction.document_id}
    documents = (
        db.scalars(select(Document).where(Document.id.in_(document_ids)).order_by(Document.created_at)).all()
        if document_ids
        else []
    )
    document_rows = [
        {
            "name": item.original_name,
            "type": item.document_type,
            "status": item.status,
            "records": item.record_count,
            "notes": item.notes or "",
            "imported_at": item.created_at.isoformat() if item.created_at else "",
        }
        for item in documents
    ]

    account_ids = {transaction.account_id for transaction, *_ in raw_rows if transaction.account_id}
    accounts = (
        db.scalars(select(Account).where(Account.id.in_(account_ids)).order_by(Account.name)).all()
        if account_ids
        else []
    )
    account_rows = [
        {
            "name": item.name,
            "institution": item.institution,
            "type": item.account_type,
            "owner": item.owner_label,
            "active": item.active,
        }
        for item in accounts
    ]

    review_rows = db.execute(
        select(ReviewItem, Transaction)
        .join(Transaction, Transaction.id == ReviewItem.transaction_id)
        .where(
            ReviewItem.household_id == household.id,
            ReviewItem.status == "open",
            Transaction.booked_at >= start,
            Transaction.booked_at < end,
        )
        .order_by(Transaction.booked_at, ReviewItem.created_at)
    ).all()
    reviews = [
        {
            "date": transaction.booked_at.isoformat(),
            "description": transaction.description,
            "amount": float(money(transaction.amount)),
            "reason": review.reason,
            "details": review.details or "",
        }
        for review, transaction in review_rows
    ]

    profile = db.scalar(select(FinancialProfile).where(FinancialProfile.household_id == household.id))
    top_contributors = sorted(
        (
            {
                "date": transaction.booked_at.isoformat(),
                "description": transaction.description,
                "amount": float(money(-Decimal(transaction.amount))),
                "category": category_name,
            }
            for transaction, category_name in consolidated_rows
            if transaction.amount < 0
        ),
        key=lambda item: item["amount"],
        reverse=True,
    )[:30]
    summary = {
        "period": start.strftime("%Y-%m"),
        "generated_at_utc": generated_at.isoformat(),
        "household": household.name,
        "cash_cap": float(money(profile.monthly_cash_cap)) if profile else 0,
        "raw_spending": float(money(raw_spending)),
        "consolidated_spending": float(money(consolidated_spending)),
        "overlap_reduction": float(money(raw_spending - consolidated_spending)),
        "transactions_in_month": len(raw_rows),
        "raw_expense_rows": len(raw_expenses),
        "consolidated_expense_rows": len(consolidated_rows),
        "excluded_rows": sum(transaction.excluded for transaction, *_ in raw_rows),
        "overlap_rows_ignored": duplicates_ignored,
        "open_reviews_in_month": len(reviews),
        "documents_in_month": len(documents),
        "accounts_in_month": len(accounts),
        "raw_categories": _category_totals(raw_expenses),
        "consolidated_categories": _category_totals(consolidated_rows),
        "top_contributors": top_contributors,
    }

    month_key = start.strftime("%Y-%m")
    filename = f"diagnostico-financeiro-{month_key}-{generated_at:%Y%m%dT%H%M%SZ}.zip"
    output_path = output_dir / filename
    with tempfile.TemporaryDirectory(prefix="financial-diagnostic-") as temporary:
        workdir = Path(temporary)
        (workdir / "resumo.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_csv(workdir / "lancamentos.csv", TRANSACTION_FIELDS, transaction_rows)
        _write_csv(
            workdir / "possiveis_duplicidades.csv",
            (
                "group_id",
                "occurrences",
                "counted",
                "ignored",
                "amount_each",
                "descriptions",
                "accounts",
                "documents",
            ),
            duplicate_rows,
        )
        _write_csv(
            workdir / "documentos.csv",
            ("name", "type", "status", "records", "notes", "imported_at"),
            document_rows,
        )
        _write_csv(
            workdir / "contas.csv",
            ("name", "institution", "type", "owner", "active"),
            account_rows,
        )
        _write_csv(
            workdir / "revisoes_abertas.csv",
            ("date", "description", "amount", "reason", "details"),
            reviews,
        )
        (workdir / "LEIA-ME.txt").write_text(
            "Diagnóstico financeiro somente de leitura.\n"
            "Inclui descrições e valores do mês para conferência.\n"
            "Não inclui senhas, hashes de autenticação, chaves, arquivos criptografados ou PDFs.\n"
            "Envie o ZIP apenas por um canal privado.\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(workdir.iterdir()):
                archive.write(item, arcname=item.name)
    return output_path


def main() -> int:
    args = _arguments()
    month = _parse_month(args.month)
    with SessionLocal() as db:
        household = select_household(db, args.household)
        output_path = export_diagnostic(db, household, month, args.output_dir)
    print("Diagnóstico gerado com sucesso.")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
