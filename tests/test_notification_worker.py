"""End-to-end tests for MAIL-02's worker orchestration
(`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #69):
`app.cli.notification_worker.run_once`, wiring discovery -> claim ->
revalidate -> render -> send -> finalize together.

Uses a fake, in-memory adapter (never `smtplib`, never real network) --
the Work Order's "CI usa fake/in-memory SMTP adapter e nunca tenta
autenticar no Gmail real". SQLite in-memory database via a real
`sessionmaker`, matching `tests/test_async_capture_worker.py`'s approach
for its own worker-level tests.
"""

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-notification-worker-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "notification-worker-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.cli.notification_worker import run_once  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    Household,
    NotificationDelivery,
    NotificationRecipient,
    NotificationSettings,
    Obligation,
    Transaction,
)
from app.services import notification_scheduler as scheduler  # noqa: E402
from app.services import notification_worker_status as worker_status  # noqa: E402
from app.services.email_delivery import EmailDeliveryResult  # noqa: E402

TZ = "America/Sao_Paulo"


class _FakeAdapter:
    """Duck-typed stand-in for `SmtpEmailAdapter`: never touches `smtplib`
    or the network. `results` is an optional queue of canned
    `EmailDeliveryResult`s consumed one per `send()` call; once exhausted
    (or if never provided), every call succeeds."""

    def __init__(self, results: list[EmailDeliveryResult] | None = None) -> None:
        self.sent_to: list[str] = []
        self._results = list(results) if results is not None else None

    def send(self, message) -> EmailDeliveryResult:
        self.sent_to.append(message.to_email)
        if self._results:
            return self._results.pop(0)
        return EmailDeliveryResult(ok=True, message_id=f"<fake-{len(self.sent_to)}@test>")


def _session_factory():
    # `run_once` opens a fresh session per phase/claim (`with
    # session_factory() as db: ...`), so the underlying connection must be
    # shared across those calls -- a bare `sqlite:///:memory:` engine
    # would hand each new connection its own throwaway database. `StaticPool`
    # is the same fix `tests/test_notification_settings_api.py::_client`
    # already uses for the identical reason.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _household(db: Session, *, name: str = "Família") -> Household:
    household = Household(name=name)
    db.add(household)
    db.flush()
    return household


def _settings(db: Session, household_id: str, **kwargs) -> NotificationSettings:
    row = NotificationSettings(
        household_id=household_id,
        enabled=kwargs.pop("enabled", True),
        send_time_local=kwargs.pop("send_time_local", "08:00"),
        timezone=kwargs.pop("timezone", TZ),
    )
    db.add(row)
    db.flush()
    return row


def _recipient(db: Session, household_id: str, *, email: str = "vinicius@example.com", **kwargs) -> NotificationRecipient:
    row = NotificationRecipient(
        household_id=household_id,
        email=email,
        normalized_email=email.lower(),
        active=kwargs.pop("active", True),
        notify_d1=kwargs.pop("notify_d1", True),
        notify_d0=kwargs.pop("notify_d0", True),
    )
    db.add(row)
    db.flush()
    return row


def _obligation(db: Session, household_id: str, *, due_date: date, **kwargs) -> Obligation:
    row = Obligation(
        household_id=household_id,
        name=kwargs.pop("name", "Aluguel"),
        due_date=due_date,
        amount=kwargs.pop("amount", Decimal("1500.00")),
        status=kwargs.pop("status", "pending"),
        active=kwargs.pop("active", True),
    )
    db.add(row)
    db.flush()
    return row


def _utc_at(local_date: date, local_hour: int, local_minute: int = 0) -> datetime:
    return datetime(local_date.year, local_date.month, local_date.day, local_hour, local_minute, tzinfo=UTC) + timedelta(
        hours=3
    )


