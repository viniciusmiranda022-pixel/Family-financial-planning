"""Regression tests for Nubank current-invoice summary reconciliation.

Synthetic values only. No real PDF, PII, account number, merchant or user
financial amount is stored in this fixture.
"""

from decimal import Decimal

from app.services.importer import parse_document_contract
from app.services.reconciliation import reconcile_parsed_document
from tests.fixtures.pdf_documents import render_single_column_pdf


def _invoice_with_repeated_summary_labels() -> bytes:
    return render_single_column_pdf(
        (
            "Nubank",
            "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
            # Decoy outside the canonical current-invoice summary.
            "OPCOES DE PAGAMENTO",
            "Total a pagar R$ 999,00",
            "RESUMO DA FATURA ATUAL",
            "Fatura anterior R$ 300,00",
            "Pagamento recebido -R$ 300,00",
            "Total de compras R$ 450,00",
            "Total a pagar R$ 450,00",
            # Repeated simulation labels after the canonical total.
            "SIMULACAO DE PARCELAMENTO",
            "Fatura anterior R$ 0,00",
            "Total a pagar R$ 75,00",
            "TRANSACOES DE 26 JUN A 25 JUL",
            "24 JUN Loja Exemplo R$ 450,00",
            "01 JUL Pagamento em 01 JUL -R$ 300,00",
        )
    )


def _invoice_without_current_summary() -> bytes:
    return render_single_column_pdf(
        (
            "Nubank",
            "FATURA 03 AGO 2026 EMISSAO E ENVIO 25 JUL 2026",
            "OPCOES DE PAGAMENTO",
            "Fatura anterior R$ 888,00",
            "Total a pagar R$ 999,00",
            "TRANSACOES DE 26 JUN A 25 JUL",
            "24 JUN Loja Exemplo R$ 100,00",
        )
    )


def test_nubank_declared_fields_are_scoped_to_current_invoice_summary() -> None:
    parsed = parse_document_contract(
        "fatura.pdf",
        _invoice_with_repeated_summary_labels(),
        "credit_card",
    )

    assert parsed.declared_fields["opening_balance"] == Decimal("300.00")
    assert parsed.declared_fields["declared_total"] == Decimal("450.00")
    assert parsed.calculated_fields["purchases_total"] == Decimal("450.00")
    assert parsed.calculated_fields["payments_total"] == Decimal("300.00")

    result = reconcile_parsed_document(parsed)
    assert result.status == "reconciled"
    assert result.reconstructed_total == Decimal("450.00")
    assert result.difference == Decimal("0.00")


def test_nubank_declared_fields_fail_closed_without_current_summary() -> None:
    parsed = parse_document_contract(
        "fatura.pdf",
        _invoice_without_current_summary(),
        "credit_card",
    )

    assert parsed.declared_fields["opening_balance"] is None
    assert parsed.declared_fields["declared_total"] is None

    result = reconcile_parsed_document(parsed)
    assert result.status == "unknown"
    assert result.reason == "missing_required_card_components"