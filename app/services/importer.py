import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.services.classifier import normalize_description


@dataclass(frozen=True)
class ParsedTransaction:
    booked_at: date
    description: str
    amount: Decimal
    source_line: int
    card_last_four: str | None = None
    installment_current: int | None = None
    installment_total: int | None = None


@dataclass(frozen=True)
class ParsedPayroll:
    person_name: str
    competence: date
    payment_date: date
    gross_amount: Decimal
    deductions: Decimal
    net_amount: Decimal
    payroll_loan: Decimal


def file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_decimal(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    if isinstance(value, (int, float)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    cleaned = str(value).strip().replace("R$", "").replace(" ", "")
    negative_parentheses = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-+]", "", cleaned)
    try:
        result = Decimal(cleaned or "0")
    except InvalidOperation as exc:
        raise ValueError(f"Valor monetário inválido: {value}") from exc
    if negative_parentheses:
        result = -result
    return result.quantize(Decimal("0.01"))


def parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value[:10] if fmt != "%Y%m%d" else value[:8], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Data inválida: {value}")


def _installment(description: str) -> tuple[int | None, int | None]:
    match = re.search(r"(?:PARCELA\s*)?(\d{1,2})\s*/\s*(\d{1,2})", description.upper())
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def _normalize_credit_card_amount(description: str, amount: Decimal) -> Decimal:
    normalized = normalize_description(description)
    if "PAGAMENTO RECEBIDO" in normalized or "PAGAMENTO FATURA" in normalized:
        return abs(amount)
    if "ESTORNO" in normalized or "CREDITO" in normalized:
        return abs(amount)
    # Itaú invoices and the Nubank export use positive values for purchases and
    # negative values for credits, even though both appear in the same column.
    if amount < 0:
        return abs(amount)
    return -abs(amount)


def parse_csv(payload: bytes, document_type: str) -> list[ParsedTransaction]:
    decoded = payload.decode("utf-8-sig", errors="replace")
    sample = decoded[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = csv.DictReader(io.StringIO(decoded), dialect=dialect)
    if not rows.fieldnames:
        raise ValueError("CSV sem cabeçalho")
    keys = {normalize_description(key).lower().replace(" ", "_"): key for key in rows.fieldnames if key}
    date_key = next(
        (keys[k] for k in ("date", "data", "transaction_date", "booking_date") if k in keys), None
    )
    description_key = next(
        (keys[k] for k in ("title", "descricao", "description", "memo", "estabelecimento") if k in keys), None
    )
    amount_key = next(
        (keys[k] for k in ("amount", "valor", "value", "transaction_amount") if k in keys), None
    )
    if not all((date_key, description_key, amount_key)):
        raise ValueError("CSV precisa conter colunas de data, descrição e valor")
    parsed: list[ParsedTransaction] = []
    for line, row in enumerate(rows, start=2):
        if not row.get(date_key) or not row.get(amount_key):
            continue
        description = (row.get(description_key) or "Sem descrição").strip()
        amount = parse_decimal(row[amount_key])
        if document_type == "credit_card":
            amount = _normalize_credit_card_amount(description, amount)
        current, total = _installment(description)
        parsed.append(
            ParsedTransaction(parse_date(row[date_key]), description, amount, line, None, current, total)
        )
    return parsed


def _ofx_tag(block: str, name: str) -> str | None:
    match = re.search(rf"<{name}>\s*([^<\r\n]+)", block, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_ofx(payload: bytes) -> list[ParsedTransaction]:
    text = payload.decode("latin-1", errors="replace")
    blocks = re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", text, re.IGNORECASE | re.DOTALL)
    parsed: list[ParsedTransaction] = []
    for line, block in enumerate(blocks, start=1):
        raw_date = _ofx_tag(block, "DTPOSTED")
        raw_amount = _ofx_tag(block, "TRNAMT")
        description = _ofx_tag(block, "MEMO") or _ofx_tag(block, "NAME") or "Lançamento OFX"
        if not raw_date or raw_amount is None:
            continue
        parsed.append(
            ParsedTransaction(parse_date(raw_date[:8]), description, parse_decimal(raw_amount), line)
        )
    if not parsed:
        raise ValueError("Nenhuma transação reconhecida no OFX")
    return parsed


def _pdf_text(payload: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("Leitor de PDF indisponível") from exc
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        raise ValueError("PDF inválido, protegido ou incompleto; encaminhado para revisão") from exc


def _reference_date(text: str) -> date:
    for pattern in (
        r"EMISS[ÃA]O\s*:\s*(\d{2}/\d{2}/\d{4})",
        r"VENCIMENTO\s*:\s*(\d{2}/\d{2}/\d{4})",
        r"PER[IÍ]ODO DE VISUALIZA[CÇ][AÃ]O:\s*\d{2}/\d{2}/\d{4}\s+AT[EÉ]\s+(\d{2}/\d{2}/\d{4})",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return parse_date(match.group(1))
    return date.today()


def _card_date(raw_date: str, reference: date) -> date:
    day, month = (int(value) for value in raw_date.split("/"))
    year = reference.year - 1 if month > reference.month + 1 else reference.year
    return date(year, month, day)


def parse_credit_card_pdf(payload: bytes) -> list[ParsedTransaction]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("Leitor de PDF indisponível") from exc
    text = _pdf_text(payload)
    reference = _reference_date(text)
    line_pattern = re.compile(r"^\s*(\d{2}/\d{2})(?!/)\s+(.+?)\s+(-?\s*[\d.]+,\d{2})\s*$")
    parsed: list[ParsedTransaction] = []
    line_number = 0
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            for page in pdf.pages:
                # Itaú places two independent transaction tables side by side.
                # Cropping prevents a row from one card being joined to another.
                split = page.width * 0.60
                for x0, x1 in ((0, split), (split, page.width)):
                    column = page.crop((x0, 0, x1, page.height)).extract_text() or ""
                    card_last_four = None
                    for raw_line in column.splitlines():
                        line_number += 1
                        card_match = re.search(r"final\s*(\d{4})", raw_line, re.IGNORECASE)
                        if card_match:
                            card_last_four = card_match.group(1)
                        match = line_pattern.match(raw_line)
                        if not match:
                            continue
                        raw_date, description, raw_amount = match.groups()
                        amount = _normalize_credit_card_amount(description, parse_decimal(raw_amount))
                        current, total = _installment(description)
                        parsed.append(
                            ParsedTransaction(
                                reference.replace(day=1),
                                description.strip(),
                                amount,
                                line_number,
                                card_last_four,
                                current,
                                total,
                            )
                        )
    except Exception as exc:
        raise ValueError("PDF inválido, protegido ou incompleto; encaminhado para revisão") from exc
    if not parsed:
        raise ValueError("Fatura sem compras textuais reconhecíveis; encaminhada para revisão")
    return parsed


def parse_bank_statement_pdf(payload: bytes) -> list[ParsedTransaction]:
    text = _pdf_text(payload)
    line_pattern = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\s+(.+?)\s+(-?\s*[\d.]+,\d{2})\s*$")
    parsed: list[ParsedTransaction] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        match = line_pattern.match(raw_line)
        if not match:
            continue
        raw_date, description, raw_amount = match.groups()
        normalized = normalize_description(description)
        if normalized.startswith("SALDO DO DIA") or normalized.startswith("SALDO ANTERIOR"):
            continue
        parsed.append(
            ParsedTransaction(
                parse_date(raw_date), description.strip(), parse_decimal(raw_amount), line_number
            )
        )
    if not parsed:
        raise ValueError("Extrato sem lançamentos textuais reconhecíveis; encaminhado para revisão")
    return parsed


MONTHS_PT = {
    "JANEIRO": 1,
    "FEVEREIRO": 2,
    "MARCO": 3,
    "MARÇO": 3,
    "ABRIL": 4,
    "MAIO": 5,
    "JUNHO": 6,
    "JULHO": 7,
    "AGOSTO": 8,
    "SETEMBRO": 9,
    "OUTUBRO": 10,
    "NOVEMBRO": 11,
    "DEZEMBRO": 12,
}


def parse_payroll_pdf(payload: bytes) -> ParsedPayroll:
    text = _pdf_text(payload)
    header = re.search(
        r"M[ÊE]S:\s*([A-ZÇÃ]+)/(\d{4})\s+CR[EÉ]DITO:\s*(\d{2}/\d{2}/\d{4})"
        r"\s+L[ÍI]QUIDO A RECEBER:\s*R\$\s*([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    totals = re.search(
        r"TOTAL GANHOS\s+TOTAL DESCONTOS.*?\n\s*([\d.]+,\d{2})\s+([\d.]+,\d{2})\s+([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    person = re.search(r"IDENTIFICA[CÇ][AÃ]O\s*\n\s*\d+\s*-\s*([^\n]+)", text, re.IGNORECASE)
    if not header or not totals or not person:
        raise ValueError("Holerite sem os campos obrigatórios; encaminhado para revisão")
    month_name, year, payment_date, header_net = header.groups()
    month = MONTHS_PT.get(month_name.upper())
    if not month:
        raise ValueError(f"Mês não reconhecido no holerite: {month_name}")
    gross, deductions, total_net = (parse_decimal(value) for value in totals.groups())
    net = parse_decimal(header_net)
    if net != total_net:
        raise ValueError("O valor líquido do holerite não confere com o total")
    loan_match = re.search(r"BANCO\s+BRASIL\s+\d{1,3}/\d{1,3}\s+([\d.]+,\d{2})", text, re.IGNORECASE)
    return ParsedPayroll(
        person_name=person.group(1).strip(),
        competence=date(int(year), month, 1),
        payment_date=parse_date(payment_date),
        gross_amount=gross,
        deductions=deductions,
        net_amount=net,
        payroll_loan=parse_decimal(loan_match.group(1)) if loan_match else Decimal("0"),
    )


def parse_pdf(payload: bytes, document_type: str) -> list[ParsedTransaction]:
    if document_type == "credit_card":
        return parse_credit_card_pdf(payload)
    if document_type == "bank_statement":
        return parse_bank_statement_pdf(payload)
    raise ValueError("Tipo de PDF não suportado para lançamentos")


def parse_document(filename: str, payload: bytes, document_type: str) -> list[ParsedTransaction]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".csv", ".txt"}:
        return parse_csv(payload, document_type)
    if suffix in {".ofx", ".qfx"}:
        return parse_ofx(payload)
    if suffix == ".pdf":
        return parse_pdf(payload, document_type)
    raise ValueError("Formato não suportado. Use PDF textual, CSV ou OFX")


def transaction_fingerprint(
    account_id: str | None, item: ParsedTransaction, owner_label: str = "Família"
) -> str:
    normalized = normalize_description(item.description)
    payload = "|".join(
        [
            account_id or "none",
            item.booked_at.isoformat(),
            f"{item.amount:.2f}",
            normalized,
            owner_label,
            str(item.installment_current or ""),
            str(item.installment_total or ""),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
