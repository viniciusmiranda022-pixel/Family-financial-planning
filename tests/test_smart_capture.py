from datetime import date

from app.services.smart_capture import (
    detect_document_type,
    parse_boleto_text,
    parse_payroll_text,
    parse_receipt_text,
    parse_text_capture,
    preview_capture,
)


def test_natural_language_expense_is_classified_before_confirmation() -> None:
    item = parse_text_capture(
        "Gastei R$ 150 com combustível ontem no cartão Itaú",
        reference=date(2026, 8, 25),
    )
    assert item["amount"] == 150.0
    assert item["booked_at"] == "2026-08-24"
    assert item["movement_type"] == "expense"
    assert item["category_name"] == "Transporte"
    assert item["confidence"] >= 0.9


def test_natural_language_purchase_accepts_bare_amount_after_object() -> None:
    item = parse_text_capture(
        "Quero comprar uma enxada de 250",
        reference=date(2026, 8, 25),
    )
    assert item["amount"] == 250.0
    assert item["movement_type"] == "expense"


def test_natural_language_investment_is_not_classified_as_spending() -> None:
    item = parse_text_capture(
        "Investi 500 reais no Privilège DI hoje",
        reference=date(2026, 8, 25),
    )
    assert item["amount"] == 500.0
    assert item["movement_type"] == "investment"
    assert item["category_name"] == "Transferência patrimonial"


def test_text_and_audio_default_to_message_capture() -> None:
    result = preview_capture(
        text="Recebi R$ 1.200 de comissão hoje",
        filename=None,
        content_type=None,
        payload=None,
        requested_type="auto",
        account_type="checking",
        reference=date(2026, 8, 25),
    )
    assert result["source_type"] == "text"
    assert result["detected_type"] == "text"
    assert result["items"][0]["movement_type"] == "income"


def test_credit_card_payment_is_reconciliation_not_income() -> None:
    result = preview_capture(
        text=None,
        filename="cartao.csv",
        content_type="text/csv",
        payload=b"date,title,amount\n2026-08-03,Pagamento recebido,-1724.90\n",
        requested_type="credit_card",
        account_type="credit_card",
        reference=date(2026, 8, 25),
    )
    item = result["items"][0]
    assert item["movement_type"] == "reconciliation"
    assert item["signed_amount"] == 1724.9
    assert item["category_name"] == "Conciliação"


def test_receipt_boleto_and_payroll_have_distinct_proposals() -> None:
    receipt = parse_receipt_text(
        "POSTO CENTRAL\nDATA 24/08/2026\nTOTAL A PAGAR R$ 217,35",
        reference=date(2026, 8, 25),
    )
    assert receipt["kind"] == "transaction"
    assert receipt["amount"] == 217.35
    assert receipt["category_name"] == "Transporte"

    boleto_text = (
        "ENERGIA DA RESIDÊNCIA\nVENCIMENTO 30/08/2026\n"
        "VALOR DO DOCUMENTO R$ 350,40\nLINHA DIGITÁVEL 00190.00009"
    )
    assert detect_document_type("conta.pdf", boleto_text, "auto") == "boleto"
    boleto = parse_boleto_text(boleto_text, reference=date(2026, 8, 25))
    assert boleto["kind"] == "obligation"
    assert boleto["due_date"] == "2026-08-30"
    assert boleto["amount"] == 350.4

    payroll = parse_payroll_text(
        "KELLY MIRANDA\nCOMPETÊNCIA 08/2026\nCRÉDITO 05/09/2026\n"
        "LÍQUIDO A RECEBER R$ 4.321,10",
        reference=date(2026, 8, 25),
    )
    assert payroll["kind"] == "payroll"
    assert payroll["competence"] == "2026-08-01"
    assert payroll["payment_date"] == "2026-09-05"
    assert payroll["amount"] == 4321.1
