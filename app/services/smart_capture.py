import io
import re
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import get_settings
from app.services.classifier import Classification, classify, normalize_description
from app.services.importer import ParsedTransaction, parse_date, parse_decimal, parse_document

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SUPPORTED_DOCUMENT_TYPES = {
    "auto",
    "receipt",
    "boleto",
    "bank_statement",
    "credit_card",
    "payroll",
}

MOVEMENT_LABELS = {
    "expense": "Despesa",
    "income": "Receita",
    "investment": "Aplicação/investimento",
    "redemption": "Resgate de investimento",
    "refund": "Reembolso/estorno",
    "transfer": "Transferência interna",
    "reconciliation": "Pagamento/conciliação",
}

MONEY_PATTERN = re.compile(
    r"(?<!\d)(?P<prefix>R\$\s*)?"
    r"(?P<number>\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s*(?P<suffix>MIL|K|REAIS?)?(?!\d)",
    re.IGNORECASE,
)
FULL_DATE_PATTERN = re.compile(r"\b(\d{2}/\d{2}/(?:\d{2}|\d{4}))\b")
SHORT_DATE_PATTERN = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")


class CaptureParseError(ValueError):
    pass


def _is_shorthand_amount(text: str, start: int, end: int) -> bool:
    before = text[:start].strip(" -:;")
    after = text[end:].strip(" -:;")
    if bool(before) == bool(after):
        return False
    description = after or before
    words = normalize_description(description).split()
    if not words or len(normalize_description(text).split()) > 8:
        return False
    timing_words = {"DIA", "HOJE", "ONTEM", "ANTEONTEM"}
    return any(word not in timing_words and not word.isdigit() for word in words)


def _money_candidates(
    text: str, *, allow_shorthand: bool = False
) -> list[tuple[Decimal, int, int, str]]:
    results: list[tuple[Decimal, int, int, str]] = []
    for match in MONEY_PATTERN.finditer(text):
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        before = text[max(0, match.start() - 6) : match.start()].lower()
        after = text[match.end() : match.end() + 2]
        if "/" in before[-2:] or "/" in after or re.search(r"\bdia\s*$", before):
            continue
        if not prefix and not suffix:
            context = normalize_description(text[max(0, match.start() - 45) : match.end() + 30])
            has_financial_context = any(
                word in context
                for word in (
                    "GASTEI",
                    "PAGUEI",
                    "COMPREI",
                    "COMPRAR",
                    "COMPRA",
                    "QUERO",
                    "ABASTECI",
                    "RECEBI",
                    "INVESTI",
                    "APLIQUEI",
                    "RESGATEI",
                    "VALOR",
                    "TOTAL",
                    "PRECO",
                    "CUSTOU",
                    "CUSTA",
                )
            )
            if not has_financial_context and not (
                allow_shorthand and _is_shorthand_amount(text, match.start(), match.end())
            ):
                continue
        try:
            value = parse_decimal(match.group("number"))
        except ValueError:
            continue
        if suffix and suffix.upper() in {"MIL", "K"}:
            value *= Decimal("1000")
        if value <= 0:
            continue
        results.append((value, match.start(), match.end(), match.group(0)))
    return results


def _amount_near_labels(text: str, labels: tuple[str, ...]) -> Decimal | None:
    normalized_lines = [(line, normalize_description(line)) for line in text.splitlines() if line.strip()]
    for label in labels:
        for index, (line, normalized) in enumerate(normalized_lines):
            if label not in normalized:
                continue
            candidates = _money_candidates(line)
            if candidates:
                return candidates[-1][0]
            if index + 1 < len(normalized_lines):
                candidates = _money_candidates(normalized_lines[index + 1][0])
                if candidates:
                    return candidates[0][0]
    candidates = _money_candidates(text)
    return candidates[-1][0] if candidates else None


def _date_from_text(text: str, reference: date) -> date:
    normalized = normalize_description(text)
    if "ANTEONTEM" in normalized:
        return reference - timedelta(days=2)
    if "ONTEM" in normalized:
        return reference - timedelta(days=1)
    match = FULL_DATE_PATTERN.search(text)
    if match:
        return parse_date(match.group(1))
    match = SHORT_DATE_PATTERN.search(text)
    if match:
        day, month = (int(value) for value in match.groups())
        year = reference.year - 1 if month > reference.month + 1 else reference.year
        try:
            return date(year, month, day)
        except ValueError:
            pass
    match = re.search(r"\bDIA\s+(\d{1,2})\b", normalized)
    if match:
        try:
            return reference.replace(day=int(match.group(1)))
        except ValueError:
            pass
    return reference


