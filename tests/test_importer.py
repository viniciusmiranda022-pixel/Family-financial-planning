from decimal import Decimal

from app.services.classifier import classify
from app.services.importer import parse_csv, parse_decimal, transaction_fingerprint


def test_brazilian_money_parser() -> None:
    assert parse_decimal("1.724,90") == Decimal("1724.90")
    assert parse_decimal("- 80,44") == Decimal("-80.44")
    assert parse_decimal("(12,50)") == Decimal("-12.50")


def test_nubank_csv_normalizes_charge_and_payment() -> None:
    payload = b"date,title,amount\n2026-08-04,iFood - NuPay,44.88\n2026-08-03,Pagamento recebido,-1724.90\n"
    rows = parse_csv(payload, "credit_card")
    assert rows[0].amount == Decimal("-44.88")
    assert rows[1].amount == Decimal("1724.90")
    assert classify(rows[1].description, float(rows[1].amount)).excluded is True


def test_exact_overlap_has_same_fingerprint() -> None:
    payload = b"date,title,amount\n2026-08-04,iFood - NuPay,44.88\n"
    first = parse_csv(payload, "credit_card")[0]
    second = parse_csv(payload, "credit_card")[0]
    assert transaction_fingerprint("account-1", first) == transaction_fingerprint("account-1", second)


def test_internal_transfer_requires_review_and_is_excluded() -> None:
    result = classify("PIX TRANSF KELLY", -100)
    assert result.excluded is True
    assert result.review_reason is not None
