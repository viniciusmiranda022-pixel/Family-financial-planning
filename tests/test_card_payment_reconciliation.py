"""Tests for the visual card-payment <-> bank-debit reconciliation feature.

Work Order: `docs/WORK_ORDER_VISUAL_CARD_PAYMENT_RECONCILIATION.md`.

Service-level tests build directly on `tests/fixtures/synthetic_household`
(which already contains the checking-side `card_payment` reconciliation row
paying for `card_purchase`) and add the missing invoice-side "payment
received" row a real Nubank/Itaú import would also produce, to exercise the
deterministic/ambiguous/unmatched states without touching the shared fixture
used by every other suite. The HTTP-level test drives the real endpoints
(auth, household isolation, audit trail, conflict responses) the way
`tests/test_monthly_close_api.py` does, with its own fully isolated engine.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-card-payment-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "card-payment-reconciliation-test-secret-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.db import Base, get_db  # noqa: E402
from app.models import Account, AuditEvent, Household, Transaction, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.card_payment_reconciliation import (  # noqa: E402
    CardPaymentLinkError,
    link_card_payment,
    list_card_payment_reconciliations,
    unlink_card_payment,
)
from tests.fixtures.synthetic_household import build_synthetic_household  # noqa: E402


def _memory_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _card_side_payment_line(household, *, day: int = 1, amount: str = "220.00") -> Transaction:
    """The invoice's own "payment received" line, the way a real PDF/CSV
    import would persist it: positive amount, `credit_card` account,
    `transaction_type="reconciliation"` from the very same shared classifier
    that produced the fixture's `card_payment` row on the checking side.
    """

    return Transaction(
        household_id=household.household.id,
        account_id=household.credit_card.id,
        category_id=household.categories["Conciliação"].id,
        booked_at=date(2026, 6, day),
        occurred_at=date(2026, 6, day),
        competence="2026-06",
        description="Pagamento em 15 JUN",
        normalized_description="PAGAMENTO EM 15 JUN",
        amount=Decimal(amount),
        transaction_type="reconciliation",
        fingerprint=f"cardpayline{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=70,
        confidence=Decimal("1"),
        excluded=True,
    )


def _checking_match(db: Session, household) -> "object":
    checking_id = household.transactions["card_payment"].id
    matches = list_card_payment_reconciliations(db, household_id=household.household.id)
    return next(match for match in matches if match.checking_transaction_id == checking_id)


def test_deterministic_single_candidate_is_matched_not_auto_linked() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    card_line = _card_side_payment_line(household)
    db.add(card_line)
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "matched"
    assert len(match.candidates) == 1
    assert match.candidates[0].transaction_id == card_line.id
    assert match.candidates[0].difference == Decimal("0.00")
    # Read-only: presenting a deterministic candidate never writes anything.
    assert db.get(Transaction, card_line.id).linked_transaction_id is None
    assert household.transactions["card_payment"].linked_transaction_id is None


def test_ambiguous_multiple_candidates_never_auto_resolve() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    card_line_a = _card_side_payment_line(household, day=1)
    card_line_b = _card_side_payment_line(household, day=3)
    db.add_all([card_line_a, card_line_b])
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "ambiguous"
    assert {candidate.transaction_id for candidate in match.candidates} == {
        card_line_a.id,
        card_line_b.id,
    }
    assert household.transactions["card_payment"].linked_transaction_id is None


def test_missing_counterpart_stays_explicit_unmatched() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    # No invoice-side payment row exists at all: an imported bank debit
    # whose card invoice has not been imported (or parsed) yet.
    match = _checking_match(db, household)
    assert match.status == "unmatched"
    assert match.candidates == ()


def test_candidate_outside_match_window_is_not_offered() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    # `card_payment` is booked 2026-06-15; 200 days later is far outside any
    # real billing cycle and must never be silently offered as evidence.
    far_away = _card_side_payment_line(household, day=1)
    far_away.booked_at = date(2027, 1, 1)
    far_away.occurred_at = far_away.booked_at
    far_away.competence = "2027-01"
    db.add(far_away)
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "unmatched"
    assert match.candidates == ()


def _checking_side_debit(household, *, day: int, amount: str = "-220.00", suffix: str = "") -> Transaction:
    """A second checking-side reconciliation row (bank debit), independent
    of the fixture's own `card_payment` row, for the multi-checking-row
    graph-cardinality tests below."""

    return Transaction(
        household_id=household.household.id,
        account_id=household.checking.id,
        booked_at=date(2026, 6, day),
        occurred_at=date(2026, 6, day),
        competence="2026-06",
        description=f"Débito fatura{suffix}",
        normalized_description=f"DEBITO FATURA{suffix}",
        amount=Decimal(amount),
        transaction_type="reconciliation",
        fingerprint=f"chkdebit{suffix}{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=70,
        confidence=Decimal("1"),
        excluded=True,
    )


def test_orphaned_link_reference_falls_back_without_crashing() -> None:
    """Defensive corner case introduced by the bipartite-degree rewrite:
    `edges` deliberately has no entry for an already-linked checking row
    (see `_candidate_edges`), so a row whose `linked_transaction_id` fails
    to resolve to a same-household counterpart -- unreachable through this
    module's own link/unlink API, which always keeps both sides symmetric,
    but a real defensive branch nonetheless -- must fall back to a
    stand-alone candidate lookup instead of a `KeyError`.

    That stand-alone lookup has no way to know whether some *other*
    checking row also wants the same card candidate (it is deliberately
    excluded from the shared `card_degrees` count), so it reports the
    conservative `ambiguous` rather than guessing `matched` -- consistent
    with the project's "insufficient/uncertain evidence stays explicit
    review, never fabricated certainty" rule, which applies doubly to a row
    whose own link reference is already inconsistent.
    """

    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]
    checking.linked_transaction_id = "does-not-exist"
    card_line = _card_side_payment_line(household)
    db.add(card_line)
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "ambiguous"
    assert match.candidates[0].transaction_id == card_line.id


def test_linked_status_requires_reciprocal_pointer_not_one_sided() -> None:
    """A `linked_transaction_id` that resolves to a real, same-household
    `reconciliation` row on the right account type is still not proof of a
    link unless that row points back. Go-live manual slice 3 review round
    (2026-09-07, `BLOQUEIO DE MERGE`, "`paid` precisa significar quitação
    comprovada, não apenas ponteiro não nulo") applies symmetrically here:
    a one-sided pointer is corruption, not lineage, on either side of the
    pair -- `_verified_reconciliation_counterpart` is the single check
    both `list_card_payment_reconciliations` and `list_card_invoice_
    obligations` share, so this must never report `linked` any more than
    the card-side read model may report `paid` for the same shape."""

    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]
    card_line = _card_side_payment_line(household)
    db.add(card_line)
    db.flush()
    checking.linked_transaction_id = card_line.id
    # `card_line.linked_transaction_id` is intentionally left `None` -- a
    # one-sided pointer, not the symmetric pair `link_card_payment` always
    # writes on both rows together.
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "ambiguous"
    assert match.linked_transaction_id is None
    assert match.candidates[0].transaction_id == card_line.id


def test_checking_row_excluded_when_own_account_belongs_to_another_household() -> None:
    """`Transaction.household_id` and `Transaction.account_id` are
    independent columns: a checking-side row claiming `household_id == A`
    but whose `account_id` actually references an `Account` of household B
    is corrupted data, not a genuine bank debit of household A. BLOQUEIO
    DE MERGE #5 (2026-09-07): it must never enter household A's candidate
    universe (and so can never be offered as evidence, matched, or leak
    its foreign account's name) -- `Transaction.household_id` alone is not
    sufficient proof of which household a row's account belongs to."""

    db = _memory_session()
    household_a = build_synthetic_household(db, name="Família A Conta Cruzada")
    household_b = build_synthetic_household(db, name="Família B Conta Cruzada")

    corrupted = Transaction(
        household_id=household_a.household.id,
        account_id=household_b.checking.id,
        booked_at=date(2026, 6, 20),
        occurred_at=date(2026, 6, 20),
        competence="2026-06",
        description="Débito fatura conta cruzada",
        normalized_description="DEBITO FATURA CONTA CRUZADA",
        amount=Decimal("-330.00"),
        transaction_type="reconciliation",
        fingerprint=f"chkcrosshh{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=70,
        confidence=Decimal("1"),
        excluded=True,
    )
    db.add(corrupted)
    db.flush()

    matches = list_card_payment_reconciliations(db, household_id=household_a.household.id)
    assert corrupted.id not in {match.checking_transaction_id for match in matches}


def test_checking_row_stays_unlinked_when_card_counterpart_account_belongs_to_another_household() -> None:
    """A `linked_transaction_id` reciprocally pointing at a real,
    same-household (`Transaction.household_id`), right-type, right-
    direction `reconciliation` row is still not proof of a link if *that
    counterpart's own account* belongs to a different household. BLOQUEIO
    DE MERGE #5 (2026-09-07): `counterpart.household_id == household_id`
    alone does not prove `counterpart.account.household_id` does too --
    `db.get` by primary key bypasses any household-scoped query filter, so
    `_verified_reconciliation_counterpart` must check the counterpart's
    account household explicitly. This must fail closed to a non-`linked`
    status, exactly like an orphaned or non-reciprocal pointer."""

    db = _memory_session()
    household_a = build_synthetic_household(db, name="Família A Contraparte Cruzada")
    household_b = build_synthetic_household(db, name="Família B Contraparte Cruzada")
    checking = household_a.transactions["card_payment"]

    corrupted_card_line = Transaction(
        household_id=household_a.household.id,
        account_id=household_b.credit_card.id,
        category_id=household_a.categories["Conciliação"].id,
        booked_at=date(2026, 6, 15),
        occurred_at=date(2026, 6, 15),
        competence="2026-06",
        description="Pagamento em 15 JUN",
        normalized_description="PAGAMENTO EM 15 JUN",
        amount=Decimal("220.00"),
        transaction_type="reconciliation",
        fingerprint=f"cardpaycrosshh{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=70,
        confidence=Decimal("1"),
        excluded=True,
    )
    db.add(corrupted_card_line)
    db.flush()
    checking.linked_transaction_id = corrupted_card_line.id
    corrupted_card_line.linked_transaction_id = checking.id
    db.flush()

    match = _checking_match(db, household_a)
    assert match.status != "linked"
    assert match.linked_transaction_id is None
    assert all(candidate.transaction_id != corrupted_card_line.id for candidate in match.candidates)


def test_two_checking_debits_one_card_candidate_never_both_matched() -> None:
    """P0 regression: a single card-side payment cannot be the deterministic
    `matched` suggestion for two different bank debits at once. Each
    checking row sees exactly one candidate in isolation, but that candidate
    (the card row) is claimed by both -- globally ambiguous evidence, so
    neither may be presented as a one-click `matched` pair."""

    db = _memory_session()
    household = build_synthetic_household(db)
    # `card_payment` (the fixture's own checking row) is -220.00 on 2026-06-15.
    other_debit = _checking_side_debit(household, day=16)
    card_line = _card_side_payment_line(household)  # +220.00 on 2026-06-01
    db.add_all([other_debit, card_line])
    db.flush()

    matches = list_card_payment_reconciliations(db, household_id=household.household.id)
    checking_ids = {household.transactions["card_payment"].id, other_debit.id}
    seen = {m.checking_transaction_id: m for m in matches if m.checking_transaction_id in checking_ids}
    assert len(seen) == 2
    for match in seen.values():
        assert match.status == "ambiguous", (
            f"checking row {match.checking_transaction_id} was presented as "
            f"'{match.status}' even though its only candidate is shared with "
            "another checking row"
        )
        assert match.candidates[0].transaction_id == card_line.id
    # No auto-resolution: neither row was linked by merely listing evidence.
    assert household.transactions["card_payment"].linked_transaction_id is None
    assert other_debit.linked_transaction_id is None


def test_one_checking_multiple_card_candidates_stays_ambiguous() -> None:
    """Symmetric case (already covered by
    `test_ambiguous_multiple_candidates_never_auto_resolve`, preserved here
    under the new bipartite-degree implementation): one checking row, two
    eligible card candidates, never auto-resolved."""

    db = _memory_session()
    household = build_synthetic_household(db)
    card_line_a = _card_side_payment_line(household, day=1)
    card_line_b = _card_side_payment_line(household, day=3)
    db.add_all([card_line_a, card_line_b])
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "ambiguous"
    assert {c.transaction_id for c in match.candidates} == {card_line_a.id, card_line_b.id}


def test_multiple_independent_pairs_each_remain_matched() -> None:
    """Two checking rows and two card rows, each pair unambiguous (distinct
    amounts so the graph has no cross-edges): both pairs must still surface
    as deterministic `matched`, proving the one-to-one fix does not
    over-flag unrelated pairs as ambiguous."""

    db = _memory_session()
    household = build_synthetic_household(db)
    other_debit = _checking_side_debit(household, day=20, amount="-75.50", suffix="b")
    card_for_fixture = _card_side_payment_line(household, day=1, amount="220.00")
    card_for_other = _card_side_payment_line(household, day=5, amount="75.50")
    db.add_all([other_debit, card_for_fixture, card_for_other])
    db.flush()

    matches = {
        m.checking_transaction_id: m
        for m in list_card_payment_reconciliations(db, household_id=household.household.id)
    }
    fixture_match = matches[household.transactions["card_payment"].id]
    other_match = matches[other_debit.id]
    assert fixture_match.status == "matched"
    assert fixture_match.candidates[0].transaction_id == card_for_fixture.id
    assert other_match.status == "matched"
    assert other_match.candidates[0].transaction_id == card_for_other.id


def test_period_filter_does_not_hide_cross_period_ambiguity() -> None:
    """The candidate graph must be built household-wide, not scoped to the
    queried `period` -- otherwise a conflicting checking row that happens to
    fall in a different competence than the one being displayed would be
    invisible to the cardinality check, and the displayed row would be
    mislabeled `matched`."""

    db = _memory_session()
    household = build_synthetic_household(db)
    # Fixture's own row is competence 2026-06. Put the conflicting debit in
    # July, within the match window but a different competence/period.
    other_debit = _checking_side_debit(household, day=1)
    other_debit.competence = "2026-07"
    card_line = _card_side_payment_line(household)
    db.add_all([other_debit, card_line])
    db.flush()

    # Query scoped to the fixture row's own period only.
    matches = list_card_payment_reconciliations(db, household_id=household.household.id, period="2026-06")
    assert len(matches) == 1
    match = matches[0]
    assert match.checking_transaction_id == household.transactions["card_payment"].id
    assert match.status == "ambiguous", (
        "period filter hid the July checking row that also claims this same "
        "card candidate, so the June row was wrongly reported as matched"
    )


def test_link_is_symmetric_non_destructive_and_preserves_inv002() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    card_line = _card_side_payment_line(household)
    db.add(card_line)
    db.flush()
    checking = household.transactions["card_payment"]

    before = {
        "checking_amount": checking.amount,
        "checking_type": checking.transaction_type,
        "checking_excluded": checking.excluded,
        "card_amount": card_line.amount,
        "card_type": card_line.transaction_type,
        "card_excluded": card_line.excluded,
    }

    linked_checking, linked_card = link_card_payment(
        db,
        household_id=household.household.id,
        checking_transaction_id=checking.id,
        card_transaction_id=card_line.id,
    )
    db.flush()

    # Only the link column moved -- every financial fact INV-002 depends on
    # is byte-for-byte identical before and after.
    assert linked_checking.amount == before["checking_amount"]
    assert linked_checking.transaction_type == before["checking_type"]
    assert linked_checking.excluded == before["checking_excluded"]
    assert linked_card.amount == before["card_amount"]
    assert linked_card.transaction_type == before["card_type"]
    assert linked_card.excluded == before["card_excluded"]

    assert linked_checking.linked_transaction_id == card_line.id
    assert linked_card.linked_transaction_id == checking.id

    match = _checking_match(db, household)
    assert match.status == "linked"
    assert match.linked_transaction_id == card_line.id

    # Unlink reverses the link and nothing else.
    unlinked_transaction, unlinked_counterpart = unlink_card_payment(
        db, household_id=household.household.id, transaction_id=checking.id
    )
    db.flush()
    assert unlinked_transaction.linked_transaction_id is None
    assert unlinked_counterpart.linked_transaction_id is None
    assert unlinked_transaction.amount == before["checking_amount"]
    assert unlinked_counterpart.amount == before["card_amount"]
    match_after_unlink = _checking_match(db, household)
    assert match_after_unlink.status == "matched"


def test_positive_checking_reconciliation_never_matches_positive_card_payment() -> None:
    """P0 regression: matching on absolute value alone would let a positive
    checking-side reconciliation row (not a bank debit) pair with a positive
    card-side payment row. The documented relationship (`docs/ARCHITECTURE.md`,
    "Conciliação visual de pagamento de fatura") requires a negative bank
    debit on the checking side; a positive checking row must never surface
    as a candidate, and a human confirm attempt must be rejected too."""

    db = _memory_session()
    household = build_synthetic_household(db)
    positive_checking = _checking_side_debit(household, day=16, amount="220.00", suffix="pos")
    card_line = _card_side_payment_line(household)  # +220.00
    db.add_all([positive_checking, card_line])
    db.flush()

    matches = list_card_payment_reconciliations(db, household_id=household.household.id)
    match = next(m for m in matches if m.checking_transaction_id == positive_checking.id)
    assert match.status == "unmatched"
    assert match.candidates == ()

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=positive_checking.id,
            card_transaction_id=card_line.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass
    assert positive_checking.linked_transaction_id is None
    assert card_line.linked_transaction_id is None


def test_negative_card_reconciliation_never_matches_negative_checking_debit() -> None:
    """Symmetric P0 case: a negative card-side reconciliation row (not a
    payment-received line) must never pair with a negative checking debit
    even though the absolute values and account types line up."""

    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]  # -220.00
    negative_card_line = _card_side_payment_line(household, amount="-220.00")
    db.add(negative_card_line)
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "unmatched"
    assert match.candidates == ()

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=checking.id,
            card_transaction_id=negative_card_line.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass
    assert checking.linked_transaction_id is None
    assert negative_card_line.linked_transaction_id is None


def test_valid_negative_checking_positive_card_direction_still_accepted() -> None:
    """Control case: the documented valid direction (negative checking debit,
    positive card payment) must remain unaffected by the direction check."""

    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]  # -220.00
    card_line = _card_side_payment_line(household)  # +220.00
    db.add(card_line)
    db.flush()

    match = _checking_match(db, household)
    assert match.status == "matched"
    assert match.candidates[0].transaction_id == card_line.id

    linked_checking, linked_card = link_card_payment(
        db,
        household_id=household.household.id,
        checking_transaction_id=checking.id,
        card_transaction_id=card_line.id,
    )
    assert linked_checking.linked_transaction_id == card_line.id
    assert linked_card.linked_transaction_id == checking.id


