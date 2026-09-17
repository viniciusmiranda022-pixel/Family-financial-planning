"""Tests for MAIL-03's operational status surface
(`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70):
`app.services.notification_worker_status` and
`GET /api/notification-settings/status`.

Split into a service-level section (SQLite session factory, matching
`tests/test_notification_worker.py`'s own approach) and an HTTP-level
section (real FastAPI app, matching `tests/test_notification_settings_api.py`)
-- neither duplicates `tests/test_notification_worker.py`'s own heartbeat
assertions (a successful pass, a crashed pass, a failed-send pass), which
exercise `run_once` end to end; this module tests the read side and the
API contract in isolation.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "notification-worker-status-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-notification-worker-status-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Household, NotificationDelivery, NotificationRecipient, Obligation, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import notification_worker_status as worker_status  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402

# ---------------------------------------------------------------------------
# Service-level: app.services.notification_worker_status
# ---------------------------------------------------------------------------


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _household(db: Session, *, name: str = "Família") -> Household:
    household = Household(name=name)
    db.add(household)
    db.flush()
    return household


def _obligation(db: Session, household_id: str, *, due_date, **kwargs) -> Obligation:
    row = Obligation(
        household_id=household_id,
        name=kwargs.pop("name", "Aluguel"),
        due_date=due_date,
        amount=kwargs.pop("amount", Decimal("100.00")),
        status=kwargs.pop("status", "pending"),
        active=kwargs.pop("active", True),
    )
    db.add(row)
    db.flush()
    return row


def _recipient(db: Session, household_id: str, *, email: str = "a@example.com") -> NotificationRecipient:
    row = NotificationRecipient(household_id=household_id, email=email, normalized_email=email.lower())
    db.add(row)
    db.flush()
    return row


def _delivery(db: Session, *, household_id: str, obligation_id: str, recipient_id: str, status: str, **kwargs):
    row = NotificationDelivery(
        household_id=household_id,
        obligation_id=obligation_id,
        recipient_id=recipient_id,
        obligation_due_date=kwargs.pop("obligation_due_date"),
        alert_kind=kwargs.pop("alert_kind", "d0"),
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def test_heartbeat_snapshot_is_none_before_any_worker_pass() -> None:
    SessionFactory = _session_factory()
    with SessionFactory() as db:
        assert worker_status.heartbeat_snapshot(db) is None


def test_mark_run_started_then_finished_round_trips() -> None:
    SessionFactory = _session_factory()
    started = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
    finished = started + timedelta(seconds=1)

    with SessionFactory() as db:
        worker_status.mark_run_started(db, worker_id="host:123", now_utc=started)
        db.commit()

    with SessionFactory() as db:
        # A pass "in progress" (started, not yet finished) is itself a
        # legitimate, informative state -- distinct from "never run at
        # all" (`None`, asserted by
        # `test_heartbeat_snapshot_is_none_before_any_worker_pass`).
        in_progress = worker_status.heartbeat_snapshot(db)
        assert in_progress["last_started_at"] == started.isoformat()
        assert in_progress["last_finished_at"] is None
        assert in_progress["last_run_ok"] is None

    with SessionFactory() as db:
        worker_status.mark_run_finished(
            db, ok=True, counts={"sent": 3}, error_code=None, now_utc=finished
        )
        db.commit()

    with SessionFactory() as db:
        snapshot = worker_status.heartbeat_snapshot(db)
        assert snapshot == {
            "last_started_at": started.isoformat(),
            "last_finished_at": finished.isoformat(),
            "last_run_ok": True,
            "last_error_code": None,
            "last_counts": {"sent": 3},
        }


def test_mark_run_started_is_concurrency_safe_across_two_first_passes() -> None:
    """Two worker processes racing to record the very first heartbeat must
    still end up with exactly one row (Work Order "Concorrência e
    idempotência": duas instâncias do worker rodando por erro
    operacional) -- the fixed primary key is the actual barrier, not
    "only ever call this once" application discipline."""

    SessionFactory = _session_factory()
    now = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)

    with SessionFactory() as db_a, SessionFactory() as db_b:
        worker_status.mark_run_started(db_a, worker_id="host:1", now_utc=now)
        worker_status.mark_run_started(db_b, worker_id="host:2", now_utc=now)
        db_a.commit()
        db_b.commit()

    with SessionFactory() as db:
        from app.models import NotificationWorkerHeartbeat

        assert db.query(NotificationWorkerHeartbeat).count() == 1


def test_delivery_status_counts_are_household_scoped() -> None:
    SessionFactory = _session_factory()
    today = datetime(2026, 3, 10).date()
    with SessionFactory() as db:
        household_a = _household(db, name="A")
        household_b = _household(db, name="B")
        obligation_a = _obligation(db, household_a.id, due_date=today)
        obligation_b = _obligation(db, household_b.id, due_date=today)
        recipient_a = _recipient(db, household_a.id, email="a@example.com")
        recipient_b = _recipient(db, household_b.id, email="b@example.com")
        _delivery(
            db,
            household_id=household_a.id,
            obligation_id=obligation_a.id,
            recipient_id=recipient_a.id,
            status="sent",
            obligation_due_date=today,
        )
        _delivery(
            db,
            household_id=household_a.id,
            obligation_id=obligation_a.id,
            recipient_id=recipient_a.id,
            status="failed",
            obligation_due_date=today,
            alert_kind="d1",
        )
        _delivery(
            db,
            household_id=household_b.id,
            obligation_id=obligation_b.id,
            recipient_id=recipient_b.id,
            status="sent",
            obligation_due_date=today,
        )
        db.commit()
        household_a_id, household_b_id = household_a.id, household_b.id

    with SessionFactory() as db:
        counts_a = worker_status.delivery_status_counts(db, household_id=household_a_id)
        counts_b = worker_status.delivery_status_counts(db, household_id=household_b_id)

    assert counts_a == {"pending": 0, "sending": 0, "sent": 1, "failed": 1, "canceled": 0}
    assert counts_b == {"pending": 0, "sending": 0, "sent": 1, "failed": 0, "canceled": 0}


# ---------------------------------------------------------------------------
# HTTP-level: GET /api/notification-settings/status
# ---------------------------------------------------------------------------


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), session_factory


def _setup_household(client, *, username="admin-notification-status"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Alertas Status",
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def test_status_endpoint_reports_disabled_unconfigured_and_zero_counts_by_default() -> None:
    client, _ = _client()
    with client:
        _setup_household(client)
        response = client.get("/api/notification-settings/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is False
        assert body["sender_configured"] is False
        assert body["worker"] is None
        assert body["deliveries"] == {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "canceled": 0}


def test_status_endpoint_reflects_delivery_counts_for_this_household() -> None:
    client, SessionFactory = _client()
    with client:
        _setup_household(client)
        recipient = client.post(
            "/api/notification-recipients", json={"email": "vinicius@exemplo.com"}
        ).json()

        with SessionFactory() as db:
            household_id = db.scalar(select(Household.id))
            obligation = Obligation(
                household_id=household_id,
                name="Luz",
                due_date=datetime(2026, 3, 10).date(),
                amount=Decimal("200.00"),
            )
            db.add(obligation)
            db.flush()
            db.add(
                NotificationDelivery(
                    household_id=household_id,
                    obligation_id=obligation.id,
                    recipient_id=recipient["id"],
                    obligation_due_date=obligation.due_date,
                    alert_kind="d0",
                    status="sent",
                )
            )
            db.commit()

        response = client.get("/api/notification-settings/status")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deliveries"]["sent"] == 1
        assert body["deliveries"]["failed"] == 0


def test_status_endpoint_never_leaks_another_households_deliveries() -> None:
    client, SessionFactory = _client()
    with client:
        _setup_household(client)

        # Bypass `POST /auth/setup` for the second household -- it 409s once
        # any `User` row exists anywhere in the database (single-tenant
        # bootstrap), same reasoning `tests/test_role_based_authorization.py`
        # documents for its own cross-household fixture.
        with SessionFactory() as db:
            other_household = Household(name="Outra Família")
            db.add(other_household)
            db.flush()
            db.add(
                User(
                    household_id=other_household.id,
                    name="Outro Admin",
                    username="admin-other-household-status",
                    password_hash=hash_password("senha-local-segura-2"),
                    is_admin=True,
                )
            )
            other_recipient = NotificationRecipient(
                household_id=other_household.id, email="outro@example.com", normalized_email="outro@example.com"
            )
            db.add(other_recipient)
            other_obligation = Obligation(
                household_id=other_household.id,
                name="Água",
                due_date=datetime(2026, 3, 10).date(),
                amount=Decimal("50.00"),
            )
            db.add(other_obligation)
            db.flush()
            db.add(
                NotificationDelivery(
                    household_id=other_household.id,
                    obligation_id=other_obligation.id,
                    recipient_id=other_recipient.id,
                    obligation_due_date=other_obligation.due_date,
                    alert_kind="d0",
                    status="sent",
                )
            )
            db.commit()

        response = client.get("/api/notification-settings/status")
        assert response.status_code == 200, response.text
        assert response.json()["deliveries"] == {
            "pending": 0,
            "sending": 0,
            "sent": 0,
            "failed": 0,
            "canceled": 0,
        }
