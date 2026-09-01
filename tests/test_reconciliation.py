from datetime import date
from decimal import Decimal

from app.services.importer import ParsedDocument, ParsedTransaction, parse_document_contract
from app.services.reconciliation import reconcile_parsed_document


def test_statement_reconciliation_uses_declared_balances_without_inference() -> None:
    parsed = ParsedDocument(
        document_type="bank_statement",
        parser_name="test",
        parser_version="1",
        transactions=(
            ParsedTransaction(date(2026, 8, 1), "Crédito", Decimal("50"), 1),
            ParsedTransaction(date(2026, 8, 2), "Débito", Decimal("-20"), 2),
        ),
        declared_fields={"opening_balance": Decimal("100"), "closing_balance": Decimal("130")},
        calculated_fields={"credits_total": Decimal("50"), "debits_total": Decimal("20")},
        reconciliation_formula="statement_opening_plus_credits_minus_debits",
        coverage={"transactions": True, "opening_balance": True, "closing_balance": True},
        period="2026-08",
    )

    result = reconcile_parsed_document(parsed)

    assert result.status == "reconciled"
    assert result.closing_balance_calculated == Decimal("130.00")
    assert result.difference == Decimal("0.00")


def test_missing_statement_balance_is_unknown_not_artificially_reconciled() -> None:
    parsed = ParsedDocument(
        document_type="bank_statement",
        parser_name="test",
        parser_version="1",
        declared_fields={"opening_balance": Decimal("100")},
        calculated_fields={"credits_total": Decimal("0"), "debits_total": Decimal("0")},
        reconciliation_formula="statement_opening_plus_credits_minus_debits",
        coverage={"transactions": False, "opening_balance": True, "closing_balance": False},
    )

    result = reconcile_parsed_document(parsed)

    assert result.status == "unknown"
    assert result.reason == "missing_required_statement_balance"
    assert result.closing_balance_calculated is None


def test_card_and_payroll_reconciliation_detect_real_difference() -> None:
    card = ParsedDocument(
        document_type="credit_card",
        parser_name="test",
        parser_version="1",
        declared_fields={"opening_balance": Decimal("100"), "declared_total": Decimal("170")},
        calculated_fields={
            "purchases_total": Decimal("80"),
            "fees_total": Decimal("5"),
            "refunds_total": Decimal("5"),
            "payments_total": Decimal("10"),
        },
        reconciliation_formula="card_previous_plus_purchases_fees_minus_credits_payments",
        coverage={},
    )
    payroll = ParsedDocument(
        document_type="payroll",
        parser_name="test",
        parser_version="1",
        declared_fields={
            "gross_amount": Decimal("6000"),
            "deductions": Decimal("500"),
            "net_amount": Decimal("5400"),
        },
        calculated_fields={"calculated_net": Decimal("5500")},
        reconciliation_formula="payroll_gross_minus_deductions",
        coverage={},
    )

    assert reconcile_parsed_document(card).status == "reconciled"
    payroll_result = reconcile_parsed_document(payroll)
    assert payroll_result.status == "not_reconciled"
    assert payroll_result.difference == Decimal("-100.00")


def test_empty_csv_is_rejected_instead_of_becoming_a_zero_record_import() -> None:
    payload = b"date,description,amount\n"

    try:
        parse_document_contract("empty.csv", payload, "bank_statement")
    except ValueError as exc:
        assert "sem lançamentos" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("empty financial document must be rejected")
