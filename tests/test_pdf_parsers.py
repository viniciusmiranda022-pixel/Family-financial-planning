"""Nubank/Mercado Pago PDF parser compatibility.

`docs/WORK_ORDER_PDF_PARSERS_NUBANK_MERCADO_PAGO.md` -- content-based issuer
detection, the canonical `parse_document_contract` -> `reconcile_parsed_document`
pipeline, and INV-001/002/016/017. `tests/fixtures/pdf_documents.py` renders
every fixture at test time from synthetic text; no real document, PII or
financial fact is read from or written into Git.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.classifier import classify
from app.services.importer import (
    _detect_pdf_issuer,
    _installment,
    _pdf_text,
    parse_document_contract,
)
from app.services.reconciliation import reconcile_parsed_document
from tests.fixtures import pdf_documents as fx

# ---------------------------------------------------------------------------
# Issuer detection is content-based, never filename-based.
# ---------------------------------------------------------------------------


def test_issuer_detection_reads_content_not_filename() -> None:
    assert _detect_pdf_issuer(_pdf_text(fx.nubank_credit_card_pdf())) == "nubank"
    assert _detect_pdf_issuer(_pdf_text(fx.mercado_pago_credit_card_pdf())) == "mercado_pago"
    assert _detect_pdf_issuer(_pdf_text(fx.itau_credit_card_pdf())) == "itau"


# ---------------------------------------------------------------------------
# Itaú regression baseline -- this project had no PDF-parser test coverage
# before this Work Order. These pin the pre-existing behavior so the new
# content-based dispatch cannot silently change it.
# ---------------------------------------------------------------------------


def test_itau_credit_card_pdf_unchanged_by_new_dispatch() -> None:
    parsed = parse_document_contract("fatura.pdf", fx.itau_credit_card_pdf(), "credit_card")
    assert parsed.parser_name == "itau_credit_card_pdf"
    by_desc = {t.description: t for t in parsed.transactions}
    assert by_desc["Supermercado Alfa"].amount == Decimal("-250.00")
    assert by_desc["Estorno Compra Alfa"].amount == Decimal("30.00")
    assert by_desc["Loja Beta Parcela 2/6"].amount == Decimal("-100.00")
    assert by_desc["Loja Beta Parcela 2/6"].installment_current == 2
    assert by_desc["Loja Beta Parcela 2/6"].installment_total == 6
    assert by_desc["Pagamento recebido"].amount == Decimal("500.00")
    assert by_desc["Posto Gama"].amount == Decimal("-80.00")
    assert by_desc["Supermercado Alfa"].card_last_four == "1234"
    assert by_desc["Pagamento recebido"].card_last_four == "5678"
    assert parsed.declared_fields["opening_balance"] == Decimal("500.00")
    assert parsed.declared_fields["declared_total"] == Decimal("400.00")

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_itau_bank_statement_pdf_unchanged_by_new_dispatch() -> None:
    parsed = parse_document_contract("extrato.pdf", fx.itau_bank_statement_pdf(), "bank_statement")
    assert parsed.parser_name == "bank_statement_pdf"
    descriptions = [t.description for t in parsed.transactions]
    assert descriptions == ["Deposito Salario", "Pagamento Boleto", "Compra Debito"]
    assert parsed.declared_fields["opening_balance"] == Decimal("1000.00")
    assert parsed.declared_fields["closing_balance"] == Decimal("3750.00")
    # `_statement_declared_fields` (unchanged by this Work Order) takes the
    # *first* "SALDO ... DO DIA/FINAL ... dd/mm/yyyy" match in the document,
    # which here is the excluded running-balance line's own date
    # (07/08/2026), not the later "SALDO FINAL EM 08/08/2026" line.
    assert parsed.declared_fields["as_of_date"] == "2026-08-07"

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


# ---------------------------------------------------------------------------
# Cross-brand counterparty regression (PR #41 engineer review, confirmed):
# `_detect_pdf_issuer` scanning the whole document for a bare brand token
# misrouted a real Itaú statement to the Nubank parser, because the real
# statement behind this Work Order's baseline names Nubank/Mercado Pago as
# *counterparties* ("PAG BOLETO NU PAGAMENTOS SA", "PIX QRS MERCADO PAG...").
# Detection must require a document-identity/layout marker, not just the
# brand token, before selecting a non-Itaú parser.
# ---------------------------------------------------------------------------


def test_itau_bank_statement_with_cross_brand_counterparties_stays_itau() -> None:
    payload = fx.itau_bank_statement_pdf_with_cross_brand_counterparties()
    assert _detect_pdf_issuer(_pdf_text(payload)) == "itau"

    parsed = parse_document_contract("extrato.pdf", payload, "bank_statement")
    assert parsed.parser_name == "bank_statement_pdf"
    descriptions = [t.description for t in parsed.transactions]
    assert descriptions == [
        "PAG BOLETO NU PAGAMENTOS SA",
        "PIX QRS MERCADO PAG SILVA",
        "Deposito Salario",
    ]

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_mercado_pago_statement_mentioning_nubank_counterparty_stays_mercado_pago() -> None:
    payload = fx.mercado_pago_bank_statement_pdf_mentioning_nubank()
    assert _detect_pdf_issuer(_pdf_text(payload)) == "mercado_pago"

    parsed = parse_document_contract("extrato.pdf", payload, "bank_statement")
    assert parsed.parser_name == "mercado_pago_bank_statement_pdf"
    by_desc = {t.description: t for t in parsed.transactions}
    assert by_desc["Pagamento fatura Nubank"].amount == Decimal("-50.00")

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_nubank_fatura_mentioning_mercado_pago_counterparty_stays_nubank() -> None:
    payload = fx.nubank_credit_card_pdf_mentioning_mercado_pago()
    assert _detect_pdf_issuer(_pdf_text(payload)) == "nubank"

    parsed = parse_document_contract("fatura.pdf", payload, "credit_card")
    assert parsed.parser_name == "nubank_credit_card_pdf"
    by_desc = {t.description: t for t in parsed.transactions}
    assert by_desc["Pagamento Mercado Pago Loja"].amount == Decimal("-80.00")


# ---------------------------------------------------------------------------
# Unsupported issuer (PR #41 engineer review on `c260aed`): falling back to
# Itaú unconditionally whenever Nubank/MP do not match let any textual PDF
# with an Itaú-shaped transaction row be silently accepted as Itaú, even
# with no Itaú document-identity marker at all. Detection must be positive
# for Itaú too, and an unknown/ambiguous PDF must be rejected for review,
# never guessed.
# ---------------------------------------------------------------------------


def test_unsupported_issuer_bank_statement_is_not_guessed_as_itau() -> None:
    payload = fx.unsupported_issuer_bank_statement_pdf()
    assert _detect_pdf_issuer(_pdf_text(payload)) == "unknown"

    with pytest.raises(ValueError, match="não identificado"):
        parse_document_contract("extrato.pdf", payload, "bank_statement")


def test_unsupported_issuer_credit_card_is_not_guessed_as_itau() -> None:
    payload = fx.unsupported_issuer_credit_card_pdf()
    assert _detect_pdf_issuer(_pdf_text(payload)) == "unknown"

    with pytest.raises(ValueError, match="não identificado"):
        parse_document_contract("fatura.pdf", payload, "credit_card")


# ---------------------------------------------------------------------------
# Nubank -- fatura (credit card)
# ---------------------------------------------------------------------------


def test_nubank_credit_card_pdf_recognizes_purchases_payment_and_refund() -> None:
    parsed = parse_document_contract("fatura.pdf", fx.nubank_credit_card_pdf(), "credit_card")
    assert parsed.parser_name == "nubank_credit_card_pdf"
    by_desc = {t.description: t for t in parsed.transactions}

    purchase = by_desc["Estabelecimento Um"]
    assert purchase.amount == Decimal("-100.00")
    assert purchase.booked_at == date(2026, 8, 1)  # invoice competence (INV-017)
    assert purchase.occurred_at == date(2026, 6, 24)  # original transaction date preserved

    installment = by_desc["Compra Especial Parcela 2/12"]
    assert installment.amount == Decimal("-50.00")
    assert installment.installment_current == 2
    assert installment.installment_total == 12
    assert installment.occurred_at == date(2026, 6, 24)

    refund = by_desc["Estorno Compra"]
    assert refund.amount == Decimal("20.00")
    refund_classification = classify(refund.description, float(refund.amount))
    assert refund_classification.transaction_type == "refund"
    assert refund_classification.excluded is False  # INV-016: credit, not income

    payment = by_desc["Pagamento em 01 JUL"]
    assert payment.amount == Decimal("200.00")
    assert payment.occurred_at == date(2026, 7, 1)
    # INV-002: a card-bill payment must be reconciliation, not operating
    # income, even though the parsed amount is a positive credit.
    payment_classification = classify(payment.description, float(payment.amount))
    assert payment_classification.transaction_type == "reconciliation"
    assert payment_classification.excluded is True


def test_nubank_credit_card_pdf_declared_fields_and_reconciliation() -> None:
    parsed = parse_document_contract("fatura.pdf", fx.nubank_credit_card_pdf(), "credit_card")
    assert parsed.declared_fields["opening_balance"] == Decimal("200.00")
    assert parsed.declared_fields["declared_total"] == Decimal("130.00")
    assert parsed.calculated_fields["purchases_total"] == Decimal("150.00")
    assert parsed.calculated_fields["refunds_total"] == Decimal("20.00")
    assert parsed.calculated_fields["payments_total"] == Decimal("200.00")

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_nubank_credit_card_pdf_excludes_limit_and_simulation_lines() -> None:
    parsed = parse_document_contract(
        "fatura.pdf", fx.nubank_credit_card_pdf_with_disclaimer(), "credit_card"
    )
    descriptions = [t.description for t in parsed.transactions]
    assert descriptions == ["Estabelecimento Um"]


def test_nubank_credit_card_pdf_without_transactions_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="bloco de transações"):
        parse_document_contract(
            "fatura.pdf", fx.nubank_credit_card_pdf_without_transactions_block(), "credit_card"
        )


# ---------------------------------------------------------------------------
# Nubank -- extrato de conta (bank statement)
# ---------------------------------------------------------------------------


def test_nubank_bank_statement_pdf_recognizes_multiline_entries_and_signs() -> None:
    # Fixture shape corrected on PR #41 (engineer review, head `3e25bfb`) to
    # match the real extracted layout: day header shares its line with the
    # first section aggregate, a bare "Total de ..." line switches section
    # within the same day, and each transaction is a multiline description
    # terminated by a bare (no "R$", no sign) amount line.
    parsed = parse_document_contract("extrato.pdf", fx.nubank_bank_statement_pdf(), "bank_statement")
    assert parsed.parser_name == "nubank_bank_statement_pdf"
    by_desc = {t.description: t for t in parsed.transactions}

    entrada = by_desc["Transferencia recebida pelo Pix Fulano de Tal"]
    assert entrada.amount == Decimal("300.00")
    assert entrada.booked_at == date(2026, 8, 14)

    saida = by_desc["Compra no debito Padaria Modelo"]
    assert saida.amount == Decimal("-20.00")
    assert saida.booked_at == date(2026, 8, 14)

    second_day_entrada = by_desc["Recebimento Pix Ciclano"]
    assert second_day_entrada.amount == Decimal("50.00")
    assert second_day_entrada.booked_at == date(2026, 8, 20)

    # Section switch (entradas -> saídas) within the same day, on a
    # genuinely multiline description, must not leak the day's opening sign
    # or the previous section's date into this record.
    second_day_saida = by_desc["Transferencia enviada pelo Pix Beltrano de Souza"]
    assert second_day_saida.amount == Decimal("-100.00")
    assert second_day_saida.booked_at == date(2026, 8, 20)

    # Per-day/period aggregate lines are never transactions.
    assert not any("TOTAL DE" in normalize for normalize in (d.upper() for d in by_desc))


def test_nubank_bank_statement_pdf_declared_fields_and_reconciliation() -> None:
    parsed = parse_document_contract("extrato.pdf", fx.nubank_bank_statement_pdf(), "bank_statement")
    assert parsed.declared_fields["opening_balance"] == Decimal("1000.00")
    assert parsed.declared_fields["closing_balance"] == Decimal("1230.00")
    assert parsed.declared_fields["as_of_date"] == "2026-08-31"

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_nubank_bank_statement_zero_movement_is_not_fabricated() -> None:
    # Work Order: a reconciled-looking zero-movement statement must not be
    # turned into a fabricated single transaction just to make it "pass".
    # The existing contract already rejects a PDF with no recognizable
    # transaction line; this fixture documents that Nubank's zero-movement
    # statements keep exactly that conservative behavior.
    with pytest.raises(ValueError, match="sem lançamentos"):
        parse_document_contract(
            "extrato.pdf", fx.nubank_bank_statement_pdf_zero_movement(), "bank_statement"
        )


# ---------------------------------------------------------------------------
# Mercado Pago -- fatura (credit card)
# ---------------------------------------------------------------------------


def test_mercado_pago_credit_card_pdf_recognizes_payment_refund_and_installment() -> None:
    parsed = parse_document_contract("fatura.pdf", fx.mercado_pago_credit_card_pdf(), "credit_card")
    assert parsed.parser_name == "mercado_pago_credit_card_pdf"
    by_desc = {t.description: t for t in parsed.transactions}

    payment = by_desc["Pagamento da fatura"]
    assert payment.amount == Decimal("100.00")  # credit against the balance, INV-002
    payment_classification = classify(payment.description, float(payment.amount))
    assert payment_classification.excluded is True

    refund = by_desc["Devolucao"]
    assert refund.amount == Decimal("20.00")

    installment = by_desc["Compra Loja Um Parcela 4 de 18"]
    assert installment.amount == Decimal("-54.85")
    assert installment.installment_current == 4
    assert installment.installment_total == 18
    assert installment.card_last_four == "1624"

    second_purchase = by_desc["Compra Loja Dois"]
    assert second_purchase.amount == Decimal("-30.00")
    assert second_purchase.card_last_four == "1624"

    assert parsed.declared_fields["declared_total"] == Decimal("55.00")


def test_mercado_pago_credit_card_pdf_without_opening_balance_is_unknown() -> None:
    # Work Order: the observed Mercado Pago fatura layout does not declare a
    # previous-balance/carry-over figure. Rather than invent one,
    # reconciliation must stay honestly `unknown`, never a fabricated pass.
    parsed = parse_document_contract("fatura.pdf", fx.mercado_pago_credit_card_pdf(), "credit_card")
    assert parsed.declared_fields["opening_balance"] is None

    result = reconcile_parsed_document(parsed)
    assert result.status == "unknown"
    assert result.reason == "missing_required_card_components"


# ---------------------------------------------------------------------------
# Mercado Pago -- extrato de conta (bank statement)
# ---------------------------------------------------------------------------


def test_mercado_pago_bank_statement_pdf_recognizes_multiline_rows() -> None:
    parsed = parse_document_contract(
        "extrato.pdf", fx.mercado_pago_bank_statement_pdf(), "bank_statement"
    )
    assert parsed.parser_name == "mercado_pago_bank_statement_pdf"
    by_desc = {t.description: t for t in parsed.transactions}

    refund = by_desc["Reembolso Compra garantida"]
    assert refund.amount == Decimal("12.70")
    assert refund.booked_at == date(2026, 2, 5)

    payment = by_desc["Pagamento Loja X"]
    assert payment.amount == Decimal("-50.00")
    assert payment.booked_at == date(2026, 2, 7)


def test_mercado_pago_bank_statement_pdf_declared_fields_and_reconciliation() -> None:
    parsed = parse_document_contract(
        "extrato.pdf", fx.mercado_pago_bank_statement_pdf(), "bank_statement"
    )
    assert parsed.declared_fields["opening_balance"] == Decimal("1000.00")
    assert parsed.declared_fields["closing_balance"] == Decimal("962.70")
    assert parsed.declared_fields["as_of_date"] == "2026-02-28"

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.difference == Decimal("0.00")


def test_mercado_pago_bank_statement_zero_movement_is_not_fabricated() -> None:
    with pytest.raises(ValueError, match="sem lançamentos"):
        parse_document_contract(
            "extrato.pdf", fx.mercado_pago_bank_statement_pdf_zero_movement(), "bank_statement"
        )


# ---------------------------------------------------------------------------
# Shared helpers touched by this Work Order
# ---------------------------------------------------------------------------


def test_installment_recognizes_de_and_slash_forms() -> None:
    assert _installment("Compra Parcela 12/12") == (12, 12)
    assert _installment("Compra Parcela 4 de 18") == (4, 18)
    assert _installment("Compra avulsa") == (None, None)
    # "de" alone (no "PARCELA") must not be misread as an installment.
    assert _installment("Presente de 5 de Outubro") == (None, None)


def test_pdf_text_normalizes_unicode_minus_sign() -> None:
    # U+2212 (minus sign), observed in some real Nubank PDFs; `_pdf_text`
    # normalizes it to ASCII "-" so `parse_decimal` and every line-matching
    # regex behave identically regardless of which character the issuer's
    # font actually encodes. Exercised directly on a decoded string because
    # pymupdf's base14 test font cannot encode U+2212 (see
    # `tests/fixtures/pdf_documents.py`'s module docstring).
    from app.services.importer import _PDF_TEXT_TRANSLATION

    assert "−100,00".translate(_PDF_TEXT_TRANSLATION) == "-100,00"
    assert "R$ 100,00".translate(_PDF_TEXT_TRANSLATION) == "R$ 100,00"


def test_classifier_payment_pattern_recognizes_new_issuer_phrasing() -> None:
    # Regression for the classifier.py <-> importer.py pattern unification:
    # the pre-existing narrower substring check in `_normalize_credit_card_amount`
    # would have missed "Pagamento da fatura" (Mercado Pago) entirely because
    # "PAGAMENTO FATURA" is not a contiguous substring of it.
    assert classify("Pagamento da fatura", -100.0).excluded is True
    assert classify("Pagamento recebido", -100.0).excluded is True  # unchanged existing case
    assert classify("Devolucao", 20.0).transaction_type == "refund"
    assert classify("Estorno Compra", 20.0).transaction_type == "refund"  # unchanged existing case
    assert classify("Tarifas e encargos", -5.0).category == "Juros, IOF e tarifas"


def test_classifier_recognizes_nubank_invoice_payment_phrasing() -> None:
    # Regression for PR #41 engineer review (P0 on `ef1f5a2`): the parser
    # already emitted "Pagamento em 01 JUL" as a positive credit and
    # `_transaction_components` already bucketed it into `payments_total`
    # for reconciliation via its generic unmatched-positive-credit fallback,
    # but `classify()` -- the canonical path that decides the *published*
    # transaction_type/excluded for the ledger, dashboard and reports --
    # matched none of the payment alternatives and fell through to plain
    # positive `income`. That silently violated INV-002 (a card-bill payment
    # must have zero operating income/expense effect) even though the
    # invoice still reconciled to R$ 0.01. `PAYMENT_PATTERN` now recognizes
    # this exact Nubank phrasing (day + month abbreviation), so the shared
    # policy classifies it correctly instead of only reconciling correctly.
    result = classify("Pagamento em 01 JUL", 200.0)
    assert result.transaction_type == "reconciliation"
    assert result.excluded is True
    # A merchant name that merely starts with "Pagamento" but carries no
    # day + month-abbreviation identity marker must not be swept in by this
    # addition -- e.g. no bare "Pagamento em" prefix, and no full month name
    # (only the real Nubank three-letter abbreviation, "JUL"/"AGO"/etc., not
    # "Setembro") is accepted as the marker.
    assert classify("Pagamento Estabelecimento Um", 50.0).transaction_type != "reconciliation"
    assert classify("Pagamento em 01 Setembro", 50.0).transaction_type != "reconciliation"
