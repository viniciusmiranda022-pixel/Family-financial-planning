"""Synthetic textual PDF fixture for `app.services.importer.parse_payroll_pdf`.

Every value below is invented for these tests: no real employee, employer,
CPF, matrícula, bank account or amount from any actual holerite. The layout
(labels, order, punctuation) is shaped only to satisfy the three regexes
`parse_payroll_pdf` (`app/services/importer.py`) actually reads --
`MES: .../CREDITO: .../LIQUIDO A RECEBER: R$ ...`, an `IDENTIFICACAO` block
followed by `<matrícula> - <nome>`, and a `TOTAL GANHOS   TOTAL DESCONTOS`
header immediately followed by its three totals -- plus one `BANCO BRASIL
<parcela>/<total> <valor>` consignado (payroll-loan) line, the only payroll
loan format the parser recognizes. "Fulano de Tal" and "0001" are the same
kind of obviously fictitious placeholder identity already used by
`tests/fixtures/pdf_documents.py`'s Nubank statement fixture, never a real
name or registration number.

Rendered as a PDF *at test time* via `render_single_column_pdf`, exactly like
every other fixture in this package -- real financial PDFs are gitignored
(`*.pdf`) and must never be committed.
"""

from __future__ import annotations

from tests.fixtures.pdf_documents import render_single_column_pdf

# Gross (Salário Base) minus deductions (INSS + IRRF + consignado) equals the
# declared net exactly, so `parse_payroll_pdf`'s own self-consistency check
# (`net != total_net` raises `ValueError`) passes: 4500.00 - (450.00 +
# 654.33 + 150.00) = 3245.67.
PAYROLL_HOLERITE_LINES = (
    "HOLERITE - CONTRACHEQUE",
    "IDENTIFICACAO",
    "0001 - Fulano de Tal",
    "MES: AGOSTO/2026 CREDITO: 05/09/2026 LIQUIDO A RECEBER: R$ 3.245,67",
    "DESCRICAO VALOR",
    "Salario Base 4.500,00",
    "INSS 450,00",
    "IRRF 654,33",
    "BANCO BRASIL 003/048 150,00",
    "TOTAL GANHOS TOTAL DESCONTOS LIQUIDO",
    "4.500,00 1.254,33 3.245,67",
)


def payroll_holerite_pdf() -> bytes:
    return render_single_column_pdf(PAYROLL_HOLERITE_LINES)


# A holerite missing the mandatory `IDENTIFICACAO` block: `parse_payroll_pdf`
# must reject it for review (`person` regex fails to match) rather than
# fabricate a name or silently drop the field. Never used to assert a
# reconciliation status -- the document must never be parsed at all.
PAYROLL_HOLERITE_MISSING_IDENTIFICATION_LINES = (
    "HOLERITE - CONTRACHEQUE",
    "MES: AGOSTO/2026 CREDITO: 05/09/2026 LIQUIDO A RECEBER: R$ 3.245,67",
    "TOTAL GANHOS TOTAL DESCONTOS LIQUIDO",
    "4.500,00 1.254,33 3.245,67",
)


def payroll_holerite_pdf_missing_identification() -> bytes:
    return render_single_column_pdf(PAYROLL_HOLERITE_MISSING_IDENTIFICATION_LINES)