def _date_near_label(text: str, label: str, reference: date) -> date:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if label not in normalize_description(line):
            continue
        joined = " ".join(lines[index : min(len(lines), index + 2)])
        match = FULL_DATE_PATTERN.search(joined)
        if match:
            return parse_date(match.group(1))
        match = SHORT_DATE_PATTERN.search(joined)
        if match:
            return _date_from_text(match.group(0), reference)
    return _date_from_text(text, reference)


def _classify(
    description: str,
    amount: float,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> Classification:
    """Route classification through the one canonical, household-aware path.

    Smart capture must never run a second classification policy alongside
    the importer's (docs/WORK_ORDER_EDITABLE_MERCHANT_RULES.md: "no second
    classification policy in importer, API, UI or Advisor"). When a database
    session and household are available -- the live API path, via
    `preview_capture` -- this defers to `classify_with_local_rules`, so an
    active household merchant rule applies here exactly as it does on
    import. Without them (pure-function unit tests, or any caller with no
    household context yet) it falls back to the plain deterministic
    classifier, which is exactly what `classify_with_local_rules` itself
    falls back to when no rule matches -- so this is a degenerate case of
    the same policy, never a separate one.
    """
    if db is not None and household_id is not None:
        from app.services.classification_learning import classify_with_local_rules

        applied = classify_with_local_rules(
            db, household_id=household_id, description=description, amount=amount
        )
        return Classification(
            applied.category, applied.transaction_type, applied.excluded, applied.confidence, applied.review_reason
        )
    return classify(description, amount)


def _movement_type(text: str) -> str:
    normalized = normalize_description(text)
    if re.search(r"\b(INVESTI|INVESTIMENTO|APLIQUEI|APLICACAO|APLICAR)\b", normalized):
        return "investment"
    if re.search(r"\b(RESGATEI|RESGATE|RETIREI DO INVESTIMENTO)\b", normalized):
        return "redemption"
    if re.search(r"\b(ESTORNO|REEMBOLSO|DEVOLUCAO)\b", normalized):
        return "refund"
    if re.search(r"\b(RECEBI|ENTROU|SALARIO|COMISSAO|RENDA)\b", normalized):
        return "income"
    return "expense"


def _category_for_text(
    text: str,
    amount: Decimal,
    movement_type: str,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> tuple[str, float, list[str]]:
    if movement_type in {"investment", "redemption"}:
        return "Transferência patrimonial", 0.99, []
    if movement_type == "income":
        return "Receitas", 0.94, []
    if movement_type == "refund":
        return "Reembolsos e estornos", 0.94, []
    classification = _classify(text, float(-abs(amount)), db=db, household_id=household_id)
    warnings = [classification.review_reason] if classification.review_reason else []
    return classification.category, classification.confidence, warnings


def parse_text_capture(
    text: str,
    reference: date | None = None,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> dict:
    reference = reference or date.today()
    cleaned = " ".join(text.split())
    candidates = _money_candidates(cleaned, allow_shorthand=True)
    if not candidates:
        raise CaptureParseError("Não encontrei um valor. Exemplo: ‘Gastei R$ 150 com combustível’. ")
    movement_type = _movement_type(cleaned)
    amount = candidates[0][0]
    category, confidence, warnings = _category_for_text(
        cleaned, amount, movement_type, db=db, household_id=household_id
    )
    if len(candidates) > 1:
        confidence = min(confidence, 0.62)
        warnings.append("Encontrei mais de um valor; confirme o valor correto antes de gravar")
    return {
        "kind": "transaction",
        "booked_at": _date_from_text(cleaned, reference).isoformat(),
        "description": cleaned[:500],
        "amount": float(amount),
        "movement_type": movement_type,
        "movement_label": MOVEMENT_LABELS[movement_type],
        "category_name": category,
        "confidence": round(confidence, 4),
        "warnings": warnings,
        "selected": True,
    }


def _first_meaningful_line(text: str) -> str:
    ignored = {"COMPROVANTE", "RECIBO", "NOTA FISCAL", "CUPOM FISCAL", "BOLETO"}
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if len(cleaned) < 3 or normalize_description(cleaned) in ignored:
            continue
        if MONEY_PATTERN.fullmatch(cleaned):
            continue
        return cleaned[:180]
    return "Documento capturado"


def parse_receipt_text(
    text: str,
    reference: date | None = None,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> dict:
    reference = reference or date.today()
    amount = _amount_near_labels(
        text,
        ("TOTAL A PAGAR", "VALOR TOTAL", "TOTAL", "VALOR DA COMPRA", "VALOR"),
    )
    if amount is None:
        raise CaptureParseError("Não encontrei o valor total no comprovante. Digite o valor na prévia.")
    description = _first_meaningful_line(text)
    classification_text = f"{description} {text[:2000]}"
    category, confidence, warnings = _category_for_text(
        classification_text, amount, "expense", db=db, household_id=household_id
    )
    return {
        "kind": "transaction",
        "booked_at": _date_from_text(text, reference).isoformat(),
        "description": description,
        "amount": float(amount),
        "movement_type": "expense",
        "movement_label": MOVEMENT_LABELS["expense"],
        "category_name": category,
        "confidence": round(min(confidence, 0.86), 4),
        "warnings": warnings,
        "selected": True,
    }


def parse_boleto_text(text: str, reference: date | None = None) -> dict:
    reference = reference or date.today()
    amount = _amount_near_labels(
        text,
        ("VALOR DO DOCUMENTO", "VALOR COBRADO", "VALOR A PAGAR", "TOTAL A PAGAR", "VALOR"),
    )
    if amount is None:
        raise CaptureParseError("Não encontrei o valor do boleto. Confirme se a imagem está legível.")
    due_date = _date_near_label(text, "VENCIMENTO", reference)
    beneficiary = _first_meaningful_line(text)
    return {
        "kind": "obligation",
        "due_date": due_date.isoformat(),
        "description": f"Boleto - {beneficiary}"[:500],
        "amount": float(amount),
        "category_name": "general",
        "recurrence_months": 0,
        "occurrence_count": 1,
        "confidence": 0.82,
        "warnings": ["O boleto será cadastrado como obrigação; ele só vira gasto quando for pago"],
        "selected": True,
    }


def _reference_month(text: str, reference: date) -> date:
    match = re.search(
        r"(?:COMPET[ÊE]NCIA|M[ÊE]S)\s*[:\-]?\s*(\d{1,2})\s*[/\-]\s*(\d{4})",
        text,
        re.IGNORECASE,
    )
    if match:
        return date(int(match.group(2)), int(match.group(1)), 1)
    booked = _date_from_text(text, reference)
    return booked.replace(day=1)


def parse_payroll_text(text: str, reference: date | None = None) -> dict:
    reference = reference or date.today()
    amount = _amount_near_labels(
        text,
        ("LIQUIDO A RECEBER", "VALOR LIQUIDO", "LIQUIDO", "TOTAL LIQUIDO"),
    )
    if amount is None:
        raise CaptureParseError("Não encontrei o valor líquido do holerite.")
    payment_date = _date_near_label(text, "CREDITO", reference)
    person = _first_meaningful_line(text)
    return {
        "kind": "payroll",
        "competence": _reference_month(text, reference).isoformat(),
        "payment_date": payment_date.isoformat(),
        "description": person,
        "amount": float(amount),
        "payroll_kind": "regular",
        "confidence": 0.74,
        "warnings": ["Confirme o nome, a competência e o valor líquido do holerite"],
        "selected": True,
    }


def _parsed_transaction_item(
    item: ParsedTransaction,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> dict:
    movement = "income" if item.amount > 0 else "expense"
    classification = _classify(item.description, float(item.amount), db=db, household_id=household_id)
    if classification.transaction_type == "refund":
        movement = "refund"
    elif classification.transaction_type == "reconciliation":
        movement = "reconciliation"
    elif classification.transaction_type == "transfer":
        normalized = normalize_description(item.description)
        if classification.category == "Transferência patrimonial":
            movement = "redemption" if item.amount > 0 and "RESGATE" in normalized else "investment"
        else:
            movement = "transfer"
    category = classification.category
    return {
        "kind": "transaction",
        "booked_at": item.booked_at.isoformat(),
        "description": item.description[:500],
        "amount": float(abs(item.amount)),
        "signed_amount": float(item.amount),
        "movement_type": movement,
        "movement_label": MOVEMENT_LABELS[movement],
        "category_name": category,
        "confidence": round(classification.confidence, 4),
        "warnings": [classification.review_reason] if classification.review_reason else [],
        "selected": True,
        "source_line": item.source_line,
        "installment_current": item.installment_current,
        "installment_total": item.installment_total,
        "card_last_four": item.card_last_four,
    }


def _parse_bank_text(text: str) -> list[ParsedTransaction]:
    pattern = re.compile(r"^\s*(\d{2}/\d{2}/(?:\d{2}|\d{4}))\s+(.+?)\s+(-?\s*[\d.]+,\d{2})\s*$")
    rows: list[ParsedTransaction] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        match = pattern.match(raw_line)
        if not match:
            continue
        raw_date, description, raw_amount = match.groups()
        normalized = normalize_description(description)
        if normalized.startswith(("SALDO DO DIA", "SALDO ANTERIOR")):
            continue
        rows.append(
            ParsedTransaction(
                parse_date(raw_date), description.strip(), parse_decimal(raw_amount), line_number
            )
        )
    return rows


def _parse_card_text(text: str, reference: date) -> list[ParsedTransaction]:
    pattern = re.compile(r"^\s*(\d{2}/\d{2})(?!/)\s+(.+?)\s+(-?\s*[\d.]+,\d{2})\s*$")
    rows: list[ParsedTransaction] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        match = pattern.match(raw_line)
        if not match:
            continue
        raw_date, description, raw_amount = match.groups()
        day, month = (int(value) for value in raw_date.split("/"))
        year = reference.year - 1 if month > reference.month + 1 else reference.year
        amount = -abs(parse_decimal(raw_amount))
        normalized = normalize_description(description)
        if "PAGAMENTO RECEBIDO" in normalized or "PAGAMENTO FATURA" in normalized:
            amount = abs(amount)
        rows.append(
            ParsedTransaction(date(year, month, day), description.strip(), amount, line_number)
        )
    return rows


def parse_financial_document(
    filename: str,
    payload: bytes,
    document_type: str,
    extracted_text: str,
    reference: date | None = None,
    *,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> list[dict]:
    reference = reference or date.today()
    try:
        rows = parse_document(filename, payload, document_type)
    except ValueError:
        rows = (
            _parse_card_text(extracted_text, reference)
            if document_type == "credit_card"
            else _parse_bank_text(extracted_text)
        )
    if not rows:
        raise CaptureParseError("Não encontrei lançamentos estruturados no documento.")
    return [_parsed_transaction_item(item, db=db, household_id=household_id) for item in rows[:1000]]


def detect_document_type(
    filename: str,
    text: str,
    requested_type: str,
    account_type: str | None = None,
) -> str:
    if requested_type not in SUPPORTED_DOCUMENT_TYPES:
        raise CaptureParseError("Tipo de documento não suportado")
    if requested_type != "auto":
        return requested_type
    suffix = Path(filename).suffix.lower()
    if suffix in {".ofx", ".qfx", ".csv"}:
        return "credit_card" if account_type == "credit_card" else "bank_statement"
    normalized = normalize_description(text[:12000])
    if any(token in normalized for token in ("LIQUIDO A RECEBER", "TOTAL GANHOS", "HOLERITE")):
        return "payroll"
    if "VENCIMENTO" in normalized and any(
        token in normalized for token in ("LINHA DIGITAVEL", "CODIGO DE BARRAS", "BENEFICIARIO", "CEDENTE")
    ):
        return "boleto"
    if any(token in normalized for token in ("FATURA", "PAGAMENTO MINIMO", "LIMITE DO CARTAO")):
        return "credit_card"
    if any(token in normalized for token in ("EXTRATO", "SALDO DO DIA", "SALDO ANTERIOR")):
        return "bank_statement"
    return "receipt"


def _ocr_image(payload: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise CaptureParseError("OCR local indisponível nesta instalação") from exc
    try:
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass
        image = Image.open(io.BytesIO(payload))
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width < 1600:
            ratio = 1600 / image.width
            image = image.resize((1600, max(1, int(image.height * ratio))))
        try:
            return pytesseract.image_to_string(image, lang="por+eng").strip()
        except pytesseract.TesseractError:
            return pytesseract.image_to_string(image).strip()
    except CaptureParseError:
        raise
    except Exception as exc:
        raise CaptureParseError("Não consegui ler a imagem enviada") from exc


def _ocr_pdf(payload: bytes) -> str:
    try:
        import pymupdf as fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise CaptureParseError("OCR de PDF indisponível nesta instalação") from exc
    pages: list[str] = []
    try:
        with fitz.open(stream=payload, filetype="pdf") as document:
            for page in list(document)[:20]:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                try:
                    pages.append(pytesseract.image_to_string(image, lang="por+eng"))
                except pytesseract.TesseractError:
                    pages.append(pytesseract.image_to_string(image))
    except Exception as exc:
        raise CaptureParseError("Não consegui aplicar OCR ao PDF") from exc
    return "\n".join(pages).strip()


def extract_document_text(filename: str, payload: bytes, content_type: str) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".heic"}:
        return _ocr_image(payload), "ocr_local"
    if suffix == ".pdf" or content_type == "application/pdf":
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(payload)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
        except Exception:
            text = ""
        if len(text) >= 40:
            return text, "pdf_text"
        return _ocr_pdf(payload), "ocr_local"
    if suffix in {".csv", ".txt", ".ofx", ".qfx"}:
        encoding = "latin-1" if suffix in {".ofx", ".qfx"} else "utf-8-sig"
        return payload.decode(encoding, errors="replace"), "structured_text"
    raise CaptureParseError("Formato não suportado. Use áudio, imagem, PDF, CSV ou OFX.")


@lru_cache
def _whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise CaptureParseError("Transcrição local indisponível nesta instalação") from exc
    settings = get_settings()
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    return WhisperModel(
        settings.whisper_model,
        device="cpu",
        compute_type="int8",
        download_root=str(settings.models_dir),
    )


def transcribe_audio(filename: str, payload: bytes) -> str:
    suffix = Path(filename).suffix.lower() or ".webm"
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(payload)
            temporary_path = temporary.name
        settings = get_settings()
        segments, _info = _whisper_model().transcribe(
            temporary_path,
            language=settings.whisper_language,
            vad_filter=True,
            beam_size=1,
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        if not text:
            raise CaptureParseError("O áudio não contém fala reconhecível")
        return text
    except CaptureParseError:
        raise
    except Exception as exc:
        raise CaptureParseError(
            "Não consegui transcrever o áudio. Confirme se o arquivo possui voz audível."
        ) from exc
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def preview_capture(
    *,
    text: str | None,
    filename: str | None,
    content_type: str | None,
    payload: bytes | None,
    requested_type: str,
    account_type: str | None,
    reference: date | None = None,
    db: "Session | None" = None,
    household_id: str | None = None,
) -> dict:
    reference = reference or date.today()
    filename = filename or ""
    content_type = content_type or "application/octet-stream"
    source_type = "text"
    processor = "local_rules"
    extracted = ""
    if payload:
        suffix = Path(filename).suffix.lower()
        if content_type.startswith("audio/") or suffix in {".webm", ".m4a", ".mp3", ".wav", ".ogg"}:
            source_type = "audio"
            extracted = transcribe_audio(filename, payload)
            processor = "whisper_local"
        else:
            source_type = "image" if content_type.startswith("image/") else "document"
            extracted, processor = extract_document_text(filename, payload, content_type)
    combined = "\n".join(part for part in ((text or "").strip(), extracted.strip()) if part).strip()
    if not combined:
        raise CaptureParseError("Escreva uma mensagem ou envie um arquivo para analisar")

    detected_type = (
        "text"
        if requested_type == "auto" and source_type in {"text", "audio"}
        else detect_document_type(filename, combined, requested_type, account_type)
    )
    if payload and detected_type in {"bank_statement", "credit_card"}:
        items = parse_financial_document(
            filename, payload, detected_type, combined, reference=reference,
            db=db, household_id=household_id,
        )
    elif detected_type == "boleto":
        items = [parse_boleto_text(combined, reference)]
    elif detected_type == "payroll":
        items = [parse_payroll_text(combined, reference)]
    elif detected_type == "receipt":
        items = [parse_receipt_text(combined, reference, db=db, household_id=household_id)]
    else:
        items = [parse_text_capture(combined, reference, db=db, household_id=household_id)]

    confidence = min(float(item.get("confidence", 0)) for item in items) if items else 0
    warnings = [warning for item in items for warning in item.get("warnings", []) if warning]
    return {
        "source_type": source_type,
        "detected_type": detected_type,
        "processor": processor,
        "extracted_text": extracted[:50000],
        "items": items,
        "confidence": round(confidence, 4),
        "warnings": list(dict.fromkeys(warnings)),
    }
