import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Document
from app.services.crypto import EncryptedDocumentStore
from app.services.importer import file_sha256
from app.services.plan_workbook import (
    add_import_audit,
    import_plan_data,
    parse_plan_workbook,
    select_household,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Carrega a planilha consolidada no banco local sem duplicar registros."
    )
    parser.add_argument("workbook", type=Path, help="Caminho da planilha .xlsx")
    parser.add_argument("--household", help="Nome exato da família quando houver mais de uma")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Grava a carga; sem esta opção apenas exibe a prévia e desfaz a transação",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    payload = args.workbook.read_bytes()
    data = parse_plan_workbook(args.workbook)
    with SessionLocal() as db:
        household = select_household(db, args.household)
        document_id = None
        document = None
        if args.apply:
            digest = file_sha256(payload)
            document = db.scalar(
                select(Document).where(
                    Document.household_id == household.id,
                    Document.sha256 == digest,
                )
            )
            if document is None:
                document = Document(
                    household_id=household.id,
                    original_name=args.workbook.name,
                    document_type="financial_plan_workbook",
                    sha256=digest,
                    encrypted_path="pending",
                    status="processing",
                )
                db.add(document)
                db.flush()
                document.encrypted_path = EncryptedDocumentStore().save(document.id, payload)
            document_id = document.id

        stats = import_plan_data(db, household, data, document_id)
        summary = stats.as_dict()
        summary["workbook"] = {
            "transactions_read": len(data.transactions),
            "commissions_read": len(data.commissions),
            "payroll_records_read": len(data.payroll),
            "obligations_read": len(data.obligations),
            "planning_questions_read": len(data.reviews),
        }
        if args.apply:
            document.status = "imported"
            document.record_count = len(data.transactions)
            document.notes = "Carga idempotente da planilha financeira consolidada"
            add_import_audit(db, household, stats, args.workbook.name)
            db.commit()
            print("Carga concluída e gravada no banco local.")
        else:
            db.rollback()
            print("Prévia concluída. Nenhuma alteração foi gravada.")
            print("Execute novamente com --apply para confirmar a carga.")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
