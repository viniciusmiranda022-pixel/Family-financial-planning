"""Shared synthetic regression dataset for the Financial Integrity Engine.

`build_synthetic_household` assembles one fully fictitious household that
exercises, in a single reusable fixture, the source data shapes the
documented financial rules, reconciliation, duplicate detection, canonical
snapshots, projection and monthly close all need to be exercised against:

- operating income/expense transactions;
- an internal transfer pair (INV-001);
- a patrimonial investment/redemption pair (INV-003/INV-004);
- a credit-card purchase reconciled by a card-payment transaction (INV-002);
- an unresolved cross-account duplicate pair (INV-014) -- deliberately left
  *unclassified* (`canonical_status="unassigned"`, no `duplicate_group_id`)
  so both the live import-time duplicate pass and `app.cli.backfill` have a
  real candidate to classify;
- two `Document` rows with **no** `DocumentReconciliation` yet -- the
  "imported but not reconciled" state `app.cli.backfill` is expected to
  close out with an explicit `unknown` reconciliation record rather than
  fabricated evidence (see docs/WORK_ORDER_PR8_...: "não inferir data nem
  reconstruir saldo por suposição");
- a `Commission` (receivable, competence-bound) and a `PayrollRecord`
  feeding the projection engine's inputs;
- an active recurring `Obligation`;
- a `FinancialProfile` whose `investment_balance` has **no** corresponding
  `AccountBalanceObservation` by default (`with_balance_observation=False`),
  reproducing the documented "legacy balance without reliable evidence"
  case that must stay `unknown`/untrusted rather than being inferred.

Every row is entirely fictitious: no real account numbers, documents, or
personal data. Callers that need the derived state (reconciliations,
duplicate groups, snapshots, integrity runs) build it themselves by calling
the relevant service against the object this function returns -- this
fixture only ever seeds *source* facts, never derived ones, so it stays
useful both for testing the live/import-time code paths and for testing
`app.cli.backfill` reprocessing the exact same "not yet processed" shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    Account,
    Category,
    Commission,
    Document,
    FinancialProfile,
    Household,
    Obligation,
    PayrollRecord,
    Transaction,
    User,
)

DEFAULT_PERIOD = "2026-06"


@dataclass(slots=True)
class SyntheticHousehold:
    household: Household
    user: User
    checking: Account
    credit_card: Account
    investment: Account
    categories: dict[str, Category]
    profile: FinancialProfile
    transactions: dict[str, Transaction]
    documents: dict[str, Document]
    commission: Commission
    payroll_record: PayrollRecord
    obligation: Obligation
    period: str
    duplicate_pair: tuple[Transaction, Transaction] = field(init=False)

    def __post_init__(self) -> None:
        self.duplicate_pair = (
            self.transactions["duplicate_statement_copy"],
            self.transactions["duplicate_card_copy"],
        )


def _period_start(period: str) -> date:
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, 1)


def _document(
    household_id: str,
    account_id: str | None,
    *,
    name: str,
    document_type: str,
    sha_suffix: str,
) -> Document:
    return Document(
        household_id=household_id,
        account_id=account_id,
        original_name=name,
        document_type=document_type,
        sha256=sha_suffix.rjust(64, "0"),
        encrypted_path=f"/data/documents/synthetic-{sha_suffix}.enc",
        status="processed",
        record_count=1,
    )


def build_synthetic_household(
    db: Session,
    *,
    name: str = "Família Sintética",
    period: str = DEFAULT_PERIOD,
    with_balance_observation: bool = False,
) -> SyntheticHousehold:
    """Persist and return one complete, fictitious regression household.

    `db.flush()` is called at the end so every row has a generated id and
    every foreign key is resolvable; the caller owns commit/rollback.
    """

    start = _period_start(period)

    household = Household(name=name)
    db.add(household)
    db.flush()

    user = User(
        household_id=household.id,
        name="Usuário Sintético",
        username=f"sintetico-{household.id[:8]}",
        password_hash="not-a-real-hash",
    )
    checking = Account(household_id=household.id, name="Conta Corrente", account_type="checking")
    credit_card = Account(household_id=household.id, name="Cartão de Crédito", account_type="credit_card")
    investment = Account(household_id=household.id, name="Reserva DI", account_type="investment")
    db.add_all([user, checking, credit_card, investment])
    db.flush()

    category_names = (
        "Receitas",
        "Alimentação",
        "Transferência interna",
        "Transferência patrimonial",
        "Conciliação",
        "Revisar",
    )
    categories = {label: Category(household_id=household.id, name=label) for label in category_names}
    db.add_all(categories.values())
    db.flush()

    profile = FinancialProfile(
        household_id=household.id,
        monthly_salary_net=Decimal("6000.00"),
        monthly_cash_cap=Decimal("4500.00"),
        emergency_floor=Decimal("2000.00"),
        food_allowance=Decimal("400.00"),
        meal_allowance_daily=Decimal("35.00"),
        workdays_month=21,
        investment_name=investment.name,
        investment_balance=Decimal("10000.00"),
    )
    db.add(profile)
    db.flush()

    statement_document = _document(
        household.id,
        checking.id,
        name="extrato-conta-corrente.pdf",
        document_type="bank_statement",
        sha_suffix=f"stmt{household.id[:12]}",
    )
    card_document = _document(
        household.id,
        credit_card.id,
        name="fatura-cartao.pdf",
        document_type="credit_card",
        sha_suffix=f"card{household.id[:12]}",
    )
    payroll_document = _document(
        household.id,
        None,
        name="contracheque.pdf",
        document_type="payroll",
        sha_suffix=f"pay{household.id[:12]}",
    )
    db.add_all([statement_document, card_document, payroll_document])
    db.flush()

    def txn(
        key: str,
        *,
        account: Account,
        category: Category | None,
        amount: str,
        transaction_type: str,
        description: str,
        document: Document | None = None,
        day: int = 5,
        excluded: bool = False,
    ) -> Transaction:
        booked_at = start.replace(day=day)
        return Transaction(
            household_id=household.id,
            account_id=account.id,
            category_id=category.id if category else None,
            document_id=document.id if document else None,
            booked_at=booked_at,
            description=description,
            normalized_description=description.upper(),
            amount=Decimal(amount),
            transaction_type=transaction_type,
            fingerprint=key.rjust(64, "0"),
            competence=period,
            canonical_status="canonical",
            excluded=excluded,
        )

    transactions = {
        "salary": txn(
            "income001",
            account=checking,
            category=categories["Receitas"],
            amount="6000.00",
            transaction_type="income",
            description="Salário",
            document=statement_document,
            day=1,
        ),
        "groceries": txn(
            "expense001",
            account=checking,
            category=categories["Alimentação"],
            amount="-450.00",
            transaction_type="expense",
            description="Supermercado",
            document=statement_document,
            day=6,
        ),
        "internal_transfer_out": txn(
            "transfer001",
            account=checking,
            category=categories["Transferência interna"],
            amount="-500.00",
            transaction_type="transfer",
            description="Transferência entre contas",
            document=statement_document,
            day=7,
        ),
        "internal_transfer_in": txn(
            "transfer002",
            account=credit_card,
            category=categories["Transferência interna"],
            amount="500.00",
            transaction_type="transfer",
            description="Transferência entre contas",
            document=statement_document,
            day=7,
        ),
        "investment_out": txn(
            "invest001",
            account=checking,
            category=categories["Transferência patrimonial"],
            amount="-1000.00",
            transaction_type="transfer",
            description="Aplicação Reserva DI",
            document=statement_document,
            day=8,
        ),
        "redemption_in": txn(
            "redeem001",
            account=checking,
            category=categories["Transferência patrimonial"],
            amount="300.00",
            transaction_type="transfer",
            description="Resgate Reserva DI",
            document=statement_document,
            day=9,
        ),
        "card_purchase": txn(
            "cardbuy001",
            account=credit_card,
            category=categories["Alimentação"],
            amount="-220.00",
            transaction_type="expense",
            description="Compra no cartão",
            document=card_document,
            day=10,
        ),
        "card_payment": txn(
            "cardpay001",
            account=checking,
            category=categories["Conciliação"],
            amount="-220.00",
            transaction_type="reconciliation",
            description="Pagamento da fatura",
            document=statement_document,
            day=15,
        ),
        # Unresolved duplicate: the same real-world purchase imported twice,
        # once from the bank statement and once from the card statement --
        # same amount/date/description, different accounts and documents.
        # Deliberately left `canonical_status="unassigned"` with no
        # `duplicate_group_id`: neither the live pipeline nor a backfill has
        # classified it yet in this fixture.
        "duplicate_statement_copy": txn(
            "dupstmt001",
            account=checking,
            category=categories["Revisar"],
            amount="-89.90",
            transaction_type="expense",
            description="Loja Exemplo",
            document=statement_document,
            day=12,
        ),
        "duplicate_card_copy": txn(
            "dupcard001",
            account=credit_card,
            category=categories["Revisar"],
            amount="-89.90",
            transaction_type="expense",
            description="Loja Exemplo",
            document=card_document,
            day=12,
        ),
    }
    for key in ("duplicate_statement_copy", "duplicate_card_copy"):
        transactions[key].canonical_status = "unassigned"
    db.add_all(transactions.values())
    db.flush()

    commission = Commission(
        household_id=household.id,
        description="Comissão sintética",
        expected_date=start.replace(day=20),
        gross_amount=Decimal("2000.00"),
        tax_rate=Decimal("0.06"),
        delay_days=60,
        status="expected",
    )
    payroll_record = PayrollRecord(
        household_id=household.id,
        document_id=payroll_document.id,
        person_name="Usuário Sintético",
        competence=start,
        payment_date=start.replace(day=1),
        payroll_kind="regular",
        gross_amount=Decimal("7500.00"),
        deductions=Decimal("1500.00"),
        net_amount=Decimal("6000.00"),
    )
    obligation = Obligation(
        household_id=household.id,
        name="Aluguel",
        due_date=start.replace(day=10),
        amount=Decimal("1800.00"),
        recurrence_months=1,
        occurrence_count=1,
        category="moradia",
    )
    db.add_all([commission, payroll_record, obligation])
    db.flush()

    if with_balance_observation:
        from app.models import AccountBalanceObservation

        db.add(
            AccountBalanceObservation(
                household_id=household.id,
                account_id=investment.id,
                amount=profile.investment_balance,
                as_of_date=start,
                observation_type="closing",
                source="manual_confirmed",
                confidence=Decimal("1.0000"),
                trace_id=f"synthetic-{household.id}",
            )
        )
        db.flush()

    return SyntheticHousehold(
        household=household,
        user=user,
        checking=checking,
        credit_card=credit_card,
        investment=investment,
        categories=categories,
        profile=profile,
        transactions=transactions,
        documents={
            "statement": statement_document,
            "card": card_document,
            "payroll": payroll_document,
        },
        commission=commission,
        payroll_record=payroll_record,
        obligation=obligation,
        period=period,
    )
