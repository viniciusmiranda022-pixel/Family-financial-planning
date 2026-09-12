import csv
import hashlib
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.services.classifier import (
    FEE_PATTERN,
    MONTHS_PT_ABBR,
    PAYMENT_PATTERN,
    REFUND_PATTERN,
    normalize_description,
)

PARSER_CONTRACT_VERSION = "2026.09.4"


@dataclass(frozen=True)
class ParsedTransaction:
    booked_at: date
    description: str
    amount: Decimal
    source_line: int
    card_last_four: str | None = None
    installment_current: int | None = None
    installment_total: int | None = None
    occurred_at: date | None = None


@dataclass(frozen=True)
class ParsedPayroll:
    person_name: str
    competence: date
    payment_date: date
    gross_amount: Decimal
    deductions: Decimal
    net_amount: Decimal
    payroll_loan: Decimal


@dataclass(frozen=True)
class ParsedDocument:
    document_type: str
    parser_name: str
    parser_version: str
    transactions: tuple[ParsedTransaction, ...] = ()
    payroll: ParsedPayroll | None = None
    declared_fields: Mapping[str, Decimal | str | None] = field(default_factory=dict)
    calculated_fields: Mapping[str, Decimal | str | None] = field(default_factory=dict)
    reconciliation_formula: str = "unsupported"
    coverage: Mapping[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    period: str | None = None


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


# `PARCELA 12/12` (Itaú/Nubank) and `Parcela 4 de 18` (Mercado Pago) are the
# only two installment phrasings observed across issuers; both require the
# literal "PARCELA" word when the separator is the word "DE" instead of "/"
# so a coincidental "<number> de <number>" in an unrelated description can't
# be misread as an installment.
_INSTALLMENT_SLASH = re.compile(r"PARCELA\s*(\d{1,2})\s*/\s*(\d{1,2})")
_INSTALLMENT_DE = re.compile(r"PARCELA\s*(\d{1,2})\s*DE\s*(\d{1,2})")


def _installment(description: str) -> tuple[int | None, int | None]:
    upper = description.upper()
    match = _INSTALLMENT_SLASH.search(upper) or _INSTALLMENT_DE.search(upper)
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def _normalize_credit_card_amount(description: str, amount: Decimal) -> Decimal:
    normalized = normalize_description(description)
    if PAYMENT_PATTERN.search(normalized) or REFUND_PATTERN.search(normalized):
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
        booked_at = parse_date(row[date_key])
        parsed.append(
            ParsedTransaction(
                booked_at,
                description,
                amount,
                line,
                None,
                current,
                total,
                occurred_at=booked_at,
            )
        )
    if not parsed:
        raise ValueError("CSV sem lançamentos financeiros reconhecidos")
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


# Some issuers' textual PDFs (observed in Nubank exports) encode a negative
# amount with a Unicode minus sign or dash variant instead of ASCII "-", and
# a non-breaking space between the sign and "R$". `parse_decimal` (and every
# regex below) only recognizes ASCII "-"; translating these once, right at
# extraction, keeps that single behavior correct for every format instead of
# teaching each format-specific parser its own copy of this normalization.
# Plain ASCII "-" is untouched, so Itaú's existing output is unaffected.
_PDF_TEXT_TRANSLATION = str.maketrans(
    {
        "\u2212": "-",  # minus sign
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u00a0": " ",  # non-breaking space
    }
)


def _pdf_text(payload: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("Leitor de PDF indisponível") from exc
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        raise ValueError("PDF inválido, protegido ou incompleto; encaminhado para revisão") from exc
    return raw.translate(_PDF_TEXT_TRANSLATION)


# A brand token alone ("NUBANK", "MERCADO PAGO") is not safe issuer
# evidence: it also occurs as a *counterparty* inside another bank's own
# transaction text -- confirmed against the real Itaú statement behind this
# Work Order's baseline, which contains lines like
# "PAG BOLETO NU PAGAMENTOS SA -1.724,90" and "PIX QRS MERCADO PAG...".
# Detection therefore requires the brand token *and* one of these
# document-identity/layout markers -- section headers and date formats that
# only the issuer's own masthead/summary prints, never a transaction
# description naming that issuer as a counterparty on someone else's
# statement.
_NUBANK_STRUCTURE = re.compile(
    r"RESUMO DA FATURA ATUAL"
    r"|TRANSA[ÇC][ÕO]ES DE"
    r"|FATURA\s+\d{2}\s+[A-ZÇ]{3}\s+\d{4}"
    r"|SALDO FINAL DO PER[IÍ]ODO"
    r"|\d{1,2}\s+DE\s+[A-ZÇ]+\s+DE\s+\d{4}\s+A\s+\d{1,2}\s+DE\s+[A-ZÇ]+\s+DE\s+\d{4}",
    re.IGNORECASE,
)
_MERCADO_PAGO_STRUCTURE = re.compile(
    r"DETALHES DE CONSUMO"
    r"|MOVIMENTA[ÇC][ÕO]ES NA FATURA"
    r"|PER[IÍ]ODO\s*:?\s*DE\s+\d{2}-\d{2}-\d{4}\s+(?:AL|AT[EÉ])\s+\d{2}-\d{2}-\d{4}",
    re.IGNORECASE,
)

# PR #41 engineer review (head `c260aed`): falling back to Itaú unconditionally
# whenever Nubank/MP do not match is not positive identification -- any
# unsupported textual PDF that happens to contain a `dd/mm(/yyyy)
# description amount` row would be silently accepted as Itaú instead of
# being rejected for review. Itaú needs the same treatment already applied
# to Nubank/MP above: a document-identity/layout marker, not just a
# transaction-row shape generic enough that an unrelated document could
# produce it by accident.
#
# PR #41 engineer review (head `e42a470`), second round: the first
# correction above (`_ITAU_STRUCTURE` alone) is still not an identity
# signature -- "SALDO ANTERIOR"/"SALDO DO DIA"/"SALDO FINAL EM"/"EMISSAO:"
# are generic Portuguese banking/document labels that an unrelated
# institution's statement could print too, so a third-party PDF containing
# only that vocabulary was still silently routed to Itaú. The engineer
# separately confirmed against the real supported Itaú statement (kept out
# of Git) that it also carries explicit Itaú brand identity ("Itaú"/
# "itau.com.br") alongside this balance vocabulary. Detection now requires
# both, exactly the same "brand token + own layout marker" combination
# already used for Nubank/Mercado Pago above -- a bare "SALDO DO DIA" is
# structure, not identity, and must not be enough by itself.
_ITAU_STRUCTURE = re.compile(
    r"SALDO\s+ANTERIOR|SALDO\s+DO\s+DIA|SALDO\s+FINAL\s+EM|EMISS[ÃA]O\s*:",
    re.IGNORECASE,
)

# PR #41 engineer review (head `82db527`), third round: a bare "ITAU" token
# anywhere in the normalized text is still not identity -- it is satisfied
# by an ordinary counterparty/transaction description naming Itaú on
# someone else's statement (e.g. "PIX TRANSF ITAU ..."), the exact
# cross-institution collision class already fixed above for Nubank/Mercado
# Pago. The engineer confirmed the real supported Itaú statement carries
# issuer-owned identity text ("itau.com.br") that an ordinary transaction
# description cannot produce; the existing synthetic fixtures already model
# this with "Itau Unibanco S.A." / "www.itau.com.br" masthead lines. Both
# patterns are matched against the *normalized* text, where
# `normalize_description` has already turned "itau.com.br" into
# "ITAU COM BR" (dots become spaces) -- so this is checked here rather than
# with `_ITAU_STRUCTURE` above, which matches the raw, un-normalized text.
_ITAU_IDENTITY = re.compile(r"ITAU\s+UNIBANCO|ITAU\s+COM\s+BR")


def _detect_pdf_issuer(text: str) -> str:
    """Identify which institution's textual layout produced this PDF.

    Detection is content-based only, per the Work Order's requirement: it
    looks for the issuer's own document-identity markers inside the
    *extracted* text, never the uploaded filename and never personal data --
    an institution name is not PII. A brand mention by itself is not enough
    (see `_NUBANK_STRUCTURE`/`_MERCADO_PAGO_STRUCTURE` above); it must
    co-occur with a layout marker that issuer's own document prints, never
    a counterparty. Itaú requires the identical combination -- its own
    owned identity marker (`_ITAU_IDENTITY`: "Itaú Unibanco" or
    "itau.com.br", never a bare "Itaú" token that an ordinary transaction
    description could also contain) *and* its balance/emission vocabulary
    (`_ITAU_STRUCTURE`) -- rather than being treated as the unconditional
    fallback or accepted on generic vocabulary alone. Anything matching none
    of the three known signatures is `"unknown"`: the caller must reject it
    for review rather than guess, so an unsupported or ambiguous PDF can
    never be silently ingested as if it were a supported issuer.
    """

    normalized = normalize_description(text)
    if ("NUBANK" in normalized or "NU PAGAMENTOS" in normalized) and _NUBANK_STRUCTURE.search(text):
        return "nubank"
    if (
        "MERCADO PAGO" in normalized or "MERCADOPAGO" in normalized
    ) and _MERCADO_PAGO_STRUCTURE.search(text):
        return "mercado_pago"
    if _ITAU_IDENTITY.search(normalized) and _ITAU_STRUCTURE.search(text):
        return "itau"
    return "unknown"


_UNSUPPORTED_PDF_ISSUER_ERROR = (
    "PDF não identificado como Itaú, Nubank ou Mercado Pago; encaminhado para revisão"
)


# Vocabulary observed on Itaú/Nubank/Mercado Pago invoices that shares the
# shape of a transaction line (a trailing "R$ value") but is a limit,
# simulated-interest or minimum-payment disclosure, never a real
# transaction. Work Order requirement: these must never become a
# transaction just because they end in a money value.
_NON_TRANSACTION_TEXT = re.compile(
    r"LIMITE|SIMULA|M[ÍI]NIMO|EFETIV|ROTATIVO|\bCET\b|IOF SOBRE|ENCARGOS DE ATRASO"
)


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


def _parse_itau_credit_card_pdf(payload: bytes) -> list[ParsedTransaction]:
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
                                _card_date(raw_date, reference),
                            )
                        )
    except Exception as exc:
        raise ValueError("PDF inválido, protegido ou incompleto; encaminhado para revisão") from exc
    if not parsed:
        raise ValueError("Fatura sem compras textuais reconhecíveis; encaminhada para revisão")
    return parsed


