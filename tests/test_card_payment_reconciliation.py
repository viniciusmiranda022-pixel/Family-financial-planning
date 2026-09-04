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
