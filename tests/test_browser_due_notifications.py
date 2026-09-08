"""Backend regression tests for the data surface behind the browser
due-date notification feature (Fase 3,
`docs/WORK_ORDER_BROWSER_DUE_NOTIFICATIONS.md`).

This slice introduces **no backend change at all**: the notification UI in
`app/static/app.js` reuses `GET /api/obligations` verbatim -- the same
endpoint `app/api.py::_obligation_rows()` already backs the "Planejamento"
table and the dashboard's "Obrigações próximas do vencimento" panel, and
whose household isolation is already covered by
`tests/test_purchase_scenario_comparison.py::
test_household_isolation_obligations_do_not_leak_or_affect_other_household`.

What this file adds is the coverage that endpoint did not yet have, now that
a browser-notification surface depends on it: an explicit authentication
check (a regression here would mean unauthenticated due-date data), proof
that repeated reads never mutate any financial entity (the Work Order's
"nenhuma mutação em Transaction/Obligation/... durante leitura/notificação"
requirement), proof that a recurring obligation still surfaces exactly one
row (never duplicated per occurrence -- the notification UI would otherwise
re-notify for the same fact under different due dates), and proof that
`due_date` can never be absent (`ObligationRequest.due_date` has no default
and no `None` variant, so this canonical entity can never produce a
fabricated/unknown due date to begin with).

Uses the same isolated-engine + `TestClient` pattern as
`tests/test_purchase_scenario_comparison.py` (a `StaticPool` in-memory
SQLite engine with `dependency_overrides[get_db]`).
"""

import os
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", f"sqlite:////tmp/ffp-due-notifications-{uuid.uuid4().hex}.sqlite")
os.environ.setdefault("SECRET_KEY", "browser-due-notifications-test-secret-long-enough-value")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, Obligation, Transaction  # noqa: E402

ENDPOINT = "/api/obligations"


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


def _setup_household(client):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Família Notificações",
            "name": "Admin",
            "username": "admin-notificacoes",
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text


def _row_counts(session_factory) -> dict:
    with session_factory() as db:
        return {
            "obligations": db.scalar(select(func.count()).select_from(Obligation)),
            "transactions": db.scalar(select(func.count()).select_from(Transaction)),
            "audit_events": db.scalar(select(func.count()).select_from(AuditEvent)),
        }


def test_requires_authentication():
    client, _ = _client()
    with client:
        response = client.get(ENDPOINT)
        assert response.status_code == 401


def test_due_date_field_cannot_be_omitted_or_null():
    """`ObligationRequest.due_date: date` has no default and rejects `null`,
    so an obligation can never be created without a real due date -- the
    schema-level guarantee behind "não inferir vencimentos inexistentes"
    (Work Order item 6) for this entity."""
    client, _ = _client()
    with client:
        _setup_household(client)
        missing = client.post(
            "/api/obligations",
            json={"name": "Sem data", "amount": 100},
        )
        assert missing.status_code == 422
        null_value = client.post(
            "/api/obligations",
            json={"name": "Data nula", "due_date": None, "amount": 100},
        )
        assert null_value.status_code == 422


def test_recurring_obligation_surfaces_a_single_row_not_one_per_occurrence():
    """A recurring obligation must appear once in `GET /obligations` (its
    single next occurrence), never once per remaining occurrence -- the
    notification UI keys its dedup/display purely off this list, so a
    duplicated row here would mean a duplicated (or repeatedly re-fired)
    alert for the same underlying fact."""
    client, _ = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/obligations",
            json={
                "name": "Assinatura mensal",
                "due_date": "2026-09-10",
                "amount": 49.9,
                "recurrence_months": 1,
                "occurrence_count": 12,
            },
        )
        assert created.status_code == 201, created.text
        rows = client.get(ENDPOINT).json()
        matching = [item for item in rows if item["name"] == "Assinatura mensal"]
        assert len(matching) == 1
        assert matching[0]["next_due_date"] == "2026-09-10"


def test_reading_obligations_never_mutates_any_financial_entity():
    """Repeated reads of the exact endpoint the notification poll loop calls
    must never create/alter a `Transaction`, `Obligation` or `AuditEvent` --
    this is a pure, side-effect-free query (`app.api._obligation_rows` has no
    `db.add`/`db.commit`), so calling it many times in a row (as a
    long-lived browser tab polling every `DUE_NOTIFICATIONS_POLL_MS` would)
    must leave every financial table exactly as it was."""
    client, session_factory = _client()
    with client:
        _setup_household(client)
        created = client.post(
            "/api/obligations",
            json={"name": "Conta fixa", "due_date": "2026-09-09", "amount": 320},
        )
        assert created.status_code == 201, created.text

        before = _row_counts(session_factory)
        for _ in range(5):
            response = client.get(ENDPOINT)
            assert response.status_code == 200
        after = _row_counts(session_factory)
        assert after == before


def test_only_soon_urgent_and_overdue_levels_fall_within_the_dashboard_window():
    """The notification panel filters on `alert_level in {overdue, urgent,
    soon}` and never recomputes a day threshold -- this asserts the backend
    contract it relies on still holds: an obligation far in the future is
    classified `scheduled`, never one of the three windowed levels."""
    client, _ = _client()
    with client:
        _setup_household(client)
        far_future = client.post(
            "/api/obligations",
            json={"name": "Distante", "due_date": "2030-01-01", "amount": 10},
        )
        assert far_future.status_code == 201, far_future.text
        rows = client.get(ENDPOINT).json()
        distant = next(item for item in rows if item["name"] == "Distante")
        assert distant["alert_level"] == "scheduled"
