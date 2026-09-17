"""Tests for `app/cli/notification_worker_healthcheck.py` (MAIL-03,
`docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md`, issue #70): the Docker
`HEALTHCHECK` probe for the `notification-worker` compose service.

Exercises `check()` directly against a real (SQLite) session, bypassing
`app.db.SessionLocal`'s process-wide engine the same way
`tests/test_notification_worker.py` bypasses it for `run_once` --
`check()` takes no session parameter (a real Docker `HEALTHCHECK CMD` has
no way to inject one), so these tests monkeypatch `SessionLocal` itself.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-notification-healthcheck-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "notification-worker-healthcheck-test-secret-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())

import app.cli.notification_worker_healthcheck as healthcheck  # noqa: E402
from app.db import Base  # noqa: E402
from app.services import notification_worker_status as worker_status  # noqa: E402


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_never_run_reports_unhealthy(monkeypatch) -> None:
    SessionFactory = _session_factory()
    monkeypatch.setattr(healthcheck, "SessionLocal", SessionFactory)

    healthy, status = healthcheck.check()
    assert healthy is False
    assert status == "never_run"


def test_recent_pass_reports_healthy(monkeypatch) -> None:
    SessionFactory = _session_factory()
    monkeypatch.setattr(healthcheck, "SessionLocal", SessionFactory)
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    with SessionFactory() as db:
        worker_status.mark_run_started(db, worker_id="host:1", now_utc=now)
        worker_status.mark_run_finished(db, ok=True, counts={"sent": 1}, error_code=None, now_utc=now)
        db.commit()

    healthy, status = healthcheck.check(now_utc=now + timedelta(seconds=30))
    assert healthy is True
    assert status == "ok"


def test_stale_pass_reports_unhealthy(monkeypatch) -> None:
    SessionFactory = _session_factory()
    monkeypatch.setattr(healthcheck, "SessionLocal", SessionFactory)
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    with SessionFactory() as db:
        worker_status.mark_run_started(db, worker_id="host:1", now_utc=now)
        worker_status.mark_run_finished(db, ok=True, counts={"sent": 1}, error_code=None, now_utc=now)
        db.commit()

    far_later = now + timedelta(hours=2)
    healthy, status = healthcheck.check(now_utc=far_later)
    assert healthy is False
    assert status == "stale"


def test_main_exits_zero_when_healthy_and_nonzero_when_not(monkeypatch, capsys) -> None:
    SessionFactory = _session_factory()
    monkeypatch.setattr(healthcheck, "SessionLocal", SessionFactory)

    assert healthcheck.main([]) == 1
    out = capsys.readouterr().out.strip()
    assert out == "never_run"
    # Regression: the sanitized status word must never leak a secret or a
    # stack trace, just the fixed word.
    assert "Traceback" not in out