def test_link_rejects_amount_mismatch() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    mismatched = _card_side_payment_line(household, amount="999.00")
    db.add(mismatched)
    db.flush()
    checking = household.transactions["card_payment"]

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=checking.id,
            card_transaction_id=mismatched.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass
    assert checking.linked_transaction_id is None


def test_link_rejects_same_account_type_pair() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]
    other_checking = Transaction(
        household_id=household.household.id,
        account_id=household.checking.id,
        booked_at=date(2026, 6, 15),
        occurred_at=date(2026, 6, 15),
        competence="2026-06",
        description="Outro débito",
        normalized_description="OUTRO DEBITO",
        amount=Decimal("-220.00"),
        transaction_type="reconciliation",
        fingerprint=f"other{uuid.uuid4().hex}".ljust(64, "0")[:64],
        source_priority=70,
        confidence=Decimal("1"),
        excluded=True,
    )
    db.add(other_checking)
    db.flush()

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=checking.id,
            card_transaction_id=other_checking.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass


def test_link_rejects_non_reconciliation_transaction() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    checking = household.transactions["card_payment"]
    purchase = household.transactions["card_purchase"]  # transaction_type="expense"

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=checking.id,
            card_transaction_id=purchase.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass


def test_link_rejects_already_linked_pair() -> None:
    db = _memory_session()
    household = build_synthetic_household(db)
    card_line = _card_side_payment_line(household)
    second_card_line = _card_side_payment_line(household, day=2)
    db.add_all([card_line, second_card_line])
    db.flush()
    checking = household.transactions["card_payment"]

    link_card_payment(
        db,
        household_id=household.household.id,
        checking_transaction_id=checking.id,
        card_transaction_id=card_line.id,
    )
    db.flush()

    try:
        link_card_payment(
            db,
            household_id=household.household.id,
            checking_transaction_id=checking.id,
            card_transaction_id=second_card_line.id,
        )
        raise AssertionError("expected CardPaymentLinkError")
    except CardPaymentLinkError:
        pass