def _parse_itau_bank_statement_pdf(text: str) -> list[ParsedTransaction]:
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


def _nubank_reference_date(text: str) -> date:
    match = re.search(r"FATURA\s+(\d{2})\s+([A-ZÇ]{3})\s+(\d{4})", text, re.IGNORECASE)
    if match:
        day, month_abbr, year = match.groups()
        month = MONTHS_PT_ABBR.get(month_abbr.upper())
        if month:
            return date(int(year), month, int(day))
    return _reference_date(text)


def _nubank_card_date(day: int, month: int, reference: date) -> date:
    year = reference.year - 1 if month > reference.month + 1 else reference.year
    return date(year, month, day)


def _combine_money_sign(sign_before_currency: str, raw_amount: str) -> str:
    """Normalize a "-" that may sit right before "R$" instead of right
    before the digits (Nubank prints negative amounts as "-R$ 200,00", with
    no space to separate the sign from the currency symbol) into a single
    leading "-" `parse_decimal` understands. Either position, or neither,
    means the same thing.
    """

    if sign_before_currency == "-" or raw_amount.startswith("-"):
        return f"-{raw_amount.lstrip('-')}"
    return raw_amount


_NUBANK_CARD_START = re.compile(r"^(\d{2})\s+([A-ZÇ]{3})\s+(.*)$", re.IGNORECASE)
_NUBANK_CARD_TAIL = re.compile(r"(.*?)\s*(-?)\s*R\$\s*(-?[\d.]+,\d{2})\s*$")