def test_end_to_end_pass_sends_d1_and_d0_and_audits_without_leaking_the_email_address() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id, email="destinatario-secreto@example.com")
        _obligation(db, household.id, due_date=today, name="Luz")
        _obligation(db, household.id, due_date=today + timedelta(days=1), name="Água")
        db.commit()

    adapter = _FakeAdapter()
    counts = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))

    assert counts["discovered"] == 2
    assert counts["sent"] == 2
    assert counts["failed"] == 0
    assert counts["canceled"] == 0
    assert len(adapter.sent_to) == 2

    with SessionFactory() as db:
        deliveries = db.query(NotificationDelivery).all()
        assert {row.status for row in deliveries} == {scheduler.SENT}
        assert all(row.message_id for row in deliveries)

        audit_rows = db.query(AuditEvent).filter(AuditEvent.source == "notification_worker").all()
        assert len(audit_rows) == 2
        for event in audit_rows:
            assert event.event_type == "notification_delivery.sent"
            assert event.user_id is None
            assert "destinatario-secreto@example.com" not in (event.details or "")


def test_second_pass_never_resends_already_terminal_deliveries() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    adapter = _FakeAdapter()
    first = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))
    second = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 30))

    assert first["sent"] == 1
    assert second["discovered"] == 0
    assert second["sent"] == 0
    assert len(adapter.sent_to) == 1


def test_obligation_paid_between_discovery_and_send_is_canceled_not_sent() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        obligation = _obligation(db, household.id, due_date=today)
        db.commit()
        obligation_id = obligation.id
        household_id = household.id

    # Discovery only -- mirrors an earlier worker pass that queued the
    # event while the obligation was still open.
    with SessionFactory() as db:
        scheduler.discover_due_deliveries(db, now_utc=_utc_at(today, 9, 0))
        db.commit()

    # The household pays the bill before the worker gets to send it.
    with SessionFactory() as db:
        obligation = db.get(Obligation, obligation_id)
        obligation.status = "paid"
        obligation.paid_at = today
        db.commit()

    adapter = _FakeAdapter()
    counts = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 30))

    assert counts["sent"] == 0
    assert counts["canceled"] == 1
    assert adapter.sent_to == []

    with SessionFactory() as db:
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status == scheduler.CANCELED
        assert delivery.last_error_code == scheduler.CANCEL_REASON_PAID

        # Regression: the worker never creates a financial fact and never
        # further mutates the obligation it merely observed.
        assert db.query(Transaction).count() == 0
        obligation = db.get(Obligation, obligation_id)
        assert obligation.household_id == household_id
        assert obligation.amount == Decimal("1500.00")
        assert obligation.status == "paid"


def test_recipient_deactivated_between_discovery_and_send_is_canceled() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        recipient = _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()
        recipient_id = recipient.id

    with SessionFactory() as db:
        scheduler.discover_due_deliveries(db, now_utc=_utc_at(today, 9, 0))
        db.commit()

    with SessionFactory() as db:
        recipient = db.get(NotificationRecipient, recipient_id)
        recipient.active = False
        db.commit()

    adapter = _FakeAdapter()
    counts = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 30))

    assert counts["canceled"] == 1
    with SessionFactory() as db:
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status == scheduler.CANCELED
        assert delivery.last_error_code == scheduler.CANCEL_REASON_RECIPIENT_GONE


def test_transient_failure_retries_then_succeeds_on_a_later_pass() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    failing_adapter = _FakeAdapter(results=[EmailDeliveryResult(ok=False, error_code="smtp_timeout")])
    first = run_once(session_factory=SessionFactory, adapter=failing_adapter, now_utc=_utc_at(today, 9, 0))
    assert first["retry_scheduled"] == 1
    assert first["sent"] == 0

    with SessionFactory() as db:
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status == scheduler.PENDING
        assert delivery.attempt_count == 1
        assert delivery.next_attempt_at is not None

    # Too soon -- backoff (300s) has not elapsed, nothing claimable yet.
    too_soon_adapter = _FakeAdapter()
    too_soon = run_once(
        session_factory=SessionFactory,
        adapter=too_soon_adapter,
        now_utc=_utc_at(today, 9, 0) + timedelta(seconds=120),
    )
    assert too_soon["sent"] == 0
    assert too_soon_adapter.sent_to == []

    # Backoff elapsed, and the (now-healthy) SMTP server accepts it.
    succeeding_adapter = _FakeAdapter()
    later = run_once(
        session_factory=SessionFactory,
        adapter=succeeding_adapter,
        now_utc=_utc_at(today, 9, 0) + timedelta(seconds=600),
    )
    assert later["sent"] == 1
    with SessionFactory() as db:
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status == scheduler.SENT
        assert delivery.attempt_count == 2