def test_household_isolation_never_offers_another_households_rows() -> None:
    db = _memory_session()
    household_a = build_synthetic_household(db, name="Família A")
    household_b = build_synthetic_household(db, name="Família B")
    # Same amount/date shape in household B must never leak as a candidate
    # for household A's checking-side row.
    other_card_line = _card_side_payment_line(household_b)
    db.add(other_card_line)
    db.flush()

    match = _checking_match(db, household_a)
    assert match.status == "unmatched"

    try:
        link_card_payment(
            db,
            household_id=household_a.household.id,
            checking_transaction_id=household_a.transactions["card_payment"].id,
            card_transaction_id=other_card_line.id,
        )
        raise AssertionError("expected LookupError across households")
    except LookupError:
        pass


def _create_household_admin(session_factory, *, household_name: str, username: str, password: str) -> None:
    with session_factory() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Admin",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()


def test_http_endpoints_authorize_isolate_and_audit() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    test_session_factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=test_engine)

    def _override_get_db():
        db = test_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            # No session cookie yet: every route below must reject.
            assert client.get("/api/card-payment-reconciliations").status_code == 401

            setup = client.post(
                "/api/auth/setup",
                json={
                    "household_name": "Família Cartão",
                    "name": "Admin",
                    "username": "admin-card",
                    "password": "senha-local-segura",
                },
            )
            assert setup.status_code == 201

            with test_session_factory() as db:
                # Minimal rows directly under the household /api/auth/setup
                # just created, so the logged-in session actually owns them
                # (household isolation is enforced by household_id, not by
                # object identity, so the synthetic fixture's convenience
                # helper isn't needed here -- and reusing it would collide
                # with the category names /api/auth/setup already seeded).
                real_household_id = db.scalar(select(User.household_id).where(User.username == "admin-card"))
                checking_account = Account(
                    household_id=real_household_id, name="Conta Corrente", account_type="checking"
                )
                credit_card_account = Account(
                    household_id=real_household_id, name="Cartão de Crédito", account_type="credit_card"
                )
                db.add_all([checking_account, credit_card_account])
                db.flush()
                checking_txn = Transaction(
                    household_id=real_household_id,
                    account_id=checking_account.id,
                    booked_at=date(2026, 6, 15),
                    occurred_at=date(2026, 6, 15),
                    competence="2026-06",
                    description="Pagamento da fatura",
                    normalized_description="PAGAMENTO DA FATURA",
                    amount=Decimal("-220.00"),
                    transaction_type="reconciliation",
                    fingerprint=f"httpcheck{uuid.uuid4().hex}".ljust(64, "0")[:64],
                    source_priority=70,
                    confidence=Decimal("1"),
                    excluded=True,
                )
                card_txn = Transaction(
                    household_id=real_household_id,
                    account_id=credit_card_account.id,
                    booked_at=date(2026, 6, 1),
                    occurred_at=date(2026, 6, 1),
                    competence="2026-06",
                    description="Pagamento em 15 JUN",
                    normalized_description="PAGAMENTO EM 15 JUN",
                    amount=Decimal("220.00"),
                    transaction_type="reconciliation",
                    fingerprint=f"httpcard{uuid.uuid4().hex}".ljust(64, "0")[:64],
                    source_priority=70,
                    confidence=Decimal("1"),
                    excluded=True,
                )
                db.add_all([checking_txn, card_txn])
                db.flush()
                checking_id = checking_txn.id
                card_line_id = card_txn.id
                db.commit()

            listing = client.get("/api/card-payment-reconciliations")
            assert listing.status_code == 200
            match = next(item for item in listing.json() if item["checking_transaction"]["id"] == checking_id)
            assert match["status"] == "matched"
            assert match["candidates"][0]["transaction_id"] == card_line_id

            link = client.post(
                "/api/card-payment-reconciliations/link",
                json={
                    "checking_transaction_id": checking_id,
                    "card_transaction_id": card_line_id,
                    "reason": "Conferido com o extrato",
                },
            )
            assert link.status_code == 201
            assert link.json()["status"] == "linked"

            duplicate_link = client.post(
                "/api/card-payment-reconciliations/link",
                json={
                    "checking_transaction_id": checking_id,
                    "card_transaction_id": card_line_id,
                    "reason": "Segunda tentativa",
                },
            )
            assert duplicate_link.status_code == 409

            with test_session_factory() as db:
                events = db.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == "card_payment_reconciliation.link")
                ).all()
                assert len(events) == 1
                assert events[0].reason == "Conferido com o extrato"
                assert events[0].source == "card_payment_reconciliation"

            # Cross-household isolation.
            _create_household_admin(
                test_session_factory,
                household_name="Outra Família",
                username="admin-outra-card",
                password="outra-senha-segura",
            )
            with TestClient(app) as other_client:
                other_login = other_client.post(
                    "/api/auth/login",
                    json={"username": "admin-outra-card", "password": "outra-senha-segura"},
                )
                assert other_login.status_code == 200
                other_listing = other_client.get("/api/card-payment-reconciliations")
                assert other_listing.status_code == 200
                assert other_listing.json() == []
                forbidden_unlink = other_client.post(
                    "/api/card-payment-reconciliations/unlink",
                    json={"transaction_id": checking_id, "reason": "tentativa indevida"},
                )
                assert forbidden_unlink.status_code == 404

            unlink = client.post(
                "/api/card-payment-reconciliations/unlink",
                json={"transaction_id": checking_id, "reason": "Reversão de teste"},
            )
            assert unlink.status_code == 200
            assert unlink.json()["status"] == "matched"

            with test_session_factory() as db:
                checking_row = db.get(Transaction, checking_id)
                card_row = db.get(Transaction, card_line_id)
                assert checking_row.linked_transaction_id is None
                assert card_row.linked_transaction_id is None
                # Non-destructive: both source rows still exist unchanged.
                assert checking_row.amount == Decimal("-220.00")
                assert card_row.amount == Decimal("220.00")
    finally:
        app.dependency_overrides.pop(get_db, None)
