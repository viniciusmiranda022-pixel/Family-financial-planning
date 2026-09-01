"""Deterministic document reconciliation without inferred financial facts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.importer import ParsedDocument, parse_decimal

RECONCILIATION_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: str
    formula_id: str
    tolerance: Decimal
    period: str | None = None
    declared_total: Decimal | None = None
    reconstructed_total: Decimal | None = None
    difference: Decimal | None = None
    opening_balance: Decimal | None = None
    closing_balance_declared: Decimal | None = None
    closing_balance_calculated: Decimal | None = None
    credits_total: Decimal | None = None
    debits_total: Decimal | None = None
    purchases_total: Decimal | None = None
    fees_total: Decimal | None = None
    refunds_total: Decimal | None = None
    payments_total: Decimal | None = None
    coverage: dict[str, bool] | None = None
    evidence: dict[str, Any] | None = None
    reason: str | None = None


def reconcile_parsed_document(parsed: ParsedDocument) -> ReconciliationResult:
    if parsed.document_type == "payroll":
        return _reconcile_payroll(parsed)
    if parsed.document_type == "bank_statement":
        return _reconcile_statement(parsed)
    if parsed.document_type == "credit_card":
        return _reconcile_card(parsed)
    return _unknown(parsed, "unsupported_document_type")


def unknown_reconciliation(
    *,
    document_type: str,
    parser_name: str,
    reason: str,
) -> tuple[ParsedDocument, ReconciliationResult]:
    parsed = ParsedDocument(
        document_type=document_type,
        parser_name=parser_name,
        parser_version="unknown",
        reconciliation_formula="unsupported",
        coverage={},
        warnings=(reason,),
    )
    return parsed, _unknown(parsed, reason)


def persist_reconciliation(
    db: Session,
    *,
    household_id: str,
    document_id: str,
    parsed: ParsedDocument,
    result: ReconciliationResult,
    account_id: str | None = None,
    confirmed_by: str | None = None,
    trace_id: str | None = None,
) -> Any:
    from app.models import AccountBalanceObservation, DocumentReconciliation

    trace = trace_id or str(uuid.uuid4())
    item = DocumentReconciliation(
        household_id=household_id,
        document_id=document_id,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        formula_id=result.formula_id,
        status=result.status,
        period=result.period,
        declared_total=result.declared_total,
        reconstructed_total=result.reconstructed_total,
        difference=result.difference,
        opening_balance=result.opening_balance,
        closing_balance_declared=result.closing_balance_declared,
        closing_balance_calculated=result.closing_balance_calculated,
        credits_total=result.credits_total,
        debits_total=result.debits_total,
        purchases_total=result.purchases_total,
        fees_total=result.fees_total,
        refunds_total=result.refunds_total,
        payments_total=result.payments_total,
        tolerance=result.tolerance,
        coverage=result.coverage or {},
        evidence=_json_safe(result.evidence or {}),
        reason=result.reason,
        trace_id=trace,
    )
    db.add(item)
    db.flush()

    as_of_raw = parsed.declared_fields.get("as_of_date")
    if (
        account_id
        and result.closing_balance_declared is not None
        and isinstance(as_of_raw, str)
    ):
        try:
            as_of_date = date.fromisoformat(as_of_raw)
        except ValueError:
            as_of_date = None
        if as_of_date and not db.scalar(
            select(AccountBalanceObservation.id).where(
                AccountBalanceObservation.household_id == household_id,
                AccountBalanceObservation.account_id == account_id,
                AccountBalanceObservation.document_id == document_id,
                AccountBalanceObservation.observation_type == "closing",
                AccountBalanceObservation.as_of_date == as_of_date,
            )
        ):
            db.add(
                AccountBalanceObservation(
                    household_id=household_id,
                    account_id=account_id,
                    amount=result.closing_balance_declared,
                    as_of_date=as_of_date,
                    observation_type="closing",
                    source="statement",
                    document_id=document_id,
                    confirmed_by=confirmed_by,
                    confidence=(
                        Decimal("1.0000")
                        if result.status == "reconciled"
                        else Decimal("0.7000")
                    ),
                    trace_id=trace,
                )
            )
    return item


def serialize_reconciliation(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "document_id": item.document_id,
        "parser_name": item.parser_name,
        "parser_version": item.parser_version,
        "formula_id": item.formula_id,
        "status": item.status,
        "period": item.period,
        "declared_total": _decimal_string(item.declared_total),
        "reconstructed_total": _decimal_string(item.reconstructed_total),
        "difference": _decimal_string(item.difference),
        "opening_balance": _decimal_string(item.opening_balance),
        "closing_balance_declared": _decimal_string(item.closing_balance_declared),
        "closing_balance_calculated": _decimal_string(item.closing_balance_calculated),
        "credits_total": _decimal_string(item.credits_total),
        "debits_total": _decimal_string(item.debits_total),
        "purchases_total": _decimal_string(item.purchases_total),
        "fees_total": _decimal_string(item.fees_total),
        "refunds_total": _decimal_string(item.refunds_total),
        "payments_total": _decimal_string(item.payments_total),
        "tolerance": _decimal_string(item.tolerance),
        "coverage": item.coverage,
        "evidence": item.evidence,
        "reason": item.reason,
        "reconciled_at": item.reconciled_at.isoformat(),
        "trace_id": item.trace_id,
    }


def _reconcile_statement(parsed: ParsedDocument) -> ReconciliationResult:
    opening = _optional_money(parsed.declared_fields.get("opening_balance"))
    closing = _optional_money(parsed.declared_fields.get("closing_balance"))
    credits = _money(parsed.calculated_fields.get("credits_total", 0))
    debits = _money(parsed.calculated_fields.get("debits_total", 0))
    coverage = dict(parsed.coverage)
    if opening is None or closing is None:
        return ReconciliationResult(
            status="unknown",
            formula_id=parsed.reconciliation_formula,
            tolerance=RECONCILIATION_TOLERANCE,
            period=parsed.period,
            opening_balance=opening,
            closing_balance_declared=closing,
            credits_total=credits,
            debits_total=debits,
            coverage=coverage,
            evidence=_evidence(parsed),
            reason="missing_required_statement_balance",
        )
    calculated = _money(opening + credits - debits)
    difference = _money(closing - calculated)
    return ReconciliationResult(
        status=_status(difference),
        formula_id=parsed.reconciliation_formula,
        tolerance=RECONCILIATION_TOLERANCE,
        period=parsed.period,
        declared_total=closing,
        reconstructed_total=calculated,
        difference=difference,
        opening_balance=opening,
        closing_balance_declared=closing,
        closing_balance_calculated=calculated,
        credits_total=credits,
        debits_total=debits,
        coverage=coverage,
        evidence=_evidence(parsed),
    )


def _reconcile_card(parsed: ParsedDocument) -> ReconciliationResult:
    opening = _optional_money(parsed.declared_fields.get("opening_balance"))
    declared = _optional_money(parsed.declared_fields.get("declared_total"))
    purchases = _money(parsed.calculated_fields.get("purchases_total", 0))
    fees = _money(parsed.calculated_fields.get("fees_total", 0))
    refunds = _money(parsed.calculated_fields.get("refunds_total", 0))
    payments = _money(parsed.calculated_fields.get("payments_total", 0))
    if opening is None or declared is None:
        return ReconciliationResult(
            status="unknown",
            formula_id=parsed.reconciliation_formula,
            tolerance=RECONCILIATION_TOLERANCE,
            period=parsed.period,
            declared_total=declared,
            opening_balance=opening,
            purchases_total=purchases,
            fees_total=fees,
            refunds_total=refunds,
            payments_total=payments,
            coverage=dict(parsed.coverage),
            evidence=_evidence(parsed),
            reason="missing_required_card_components",
        )
    reconstructed = _money(opening + purchases + fees - refunds - payments)
    difference = _money(declared - reconstructed)
    return ReconciliationResult(
        status=_status(difference),
        formula_id=parsed.reconciliation_formula,
        tolerance=RECONCILIATION_TOLERANCE,
        period=parsed.period,
        declared_total=declared,
        reconstructed_total=reconstructed,
        difference=difference,
        opening_balance=opening,
        purchases_total=purchases,
        fees_total=fees,
        refunds_total=refunds,
        payments_total=payments,
        coverage=dict(parsed.coverage),
        evidence=_evidence(parsed),
    )


def _reconcile_payroll(parsed: ParsedDocument) -> ReconciliationResult:
    gross = _optional_money(parsed.declared_fields.get("gross_amount"))
    deductions = _optional_money(parsed.declared_fields.get("deductions"))
    declared = _optional_money(parsed.declared_fields.get("net_amount"))
    calculated = _optional_money(parsed.calculated_fields.get("calculated_net"))
    if None in {gross, deductions, declared, calculated}:
        return _unknown(parsed, "missing_required_payroll_field")
    difference = _money(declared - calculated)
    return ReconciliationResult(
        status=_status(difference),
        formula_id=parsed.reconciliation_formula,
        tolerance=RECONCILIATION_TOLERANCE,
        period=parsed.period,
        declared_total=declared,
        reconstructed_total=calculated,
        difference=difference,
        credits_total=gross,
        debits_total=deductions,
        coverage=dict(parsed.coverage),
        evidence=_evidence(parsed),
    )


def _unknown(parsed: ParsedDocument, reason: str) -> ReconciliationResult:
    return ReconciliationResult(
        status="unknown",
        formula_id=parsed.reconciliation_formula,
        tolerance=RECONCILIATION_TOLERANCE,
        period=parsed.period,
        coverage=dict(parsed.coverage),
        evidence=_evidence(parsed),
        reason=reason,
    )


def _status(difference: Decimal) -> str:
    return "reconciled" if abs(difference) <= RECONCILIATION_TOLERANCE else "not_reconciled"


def _evidence(parsed: ParsedDocument) -> dict[str, Any]:
    return {
        "declared_fields": _json_safe(dict(parsed.declared_fields)),
        "calculated_fields": _json_safe(dict(parsed.calculated_fields)),
        "warnings": list(parsed.warnings),
        "transaction_count": len(parsed.transactions),
    }


def _money(value: object) -> Decimal:
    return parse_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _optional_money(value: object | None) -> Decimal | None:
    return None if value is None else _money(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _decimal_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
