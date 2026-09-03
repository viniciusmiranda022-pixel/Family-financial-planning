"""Synthetic textual PDF fixtures for `app.services.importer`'s PDF parsers.

Every line of text below is invented for these tests: no real account,
card, document, merchant or amount from any actual statement or invoice.
The layouts (which labels appear, in which order, with which punctuation)
follow `docs/WORK_ORDER_PDF_PARSERS_NUBANK_MERCADO_PAGO.md`'s "Evidência de
layout observada" section, which is itself already a description with no
personal data, account numbers or real amounts. Real financial PDFs are
gitignored (`*.pdf` in `.gitignore`) and must never be committed; these
fixtures render a PDF *at test time* instead, from plain-text templates
kept in Git as ordinary Python strings.

PDFs are rendered with `pymupdf` (already a runtime dependency, used
elsewhere for image conversion) using the built-in "helv" base font. That
font only reliably encodes Latin-1: it renders accented Portuguese/Spanish
characters (á, ã, ç, í, ó...) correctly, but silently mangles characters
outside Latin-1 such as the Unicode minus sign (U+2212) some real Nubank
PDFs use for negative amounts. `app.services.importer._pdf_text` normalizes
that character (and lookalike dashes) from *whatever* pdfplumber extracts,
so these fixtures use a plain ASCII "-" and that normalization path is
covered separately, directly on a literal string, in
`tests/test_importer.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import pymupdf as fitz

_PAGE_WIDTH = 595.0
_PAGE_HEIGHT = 842.0
_FONT_SIZE = 10.0
_LINE_HEIGHT = 14.0
_TOP_MARGIN = 50.0
_BOTTOM_MARGIN = 40.0


def render_single_column_pdf(lines: Sequence[str], *, x: float = 50.0) -> bytes:
    """Render `lines` as a plain single-column textual PDF.

    Matches the shape `pdfplumber.Page.extract_text()` returns for a real
    single-column statement/invoice: one line of text per input string, in
    order, across as many pages as needed.
    """

    document = fitz.open()
    page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    y = _TOP_MARGIN
    for line in lines:
        if line:
            page.insert_text((x, y), line, fontsize=_FONT_SIZE, fontname="helv")
        y += _LINE_HEIGHT
        if y > _PAGE_HEIGHT - _BOTTOM_MARGIN:
            page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
            y = _TOP_MARGIN
    payload = document.tobytes()
    document.close()
    return payload


def render_two_column_pdf(left_lines: Sequence[str], right_lines: Sequence[str]) -> bytes:
    """Render two independent side-by-side text columns on one page.

    Mirrors Itaú's real credit-card PDF layout, which `_parse_itau_credit_card_pdf`
    reads by cropping each page at 60% of its width. `left_lines` is placed
    well inside the left crop and `right_lines` well inside the right crop
    so the two never bleed into each other the way real side-by-side tables
    would not either.
    """

    document = fitz.open()
    page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    right_x = _PAGE_WIDTH * 0.65
    for lines, x in ((left_lines, 50.0), (right_lines, right_x)):
        y = _TOP_MARGIN
        for line in lines:
            if line:
                page.insert_text((x, y), line, fontsize=_FONT_SIZE, fontname="helv")
            y += _LINE_HEIGHT
    payload = document.tobytes()
    document.close()
    return payload


# ---------------------------------------------------------------------------
# Itaú -- regression baseline. This project had zero PDF-parser test
# coverage before this Work Order (`tests/test_importer.py` only exercised
# `parse_csv`); these two fixtures pin down the pre-existing Itaú behavior
# so the new issuer-detection dispatch in `parse_credit_card_pdf`/
# `parse_bank_statement_pdf` cannot silently change it.
# ---------------------------------------------------------------------------

ITAU_CREDIT_CARD_LEFT = (
    "Itau Unibanco S.A.",
    "EMISSAO: 10/08/2026",
    "SALDO ANTERIOR R$ 500,00",
    "TOTAL A PAGAR R$ 400,00",
    "cartao final 1234",
    "05/08 Supermercado Alfa 250,00",
    "06/08 Estorno Compra Alfa -30,00",
    "07/08 Loja Beta Parcela 2/6 100,00",
)

ITAU_CREDIT_CARD_RIGHT = (
    "cartao final 5678",
    "08/08 Pagamento recebido -500,00",
    "09/08 Posto Gama 80,00",
)


def itau_credit_card_pdf() -> bytes:
    return render_two_column_pdf(ITAU_CREDIT_CARD_LEFT, ITAU_CREDIT_CARD_RIGHT)


ITAU_BANK_STATEMENT_LINES = (
    "Itau Unibanco S.A. - www.itau.com.br",
    "SALDO ANTERIOR 1000,00",
    "05/08/2026 Deposito Salario 3000,00",
    "06/08/2026 Pagamento Boleto -200,00",
    "07/08/2026 SALDO DO DIA 07/08/2026 3800,00",
    "08/08/2026 Compra Debito -50,00",
    "SALDO FINAL EM 08/08/2026 3750,00",
)


def itau_bank_statement_pdf() -> bytes:
    return render_single_column_pdf(ITAU_BANK_STATEMENT_LINES)


# Adversarial regression: real Itaú statement text (see PR #41 engineer
# review) legitimately names other institutions as *counterparties* --
# "PAG BOLETO NU PAGAMENTOS SA" (paying a Nubank-issued boleto) and
# "PIX QRS MERCADO PAG..." (a Pix to/from a Mercado Pago-linked account) --
# without the document being a Nubank or Mercado Pago PDF. `_detect_pdf_issuer`
# must resolve this as Itaú (brand token alone is not enough; it also needs
# one of that issuer's own document-identity/layout markers, absent here).
ITAU_BANK_STATEMENT_WITH_CROSS_BRAND_COUNTERPARTIES_LINES = (
    "Itau Unibanco S.A. - www.itau.com.br",
    "SALDO ANTERIOR 1000,00",
    "05/08/2026 PAG BOLETO NU PAGAMENTOS SA -1.724,90",
    "06/08/2026 PIX QRS MERCADO PAG SILVA -50,00",
    "07/08/2026 Deposito Salario 3000,00",
    "SALDO FINAL EM 07/08/2026 2225,10",
)


def itau_bank_statement_pdf_with_cross_brand_counterparties() -> bytes:
    return render_single_column_pdf(ITAU_BANK_STATEMENT_WITH_CROSS_BRAND_COUNTERPARTIES_LINES)


# ---------------------------------------------------------------------------
# Nubank -- fatura (credit card). Layout per the Work Order's "Nubank —
# fatura PDF" section: header with "FATURA dd MMM yyyy", a
# "RESUMO DA FATURA ATUAL" summary block, and a "TRANSAÇÕES DE ..." block
# whose lines are "dd MMM <descrição> R$ valor", some spanning more than
# one line before the amount.
# ---------------------------------------------------------------------------

NUBANK_CREDIT_CARD_LINES = (
    "Nubank",
    "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
    "RESUMO DA FATURA ATUAL",
    "Fatura anterior R$ 200,00",
    "Pagamento recebido -R$ 200,00",
    "Total de compras R$ 150,00",
    "Total a pagar R$ 130,00",
    "TRANSACOES DE 26 JUN A 25 JUL",
    "24 JUN Estabelecimento Um R$ 100,00",
    "24 JUN Compra",
    "Especial Parcela 2/12 R$ 50,00",
    "02 JUL Estorno Compra R$ 20,00",
    "01 JUL Pagamento em 01 JUL -R$ 200,00",
)


def nubank_credit_card_pdf() -> bytes:
    return render_single_column_pdf(NUBANK_CREDIT_CARD_LINES)


# A second, minimal fixture isolating the "limite/simulação" exclusion:
# a disclaimer line shaped exactly like a transaction (leads with "dd MMM",
# ends in "R$ valor") but must never be counted as one.
NUBANK_CREDIT_CARD_WITH_DISCLAIMER_LINES = (
    "Nubank",
    "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
    "TRANSACOES DE 26 JUN A 25 JUL",
    "24 JUN Estabelecimento Um R$ 100,00",
    "25 JUL Simulacao de pagamento minimo R$ 45,00",
    "25 JUL Limite disponivel para compras R$ 3000,00",
)


def nubank_credit_card_pdf_with_disclaimer() -> bytes:
    return render_single_column_pdf(NUBANK_CREDIT_CARD_WITH_DISCLAIMER_LINES)


NUBANK_CREDIT_CARD_NO_TRANSACTIONS_BLOCK = (
    "Nubank",
    "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
    "RESUMO DA FATURA ATUAL",
    "Total a pagar R$ 0,00",
)


def nubank_credit_card_pdf_without_transactions_block() -> bytes:
    return render_single_column_pdf(NUBANK_CREDIT_CARD_NO_TRANSACTIONS_BLOCK)


# ---------------------------------------------------------------------------
# Nubank -- extrato de conta (bank statement). Layout per the Work Order's
# "Nubank — extrato de conta PDF" section, corrected on PR #41 (engineer
# review, head `3e25bfb`) to match the *real* extracted shape rather than a
# cleaner invented one: a spelled-out period, a summary block, then per-day
# blocks where the day header shares its line with the first section
# aggregate for that day ("dd MMM yyyy Total de entradas/saídas ± valor"),
# a section can switch again within the same day via a bare "Total de
# entradas/saídas ± valor" line (no date repeated), and each individual
# transaction is a multiline description terminated by a bare amount line --
# no "R$", no sign; direction comes only from which section is open. This
# fixture models one day with only one direction each and a second day that
# switches from entradas to saídas mid-day, plus a genuinely multiline
# description, so both the section-switch and description-accumulation
# state machines are exercised.
# ---------------------------------------------------------------------------

NUBANK_BANK_STATEMENT_LINES = (
    "Nubank",
    "01 DE AGOSTO DE 2026 a 31 DE AGOSTO DE 2026",
    "Saldo inicial R$ 1.000,00",
    "Rendimento liquido R$ 5,00",
    "Total de entradas R$ 350,00",
    "Total de saidas R$ 120,00",
    "Saldo final do periodo R$ 1.230,00",
    "14 AGO 2026 Total de entradas + 300,00",
    "Transferencia recebida pelo Pix",
    "Fulano de Tal",
    "300,00",
    "Total de saidas - 20,00",
    "Compra no debito Padaria Modelo",
    "20,00",
    "20 AGO 2026 Total de entradas + 50,00",
    "Recebimento Pix Ciclano",
    "50,00",
    "Total de saidas - 100,00",
    "Transferencia enviada pelo Pix",
    "Beltrano de Souza",
    "100,00",
)


def nubank_bank_statement_pdf() -> bytes:
    return render_single_column_pdf(NUBANK_BANK_STATEMENT_LINES)


NUBANK_BANK_STATEMENT_ZERO_MOVEMENT_LINES = (
    "Nubank",
    "01 DE AGOSTO DE 2026 a 31 DE AGOSTO DE 2026",
    "Saldo inicial R$ 0,00",
    "Total de entradas R$ 0,00",
    "Total de saidas R$ 0,00",
    "Saldo final do periodo R$ 0,00",
)


def nubank_bank_statement_pdf_zero_movement() -> bytes:
    return render_single_column_pdf(NUBANK_BANK_STATEMENT_ZERO_MOVEMENT_LINES)


# ---------------------------------------------------------------------------
# Mercado Pago -- fatura (credit card). Layout per the Work Order's
# "Mercado Pago — fatura PDF" section: "Vencimento: dd/mm/yyyy", a summary
# block whose bare "Total R$ valor" line is the only one this parser reads
# as the declared total (it does *not* declare a previous-balance/carry-over
# figure, so `opening_balance` stays `None` -- see
# `_mercado_pago_credit_card_declared_fields`), and transaction lines under
# a "Cartão <bandeira> [****last4]" marker.
# ---------------------------------------------------------------------------

MERCADO_PAGO_CREDIT_CARD_LINES = (
    "Mercado Pago",
    "Vencimento: 10/01/2027",
    "Resumo da fatura",
    "Consumos de dezembro R$ 150,00",
    "Tarifas e encargos R$ 5,00",
    "Pagamentos e creditos devolvidos R$ -120,00",
    "Total R$ 55,00",
    "Detalhes de consumo",
    "Movimentacoes na fatura",
    "12/01 Pagamento da fatura R$ 100,00",
    "12/01 Devolucao R$ 20,00",
    "Cartao Visa [************1624]",
    "08/11 Compra Loja Um",
    "Parcela 4 de 18 R$ 54,85",
    "09/12 Compra Loja Dois R$ 30,00",
)


def mercado_pago_credit_card_pdf() -> bytes:
    return render_single_column_pdf(MERCADO_PAGO_CREDIT_CARD_LINES)


# ---------------------------------------------------------------------------
# Mercado Pago -- extrato de conta (bank statement). Layout per the Work
# Order's "Mercado Pago — extrato de conta PDF" section: "Periodo: De
# dd-mm-yyyy al dd-mm-yyyy", a "Data Descrição ID da operação Valor Saldo"
# table, rows that may wrap their description across lines before the
# trailing "<id> R$ valor R$ saldo".
# ---------------------------------------------------------------------------

MERCADO_PAGO_BANK_STATEMENT_LINES = (
    "Mercado Pago",
    "EXTRATO DE CONTA",
    "Periodo: De 01-02-2026 al 28-02-2026",
    "Saldo inicial R$ 1.000,00",
    "Entradas R$ 12,70",
    "Saidas R$ 50,00",
    "Saldo final R$ 962,70",
    "Data Descricao ID da operacao Valor Saldo",
    "05-02-2026 Reembolso",
    "Compra garantida 123456 R$ 12,70 R$ 1.012,70",
    "07-02-2026 Pagamento Loja X 654321 R$ -50,00 R$ 962,70",
)


def mercado_pago_bank_statement_pdf() -> bytes:
    return render_single_column_pdf(MERCADO_PAGO_BANK_STATEMENT_LINES)


MERCADO_PAGO_BANK_STATEMENT_ZERO_MOVEMENT_LINES = (
    "Mercado Pago",
    "EXTRATO DE CONTA",
    "Periodo: De 01-02-2026 al 28-02-2026",
    "Saldo inicial R$ 0,00",
    "Entradas R$ 0,00",
    "Saidas R$ 0,00",
    "Saldo final R$ 0,00",
    "Data Descricao ID da operacao Valor Saldo",
)


def mercado_pago_bank_statement_pdf_zero_movement() -> bytes:
    return render_single_column_pdf(MERCADO_PAGO_BANK_STATEMENT_ZERO_MOVEMENT_LINES)


# ---------------------------------------------------------------------------
# Reciprocal ambiguity: a genuine document from one issuer naming *another*
# issuer as a counterparty must still resolve to its own, real identity --
# document-identity/layout markers decide, never a brand mention alone.
# ---------------------------------------------------------------------------

MERCADO_PAGO_BANK_STATEMENT_MENTIONING_NUBANK_LINES = (
    "Mercado Pago",
    "EXTRATO DE CONTA",
    "Periodo: De 01-02-2026 al 28-02-2026",
    "Saldo inicial R$ 1.000,00",
    "Saldo final R$ 950,00",
    "Data Descricao ID da operacao Valor Saldo",
    "05-02-2026 Pagamento fatura Nubank 111111 R$ -50,00 R$ 950,00",
)


def mercado_pago_bank_statement_pdf_mentioning_nubank() -> bytes:
    return render_single_column_pdf(MERCADO_PAGO_BANK_STATEMENT_MENTIONING_NUBANK_LINES)


NUBANK_CREDIT_CARD_MENTIONING_MERCADO_PAGO_LINES = (
    "Nubank",
    "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
    "RESUMO DA FATURA ATUAL",
    "Total a pagar R$ 80,00",
    "TRANSACOES DE 26 JUN A 25 JUL",
    "24 JUN Pagamento Mercado Pago Loja R$ 80,00",
)


def nubank_credit_card_pdf_mentioning_mercado_pago() -> bytes:
    return render_single_column_pdf(NUBANK_CREDIT_CARD_MENTIONING_MERCADO_PAGO_LINES)


# ---------------------------------------------------------------------------
# Unsupported issuer (PR #41 engineer review on `c260aed`): a textual PDF
# that is transaction-row-shaped like Itaú's own layout
# (`dd/mm(/yyyy) description amount`) but carries none of the three known
# issuers' document-identity markers -- no Nubank/Mercado Pago brand+layout
# combination, and none of Itaú's own balance/emission vocabulary ("SALDO
# ANTERIOR", "SALDO DO DIA", "SALDO FINAL EM", "EMISSAO:"). `_detect_pdf_issuer`
# must resolve this as `"unknown"`, and `parse_credit_card_pdf`/
# `parse_bank_statement_pdf` must reject it for review instead of guessing
# Itaú from the row shape alone.
# ---------------------------------------------------------------------------

UNSUPPORTED_BANK_STATEMENT_LINES = (
    "Banco Delta",
    "Extrato mensal",
    "05/08/2026 Compra Loja Delta -120,00",
    "06/08/2026 Deposito Recebido 300,00",
)


def unsupported_issuer_bank_statement_pdf() -> bytes:
    return render_single_column_pdf(UNSUPPORTED_BANK_STATEMENT_LINES)


# PR #41 engineer review (head `e42a470`), second round: requiring
# `_ITAU_STRUCTURE` alone let an unrelated institution's statement be
# accepted as Itaú just because it happened to print the same generic
# Portuguese balance/emission vocabulary ("SALDO ANTERIOR"/"SALDO DO DIA").
# This fixture reproduces exactly that: an Itaú-shaped transaction row *and*
# that vocabulary, but no Itaú brand identity anywhere -- `_detect_pdf_issuer`
# must still resolve `"unknown"` rather than silently misattributing an
# unsupported document to a supported issuer.
UNSUPPORTED_BANK_STATEMENT_WITH_ITAU_STRUCTURE_VOCABULARY_LINES = (
    "Banco Delta",
    "Extrato mensal",
    "SALDO ANTERIOR 1000,00",
    "05/08/2026 Compra Loja Delta -120,00",
    "06/08/2026 SALDO DO DIA 06/08/2026 880,00",
)


def unsupported_issuer_bank_statement_pdf_with_itau_structure_vocabulary() -> bytes:
    return render_single_column_pdf(UNSUPPORTED_BANK_STATEMENT_WITH_ITAU_STRUCTURE_VOCABULARY_LINES)


# PR #41 engineer review (head `82db527`), third round: a bare "ITAU" brand
# token anywhere in the document -- even paired with `_ITAU_STRUCTURE` -- is
# still not identity. "Itaú" is a common Pix/TED/boleto counterparty name,
# so an unrelated institution's statement can legitimately mention it in a
# transaction description while also happening to print the same generic
# balance vocabulary. This fixture reproduces exactly that collision: an
# unsupported bank's own structure vocabulary *and* an "ITAU" counterparty
# reference, but no Itaú-owned identity ("Itaú Unibanco"/"itau.com.br")
# anywhere. `_detect_pdf_issuer` must still resolve `"unknown"`.
UNSUPPORTED_BANK_STATEMENT_WITH_ITAU_STRUCTURE_AND_COUNTERPARTY_LINES = (
    "Banco Delta",
    "Extrato mensal",
    "SALDO ANTERIOR 1000,00",
    "05/08/2026 PIX TRANSF ITAU SILVA -200,00",
    "06/08/2026 SALDO DO DIA 06/08/2026 800,00",
)


def unsupported_issuer_bank_statement_pdf_with_itau_structure_and_counterparty() -> bytes:
    return render_single_column_pdf(
        UNSUPPORTED_BANK_STATEMENT_WITH_ITAU_STRUCTURE_AND_COUNTERPARTY_LINES
    )


UNSUPPORTED_CREDIT_CARD_LINES = (
    "Banco Delta",
    "Fatura mensal",
    "05/08 Compra Loja Delta 120,00",
    "06/08 Assinatura Servico X 40,00",
)


def unsupported_issuer_credit_card_pdf() -> bytes:
    return render_single_column_pdf(UNSUPPORTED_CREDIT_CARD_LINES)