def test_permanent_failure_never_retries() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    adapter = _FakeAdapter(results=[EmailDeliveryResult(ok=False, error_code="smtp_auth_failed")])
    counts = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))

    assert counts["failed"] == 1
    assert counts["retry_scheduled"] == 0
    with SessionFactory() as db:
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status == scheduler.FAILED
        assert delivery.attempt_count == 1

        audit_row = db.query(AuditEvent).filter(AuditEvent.source == "notification_worker").one()
        assert "password" not in (audit_row.details or "").lower()
        assert "app_password" not in (audit_row.details or "").lower()


def test_household_isolation_worker_processes_each_household_independently() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household_a = _household(db, name="Família A")
        household_b = _household(db, name="Família B")
        _settings(db, household_a.id)
        _settings(db, household_b.id)
        _recipient(db, household_a.id, email="a@example.com")
        _recipient(db, household_b.id, email="b@example.com")
        _obligation(db, household_a.id, due_date=today, name="Conta A")
        _obligation(db, household_b.id, due_date=today, name="Conta B")
        db.commit()
        household_a_id, household_b_id = household_a.id, household_b.id

    adapter = _FakeAdapter()
    counts = run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))
    assert counts["sent"] == 2

    with SessionFactory() as db:
        deliveries = {row.household_id: row for row in db.query(NotificationDelivery).all()}
        assert set(deliveries) == {household_a_id, household_b_id}
        for household_id, delivery in deliveries.items():
            obligation = db.get(Obligation, delivery.obligation_id)
            assert obligation.household_id == household_id


# MAIL-03 (docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md, issue #70): the
# heartbeat `run_once` writes via `app.services.notification_worker_status`
# on every pass -- what `GET /notification-settings/status` (tested in
# `tests/test_notification_worker_status.py`) reads as "última execução do
# worker".
def test_run_once_records_a_successful_heartbeat() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    adapter = _FakeAdapter()
    run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))

    with SessionFactory() as db:
        heartbeat = worker_status.heartbeat_snapshot(db)
        assert heartbeat is not None
        assert heartbeat["last_run_ok"] is True
        assert heartbeat["last_started_at"] is not None
        assert heartbeat["last_finished_at"] is not None
        assert heartbeat["last_error_code"] is None
        assert heartbeat["last_counts"]["sent"] == 1


def test_run_once_records_a_failed_heartbeat_and_still_raises_when_a_pass_crashes(monkeypatch) -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(scheduler, "discover_due_deliveries", _boom)

    adapter = _FakeAdapter()
    try:
        run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))
        raise AssertionError("run_once should have propagated the exception")
    except RuntimeError:
        pass

    with SessionFactory() as db:
        heartbeat = worker_status.heartbeat_snapshot(db)
        assert heartbeat is not None
        assert heartbeat["last_run_ok"] is False
        assert heartbeat["last_error_code"] == "worker_pass_exception"

        # Regression: a crashed pass must never leave a delivery claimed
        # forever or create a financial fact.
        assert db.query(NotificationDelivery).count() == 0
        assert db.query(Transaction).count() == 0


def test_worker_error_code_from_a_failed_send_surfaces_on_the_heartbeat() -> None:
    SessionFactory = _session_factory()
    today = date(2026, 3, 10)
    with SessionFactory() as db:
        household = _household(db)
        _settings(db, household.id)
        _recipient(db, household.id)
        _obligation(db, household.id, due_date=today)
        db.commit()

    adapter = _FakeAdapter(results=[EmailDeliveryResult(ok=False, error_code="smtp_auth_failed")])
    run_once(session_factory=SessionFactory, adapter=adapter, now_utc=_utc_at(today, 9, 0))

    with SessionFactory() as db:
        heartbeat = worker_status.heartbeat_snapshot(db)
        assert heartbeat is not None
        assert heartbeat["last_run_ok"] is True
        assert heartbeat["last_error_code"] == "smtp_auth_failed"