def _parse_nubank_credit_card_pdf(text: str) -> list[ParsedTransaction]:
    reference = _nubank_reference_date(text)
    marker = re.search(r"TRANSA[ÇC][ÕO]ES DE", text, re.IGNORECASE)
    if not marker:
        raise ValueError("Fatura Nubank sem bloco de transações reconhecível; encaminhada para revisão")
    parsed: list[ParsedTransaction] = []
    pending_date: date | None = None
    pending_parts: list[str] = []
    for line_number, raw_line in enumerate(text[marker.end() :].splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        start = _NUBANK_CARD_START.match(line)
        if start:
            day, month_abbr, rest = start.groups()
            month = MONTHS_PT_ABBR.get(month_abbr.upper())
            if not month:
                pending_date = None
                continue
            pending_date = _nubank_card_date(int(day), month, reference)
            pending_parts = [rest.strip()]
        elif pending_date is not None:
            pending_parts.append(line)
        else:
            continue
        joined = " ".join(part for part in pending_parts if part)
        money = _NUBANK_CARD_TAIL.match(joined)
        if not money:
            continue
        description = money.group(1).strip()
        if description and not _NON_TRANSACTION_TEXT.search(normalize_description(description)):
            raw_amount = _combine_money_sign(money.group(2), money.group(3))
            amount = _normalize_credit_card_amount(description, parse_decimal(raw_amount))
            current, total = _installment(description)
            parsed.append(
                ParsedTransaction(
                    reference.replace(day=1),
                    description,
                    amount,
                    line_number,
                    None,
                    current,
                    total,
                    pending_date,
                )
            )
        pending_date = None
        pending_parts = []
    if not parsed:
        raise ValueError("Fatura Nubank sem compras textuais reconhecíveis; encaminhada para revisão")
    return parsed


# Real extracted Nubank bank-statement text (reproduced against the actual
# PDFs behind the 2026-09-09 initial-load dry-run, outside Git -- see
# `docs/WORK_ORDER_NUBANK_BANK_STATEMENT_PDFPLUMBER_LAYOUT.md`) does not put
# each transaction's amount on its own bare line the way the PR #41 baseline
# assumed. `pdfplumber` instead extracts the amount at the *end* of the
# transaction's own description line, and any further counterparty/bank
# metadata for that same transaction (agency, account, second bank name)
# keeps extracting on the line(s) that follow it, e.g.:
#
#   14 AGO 2026 Total de entradas + 300,00
#   Transferência recebida pelo Pix Pessoa Exemplo - 300,00
#   BANCO EXEMPLO S.A. Agência: 1 Conta: 0000
#   Total de saídas - 300,00
#   Transferência enviada pelo Pix Pessoa Exemplo - BANCO DESTINO 300,00
#   Agência: 2 Conta: 1111
#
# A minority of statements (and the fixture pinned by PR #41's regression
# test) still wrap the description across several lines and terminate with a
# bare amount line instead -- that shape is real too and must keep working
# unchanged. Both shapes are therefore supported by the same state machine:
#
# - the day header and the *first* section aggregate for that day share one
#   line; a later section switch within the same day (entradas -> saídas or
#   the reverse) repeats only the bare "Total de ..." aggregate, never the
#   date;
# - a line that is *only* an amount closes the transaction accumulated so
#   far (the legacy bare-amount-line shape);
# - a line that *ends* in an amount closes a transaction built from that
#   line's own leading text plus whatever was accumulated before it (the
#   real inline-amount shape); either way, direction always comes from
#   whichever aggregate section is currently open, never from the amount's
#   own sign or the description text (Work Order requirement: never infer
#   direction from the transaction line);
# - once a transaction has been emitted this way, any further plain-text
#   lines before the next boundary (a new day/section header, a "Saldo"/
#   "Rendimento" line, a footer/disclaimer marker, or another line ending in
#   an amount) are trailing metadata for that *same* transaction, appended
#   to its description -- never fabricated into a transaction of their own,
#   and never merged into the transaction that follows a header/footer
#   boundary;
# - footer/disclaimer text (issuer contact/ombudsman/CNPJ boilerplate) is
#   recognized and discarded outright: it is never appended as metadata and
#   can never become a transaction even if it happens to end in a
#   money-shaped value.
#
# The aggregate line itself is always discarded, never a transaction, so
# per-day/period totals can never leak into the ledger.
_NUBANK_DAY_SECTION_HEADER = re.compile(
    r"^(\d{2})\s+([A-ZÇ]{3})\s+(\d{4})\s+TOTAL DE (ENTRADAS|SA[IÍ]DAS)\b", re.IGNORECASE
)
_NUBANK_SECTION_HEADER = re.compile(r"^TOTAL DE (ENTRADAS|SA[IÍ]DAS)\b", re.IGNORECASE)
_NUBANK_BARE_AMOUNT = re.compile(r"^-?[\d.]+,\d{2}$")
# A transaction line in the real inline-amount layout: arbitrary leading
# text (the description, and possibly a trailing "-" the document uses as a
# plain separator, never a sign -- direction always comes from the current
# section) followed by the amount at the very end of the line. No "R$", no
# currency symbol: Nubank's bank-statement lines never carry one, unlike its
# credit-card invoice lines (`_NUBANK_CARD_TAIL`).
_NUBANK_INLINE_AMOUNT_TAIL = re.compile(r"^(.*\S)\s+([\d.]+,\d{2})$")
# Issuer contact/ombudsman/corporate boilerplate observed on real Nubank
# statement footers. Content-based, like `_detect_pdf_issuer`: matched
# against the same accent-/punctuation-stripped `normalize_description`
# text every other classification in this module already uses, never a
# filename or document position. Deliberately narrow -- a false negative
# only means a footer line is kept as harmless trailing metadata text (it
# still can never *become* a transaction, since it has no money value of
# its own), while a false positive would silently drop real transaction
# metadata, which is the worse failure mode.
_NUBANK_STATEMENT_FOOTER = re.compile(
    r"\bOUVIDORIA\b|\bSAC\b|CENTRAL DE ATENDIMENTO|ATENDIMENTO AO CLIENTE|"
    r"NU PAGAMENTOS|\bCNPJ\b"
)


def _strip_trailing_separator(text: str) -> str:
    """Drop a bare trailing "-" the inline-amount layout leaves behind.

    `_NUBANK_INLINE_AMOUNT_TAIL` cannot tell a "-" that sits directly before
    the amount (formatting punctuation the document itself never intends as
    a sign -- see the module comment above) from one that is genuinely part
    of the description text, because both look identical once the amount is
    stripped off. Only a "-" at the very end of the assembled description is
    unambiguous filler; a "-" anywhere else (e.g. "Pessoa Exemplo - BANCO
    DESTINO") is real content and is left untouched.
    """

    return text.rstrip(" -").strip()


def _parse_nubank_bank_statement_pdf(text: str) -> list[ParsedTransaction]:
    parsed: list[ParsedTransaction] = []
    current_day: date | None = None
    current_sign: int | None = None
    pending_parts: list[str] = []
    # True once an inline-amount transaction has been emitted and no
    # transaction-closing line has resolved since: any text accumulated in
    # `pending_parts` while this holds is trailing metadata continuation for
    # that already-emitted transaction (Work Order requirement 5), never the
    # start of the *next* transaction's own description. False means
    # `pending_parts` (if any) is instead a description still being built
    # for a not-yet-closed bare-amount-line transaction (legacy layout), so
    # it belongs to whichever line closes it next. Reset to False by
    # `attach_trailing_metadata()` (whatever was pending has been resolved)
    # and after a bare-format emit; set True after an inline emit.
    pending_is_trailing_metadata = False

    def attach_trailing_metadata() -> None:
        # Text accumulated since the last emitted transaction that never
        # resolved into an amount of its own: the real layout continues a
        # transaction's counterparty/bank metadata on lines *after* the one
        # carrying its inline amount (Work Order requirement 5). Attach it
        # to the transaction just emitted instead of discarding it or
        # fabricating a second, amount-less transaction from it. If nothing
        # has been emitted yet, there is nothing to attach it to and it is
        # simply dropped -- never turned into a fabricated transaction.
        nonlocal pending_parts, pending_is_trailing_metadata
        extra = " ".join(part for part in pending_parts if part).strip()
        pending_parts = []
        pending_is_trailing_metadata = False
        if not extra or not parsed:
            return
        last = parsed[-1]
        parsed[-1] = ParsedTransaction(
            last.booked_at,
            f"{last.description} {extra}".strip(),
            last.amount,
            last.source_line,
            last.card_last_four,
            last.installment_current,
            last.installment_total,
            last.occurred_at,
        )

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        day_section = _NUBANK_DAY_SECTION_HEADER.match(line)
        if day_section:
            attach_trailing_metadata()
            day, month_abbr, year, direction = day_section.groups()
            month = MONTHS_PT_ABBR.get(month_abbr.upper())
            current_day = date(int(year), month, int(day)) if month else None
            current_sign = 1 if direction.upper().startswith("ENTRADA") else -1
            continue
        if current_day is None:
            # Everything before the first per-day block -- the period-level
            # "Saldo inicial"/"Rendimento líquido"/"Total de entradas/saídas
            # (período)"/"Saldo final do período" summary -- is declared-field
            # territory (`_statement_declared_fields`), never a transaction.
            continue
        section = _NUBANK_SECTION_HEADER.match(line)
        if section:
            attach_trailing_metadata()
            current_sign = 1 if section.group(1).upper().startswith("ENTRADA") else -1
            continue
        normalized = normalize_description(line)
        if normalized.startswith("SALDO") or normalized.startswith("RENDIMENTO"):
            attach_trailing_metadata()
            continue
        if _NUBANK_STATEMENT_FOOTER.search(normalized):
            # Footer/disclaimer text is discarded outright: never trailing
            # metadata for the previous transaction, never a transaction of
            # its own, regardless of what it ends with.
            continue
        bare = _NUBANK_BARE_AMOUNT.match(line)
        inline = None if bare else _NUBANK_INLINE_AMOUNT_TAIL.match(line)
        if not bare and not inline:
            pending_parts.append(line)
            continue
        if pending_is_trailing_metadata:
            # `pending_parts` belongs to the transaction already emitted,
            # not to this new one (Work Order requirement 7: multiple
            # transactions in the same section stay separate even when a
            # metadata continuation line sits between two inline amounts).
            # Flush it there first so this line starts a clean description.
            attach_trailing_metadata()
        if bare:
            description = _strip_trailing_separator(" ".join(pending_parts))
            raw_amount = line
        else:
            tail, raw_amount = inline.groups()
            description = _strip_trailing_separator(" ".join([*pending_parts, tail]))
        pending_parts = []
        if not description or _NON_TRANSACTION_TEXT.search(normalize_description(description)):
            pending_is_trailing_metadata = False
            continue
        if current_sign is None:
            pending_is_trailing_metadata = False
            continue
        value = abs(parse_decimal(raw_amount))
        amount = value if current_sign > 0 else -value
        parsed.append(ParsedTransaction(current_day, description, amount, line_number))
        pending_is_trailing_metadata = bool(inline)
    attach_trailing_metadata()
    if not parsed:
        raise ValueError("Extrato Nubank sem lançamentos textuais reconhecíveis; encaminhado para revisão")
    return parsed


_MERCADO_PAGO_CARD_START = re.compile(r"^(\d{2}/\d{2})\s+(.*)$")
_MERCADO_PAGO_CARD_TAIL = re.compile(r"(.*?)\s*(-?)\s*R\$\s*(-?[\d.]+,\d{2})\s*$")
_MERCADO_PAGO_CARD_MARKER = re.compile(r"CART[ÃA]O\s+\S+\s*\[?\**(\d{4})\]?", re.IGNORECASE)


def _parse_mercado_pago_credit_card_pdf(text: str) -> list[ParsedTransaction]:
    reference = _reference_date(text)
    parsed: list[ParsedTransaction] = []
    pending_date: date | None = None
    pending_parts: list[str] = []
    card_last_four: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        card_match = _MERCADO_PAGO_CARD_MARKER.search(line)
        if card_match:
            card_last_four = card_match.group(1)
        start = _MERCADO_PAGO_CARD_START.match(line)
        if start:
            pending_date = _card_date(start.group(1), reference)
            pending_parts = [start.group(2).strip()]
        elif pending_date is not None:
            pending_parts.append(line)
        else:
            continue
        joined = " ".join(part for part in pending_parts if part)
        money = _MERCADO_PAGO_CARD_TAIL.match(joined)
        if not money:
            continue
        description = money.group(1).strip()
        if description and not _NON_TRANSACTION_TEXT.search(normalize_description(description)):
            raw_amount = _combine_money_sign(money.group(2), money.group(3))
            amount = _normalize_credit_card_amount(description, parse_decimal(raw_amount))
            current, total = _installment(description)
            parsed.append(
                ParsedTransaction(
                    reference.replace(day=1),
                    description,
                    amount,
                    line_number,
                    card_last_four,
                    current,
                    total,
                    pending_date,
                )
            )
        pending_date = None
        pending_parts = []
    if not parsed:
        raise ValueError("Fatura Mercado Pago sem compras textuais reconhecíveis; encaminhada para revisão")
    return parsed


_MERCADO_PAGO_STATEMENT_START = re.compile(r"^(\d{2}-\d{2}-\d{4})\s+(.*)$")
_MERCADO_PAGO_STATEMENT_TAIL = re.compile(
    r"(.*?)\s+\d+\s+(-?)\s*R\$\s*(-?[\d.]+,\d{2})\s+R\$\s*-?[\d.]+,\d{2}\s*$"
)


def _parse_mercado_pago_bank_statement_pdf(text: str) -> list[ParsedTransaction]:
    parsed: list[ParsedTransaction] = []
    pending_date: date | None = None
    pending_parts: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        start = _MERCADO_PAGO_STATEMENT_START.match(line)
        if start:
            pending_date = parse_date(start.group(1))
            pending_parts = [start.group(2).strip()]
        elif pending_date is not None:
            pending_parts.append(line)
        else:
            continue
        joined = " ".join(part for part in pending_parts if part)
        # "Data Descrição ID da operação Valor Saldo": the running balance
        # (last "R$ value") is captured by the pattern to delimit the line
        # correctly but intentionally discarded -- `declared_fields` already
        # carries the document's own opening/closing balance, and inventing
        # a second, per-row balance source here would be exactly the kind of
        # parallel financial fact the Work Order prohibits.
        money = _MERCADO_PAGO_STATEMENT_TAIL.match(joined)
        if not money:
            continue
        description = money.group(1).strip()
        if description:
            raw_amount = _combine_money_sign(money.group(2), money.group(3))
            parsed.append(
                ParsedTransaction(pending_date, description, parse_decimal(raw_amount), line_number)
            )
        pending_date = None
        pending_parts = []
    if not parsed:
        raise ValueError(
            "Extrato Mercado Pago sem lançamentos textuais reconhecíveis; encaminhado para revisão"
        )
    return parsed


def parse_credit_card_pdf(payload: bytes) -> list[ParsedTransaction]:
    text = _pdf_text(payload)
    issuer = _detect_pdf_issuer(text)
    if issuer == "nubank":
        return _parse_nubank_credit_card_pdf(text)
    if issuer == "mercado_pago":
        return _parse_mercado_pago_credit_card_pdf(text)
    if issuer == "unknown":
        raise ValueError(_UNSUPPORTED_PDF_ISSUER_ERROR)
    # Itaú keeps its own `pdfplumber` pass: its two-column crop needs page
    # geometry, not just plain text.
    return _parse_itau_credit_card_pdf(payload)


def parse_bank_statement_pdf(payload: bytes) -> list[ParsedTransaction]:
    text = _pdf_text(payload)
    issuer = _detect_pdf_issuer(text)
    if issuer == "nubank":
        return _parse_nubank_bank_statement_pdf(text)
    if issuer == "mercado_pago":
        return _parse_mercado_pago_bank_statement_pdf(text)
    if issuer == "unknown":
        raise ValueError(_UNSUPPORTED_PDF_ISSUER_ERROR)
    return _parse_itau_bank_statement_pdf(text)


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


def _pdf_declared_fields(payload: bytes, document_type: str) -> tuple[str, dict[str, Decimal | str | None]]:
    """Resolve `(parser_name, declared_fields)` for a PDF from the same
    content-based issuer detection `parse_credit_card_pdf`/
    `parse_bank_statement_pdf` use, so a document's provenance label and its
    reconciliation inputs always describe the same parser. Itaú's own
    `parser_name` values ("itau_credit_card_pdf", "bank_statement_pdf") are
    unchanged from before this Work Order to keep every already-passing
    Itaú result identical.
    """

    text = _pdf_text(payload)
    issuer = _detect_pdf_issuer(text)
    if issuer == "unknown":
        raise ValueError(_UNSUPPORTED_PDF_ISSUER_ERROR)
    if document_type == "credit_card":
        if issuer == "nubank":
            return "nubank_credit_card_pdf", _nubank_credit_card_declared_fields(text)
        if issuer == "mercado_pago":
            return "mercado_pago_credit_card_pdf", _mercado_pago_credit_card_declared_fields(text)
        return "itau_credit_card_pdf", _credit_card_declared_fields(text)
    if issuer == "nubank":
        return "nubank_bank_statement_pdf", _nubank_statement_declared_fields(text)
    if issuer == "mercado_pago":
        return "mercado_pago_bank_statement_pdf", _mercado_pago_statement_declared_fields(text)
    return "bank_statement_pdf", _statement_declared_fields(text)


def parse_document_contract(filename: str, payload: bytes, document_type: str) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()
    if suffix in {".csv", ".txt"}:
        rows = tuple(parse_csv(payload, document_type))
        parser_name = "tabular_csv"
        declared_fields: dict[str, Decimal | str | None] = {}
    elif suffix in {".ofx", ".qfx"}:
        rows = tuple(parse_ofx(payload))
        parser_name = "ofx_statement"
        declared_fields = _ofx_declared_fields(payload)
    elif suffix == ".pdf":
        if document_type not in {"credit_card", "bank_statement"}:
            raise ValueError("Tipo de PDF não suportado para lançamentos")
        rows = tuple(parse_pdf(payload, document_type))
        parser_name, declared_fields = _pdf_declared_fields(payload, document_type)
    else:
        raise ValueError("Formato não suportado. Use PDF textual, CSV ou OFX")

    calculated = _transaction_components(rows, document_type)
    formula = (
        "card_previous_plus_purchases_fees_minus_credits_payments"
        if document_type == "credit_card"
        else "statement_opening_plus_credits_minus_debits"
    )
    required = (
        ("declared_total", "opening_balance")
        if document_type == "credit_card"
        else ("opening_balance", "closing_balance")
    )
    period = _single_period(rows)
    return ParsedDocument(
        document_type=document_type,
        parser_name=parser_name,
        parser_version=PARSER_CONTRACT_VERSION,
        transactions=rows,
        declared_fields=declared_fields,
        calculated_fields=calculated,
        reconciliation_formula=formula,
        coverage={
            "transactions": bool(rows),
            **{field_name: declared_fields.get(field_name) is not None for field_name in required},
        },
        warnings=tuple(
            f"missing_{field_name}" for field_name in required if declared_fields.get(field_name) is None
        ),
        period=period,
    )


def parse_document(filename: str, payload: bytes, document_type: str) -> list[ParsedTransaction]:
    """Compatibility adapter for consumers not yet migrated to ParsedDocument."""

    return list(parse_document_contract(filename, payload, document_type).transactions)


def parse_payroll_document(payload: bytes) -> ParsedDocument:
    payroll = parse_payroll_pdf(payload)
    calculated_net = parse_decimal(payroll.gross_amount - payroll.deductions)
    return ParsedDocument(
        document_type="payroll",
        parser_name="payroll_pdf",
        parser_version=PARSER_CONTRACT_VERSION,
        payroll=payroll,
        declared_fields={
            "gross_amount": payroll.gross_amount,
            "deductions": payroll.deductions,
            "net_amount": payroll.net_amount,
            "payroll_loan": payroll.payroll_loan,
            "payment_date": payroll.payment_date.isoformat(),
        },
        calculated_fields={"calculated_net": calculated_net},
        reconciliation_formula="payroll_gross_minus_deductions",
        coverage={
            "competence": True,
            "payment_date": True,
            "gross_amount": True,
            "deductions": True,
            "net_amount": True,
            "payroll_loan": True,
        },
        period=payroll.competence.strftime("%Y-%m"),
    )


def _single_period(rows: tuple[ParsedTransaction, ...]) -> str | None:
    periods = {row.booked_at.strftime("%Y-%m") for row in rows}
    return next(iter(periods)) if len(periods) == 1 else None


def _transaction_components(
    rows: tuple[ParsedTransaction, ...], document_type: str
) -> dict[str, Decimal]:
    credits = Decimal("0")
    debits = Decimal("0")
    purchases = Decimal("0")
    fees = Decimal("0")
    refunds = Decimal("0")
    payments = Decimal("0")
    for row in rows:
        amount = parse_decimal(row.amount)
        normalized = normalize_description(row.description)
        if amount > 0:
            credits += amount
        elif amount < 0:
            debits += abs(amount)
        if document_type != "credit_card":
            continue
        if PAYMENT_PATTERN.search(normalized):
            payments += abs(amount)
        elif REFUND_PATTERN.search(normalized):
            refunds += abs(amount)
        elif FEE_PATTERN.search(normalized):
            fees += abs(amount)
        elif amount > 0:
            # A credit whose description matches none of the patterns above
            # still reduces what is owed exactly like a named payment does in
            # `card_previous_plus_purchases_fees_minus_credits_payments`
            # (`purchases + fees - refunds - payments`, both subtracted).
            # Bucketing it here keeps `_reconcile_card` truthful about every
            # credit on the statement -- the previous behavior silently
            # dropped it from every total, which could make a correctly
            # reconciled invoice look `not_reconciled` for no real reason.
            payments += amount
        elif amount < 0:
            purchases += abs(amount)
    return {
        "credits_total": parse_decimal(credits),
        "debits_total": parse_decimal(debits),
        "purchases_total": parse_decimal(purchases),
        "fees_total": parse_decimal(fees),
        "refunds_total": parse_decimal(refunds),
        "payments_total": parse_decimal(payments),
    }


def _ofx_declared_fields(payload: bytes) -> dict[str, Decimal | str | None]:
    text = payload.decode("latin-1", errors="replace")
    balance = _ofx_tag(text, "BALAMT")
    as_of = _ofx_tag(text, "DTASOF")
    return {
        "closing_balance": parse_decimal(balance) if balance is not None else None,
        "as_of_date": parse_date(as_of[:8]).isoformat() if as_of else None,
    }


def _statement_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    openings = re.findall(
        r"SALDO\s+(?:ANTERIOR|INICIAL).*?(-?\s*[\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    closings = re.findall(
        r"SALDO\s+(?:FINAL|DO DIA).*?(-?\s*[\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    as_of_match = re.search(
        r"(?:PER[IÍ]ODO[^\n]*?AT[EÉ]|SALDO\s+(?:FINAL|DO DIA))[^\n]*?(\d{2}/\d{2}/\d{4})",
        text,
        re.IGNORECASE,
    )
    return {
        "opening_balance": parse_decimal(openings[0]) if openings else None,
        "closing_balance": parse_decimal(closings[-1]) if closings else None,
        "as_of_date": parse_date(as_of_match.group(1)).isoformat() if as_of_match else None,
    }


def _credit_card_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    totals = re.findall(
        r"TOTAL\s+(?:DA FATURA|A PAGAR).*?R?\$?\s*([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    previous = re.findall(
        r"SALDO\s+ANTERIOR.*?R?\$?\s*([\d.]+,\d{2})",
        text,
        re.IGNORECASE,
    )
    return {
        "declared_total": parse_decimal(totals[-1]) if totals else None,
        "opening_balance": parse_decimal(previous[-1]) if previous else None,
    }


# Nubank/Mercado Pago declared-field extraction lives in its own function per
# format instead of widening `_credit_card_declared_fields`/
# `_statement_declared_fields` above with more alternatives: those two
# functions are Itaú's contract today, and "Total" alone (Mercado Pago) is
# ambiguous against Itaú's own "Total de encargos"/"Total de compras"
# subtotals -- broadening the shared regex risked quietly changing which
# money value Itaú's *existing*, already-shipped invoices resolve to. This
# still reuses everything downstream unchanged (the same `ParsedDocument`
# shape, the same `reconcile_parsed_document`): only the label text each
# issuer prints differs.
_NUBANK_CURRENT_INVOICE_SUMMARY = re.compile(r"RESUMO\s+DA\s+FATURA\s+ATUAL", re.IGNORECASE)
_NUBANK_SUMMARY_TOTAL = re.compile(
    r"TOTAL\s+A\s+PAGAR[^\n]*?R?\$?\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)
_NUBANK_SUMMARY_PREVIOUS = re.compile(
    r"FATURA\s+ANTERIOR[^\n]*?R?\$?\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)


def _nubank_credit_card_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    """Read reconciliation facts only from Nubank's current-invoice summary.

    Real textual invoices can repeat ``FATURA ANTERIOR`` and ``TOTAL A PAGAR``
    outside ``RESUMO DA FATURA ATUAL`` in payment/financing simulations.
    Those repeated labels are not the current invoice's reconciliation facts.

    The parser therefore requires the explicit current-summary marker, then
    takes the first ``TOTAL A PAGAR`` after it and only considers
    ``FATURA ANTERIOR`` before that total. If that structural evidence is
    absent, the field stays unknown instead of guessing from another block.
    """

    summary_marker = _NUBANK_CURRENT_INVOICE_SUMMARY.search(text)
    if summary_marker is None:
        return {"declared_total": None, "opening_balance": None}

    summary_tail = text[summary_marker.end() :]
    total_match = _NUBANK_SUMMARY_TOTAL.search(summary_tail)
    if total_match is None:
        return {"declared_total": None, "opening_balance": None}

    # The first TOTAL A PAGAR closes the canonical "Resumo da fatura atual".
    # Anything after it can be another offer/simulation carrying the same
    # labels and must not influence the current invoice reconciliation.
    summary_block = summary_tail[: total_match.end()]
    previous_match = _NUBANK_SUMMARY_PREVIOUS.search(summary_block)

    return {
        "declared_total": parse_decimal(total_match.group(1)),
        "opening_balance": (
            parse_decimal(previous_match.group(1)) if previous_match is not None else None
        ),
    }


def _mercado_pago_credit_card_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    # Anchored to a line that is *only* "Total[:] R$ value" (optionally with
    # a colon) so it cannot match "Consumos de ... R$ ...", "Tarifas e
    # encargos ... R$ ..." or any other subtotal that also starts with the
    # word "Total". The Work Order's observed Mercado Pago layout does not
    # include a previous-balance/carry-over line (unlike Itaú/Nubank), so
    # `opening_balance` is honestly left `None` here -- `_reconcile_card`
    # then reports `unknown`, never a fabricated balance.
    totals = re.findall(r"^[ \t]*TOTAL\s*:?\s*R?\$?\s*([\d.]+,\d{2})[ \t]*$", text, re.IGNORECASE | re.MULTILINE)
    return {
        "declared_total": parse_decimal(totals[-1]) if totals else None,
        "opening_balance": None,
    }


_NUBANK_PERIOD_EXTENSO = re.compile(
    r"\d{1,2}\s+DE\s+[A-ZÇ]+\s+DE\s+\d{4}\s+A\s+(\d{1,2})\s+DE\s+([A-ZÇ]+)\s+DE\s+(\d{4})",
    re.IGNORECASE,
)


def _nubank_statement_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    # "Saldo inicial"/"Saldo final do período" already match the generic
    # Itaú-shaped patterns in `_statement_declared_fields` verbatim, so that
    # function is reused unchanged for both. Only the period, spelled out in
    # Portuguese ("01 DE AGOSTO DE 2026 a 31 DE AGOSTO DE 2026") instead of
    # Itaú's `dd/mm/yyyy`, needs issuer-specific extraction for `as_of_date`.
    fields = dict(_statement_declared_fields(text))
    if fields.get("as_of_date") is None:
        match = _NUBANK_PERIOD_EXTENSO.search(text)
        if match:
            day, month_name, year = match.groups()
            month = MONTHS_PT.get(normalize_description(month_name))
            if month:
                fields["as_of_date"] = date(int(year), month, int(day)).isoformat()
    return fields


_MERCADO_PAGO_PERIOD = re.compile(
    r"PER[IÍ]ODO\s*:?\s*DE\s+(\d{2}-\d{2}-\d{4})\s+(?:AL|AT[EÉ])\s+(\d{2}-\d{2}-\d{4})",
    re.IGNORECASE,
)


def _mercado_pago_statement_declared_fields(text: str) -> dict[str, Decimal | str | None]:
    # Same reasoning as the Nubank statement above: "Saldo inicial"/"Saldo
    # final" already match `_statement_declared_fields` unchanged; only the
    # period ("Periodo: De dd-mm-yyyy al dd-mm-yyyy") needs its own pattern.
    fields = dict(_statement_declared_fields(text))
    if fields.get("as_of_date") is None:
        match = _MERCADO_PAGO_PERIOD.search(text)
        if match:
            fields["as_of_date"] = parse_date(match.group(2)).isoformat()
    return fields


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
