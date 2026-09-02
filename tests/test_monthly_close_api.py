"""HTTP-level tests for PR 7: finding lifecycle endpoints and monthly close.

Auth wiring, household isolation, `reason` validation and the interaction
between a real INV-014 finding and the monthly-close trust gate are all
exercised end to end here, over the actual FastAPI app. The precise trust
gate arithmetic (which combinations of status/snapshot flags allow
`trusted`) is unit-tested in isolation in `tests/test_monthly_close.py`.

This module gives itself a fully isolated database via a FastAPI
`dependency_overrides` on `get_db`, instead of relying on `app.db.engine`
(a process-wide singleton created once from whichever `DATABASE_URL` the
*first*-imported test module set -- `tests/test_api.py` also builds a real
`TestClient(app)`, and Python's module cache means both files would
otherwise silently share one engine/database).
"""

import os
import uuid
from datetime import date
from decimal import Decimal

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "monthly-close-api-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-monthly-close-api-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Household, Transaction, User  # noqa: E402
from app.security import hash_password  # noqa: E402

PERIOD = "2026-08"

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _create_household_admin(*, household_name: str, username: str, password: str) -> None:
    with _TestSessionLocal() as db:
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


def test_monthly_close_lifecycle_and_finding_actions() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            setup = client.post(
                "/api/auth/setup",
                json={
                    "household_name": "Família Fechamento",
                    "name": "Admin",
                    "username": "admin-close",
                    "password": "senha-local-segura",
                },
            )
            assert setup.status_code == 201

            # No close yet: GET synthesizes a default without persisting anything.
            empty_state = client.get(f"/api/monthly-closes/{PERIOD}")
            assert empty_state.status_code == 200
            assert empty_state.json()["status"] == "open"
            assert empty_state.json()["id"] is None
            assert empty_state.json()["pending"]["eligible_for_trust"] is False

            assert client.get("/api/monthly-closes/2026-8").status_code == 422

            member = client.post(
                "/api/users",
                json={
                    "name": "Kelly",
                    "username": "kelly-close",
                    "password": "senha-kelly-segura",
                    "is_admin": False,
                },
            )
            assert member.status_code == 201
            with TestClient(app) as member_client:
                login = member_client.post(
                    "/api/auth/login",
                    json={"username": "kelly-close", "password": "senha-kelly-segura"},
                )
                assert login.status_code == 200
                assert member_client.post(f"/api/monthly-closes/{PERIOD}/run").status_code == 403
                assert member_client.post(f"/api/monthly-closes/{PERIOD}/trust").status_code == 403
                assert member_client.post(
                    f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "não autorizado"}
                ).status_code == 403

            account = client.post(
                "/api/accounts",
                json={
                    "name": "Nubank",
                    "institution": "Nubank",
                    "account_type": "credit_card",
                    "owner_label": "Família",
                    "last_four": "1234",
                },
            )
            assert account.status_code == 201
            with _TestSessionLocal() as db:
                household = db.scalar(select(Household).where(Household.name == "Família Fechamento"))
                duplicate = Transaction(
                    household_id=household.id,
                    account_id=account.json()["id"],
                    booked_at=date(2026, 8, 15),
                    description="Compra suspeita",
                    normalized_description="COMPRA SUSPEITA",
                    amount=Decimal("-10.00"),
                    transaction_type="expense",
                    owner_label="Família",
                    fingerprint="f" * 64,
                    possible_duplicate=True,
                    excluded=False,
                    reviewed=False,
                )
                db.add(duplicate)
                db.commit()
                duplicate_id = duplicate.id

            run_response = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert run_response.status_code == 200
            run_payload = run_response.json()
            assert run_payload["status"] == "review_required"
            assert run_payload["snapshot_id"]
            assert run_payload["integrity_run_id"]
            assert run_payload["pending"]["integrity_status"] == "critical"
            assert run_payload["pending"]["eligible_for_trust"] is False

            assert client.get(f"/api/monthly-closes/{PERIOD}").json()["status"] == "review_required"

            blocked_trust = client.post(f"/api/monthly-closes/{PERIOD}/trust")
            assert blocked_trust.status_code == 422
            detail = blocked_trust.json()["detail"]
            assert any("critical" in reason for reason in detail["reasons"])

            findings = client.get(
                "/api/integrity/findings",
                params={"status": "open", "invariant_id": "INV-014", "period": PERIOD},
            ).json()
            assert findings["total"] == 1
            finding_id = findings["items"][0]["id"]

            assert client.post(
                f"/api/integrity/findings/{finding_id}/acknowledge", json={}
            ).status_code == 422

            acknowledge = client.post(
                f"/api/integrity/findings/{finding_id}/acknowledge",
                json={"reason": "Estou revisando com a Kelly"},
            )
            assert acknowledge.status_code == 200
            assert acknowledge.json()["status"] == "acknowledged"
            assert acknowledge.json()["acknowledgement_reason"] == "Estou revisando com a Kelly"

            resolve = client.post(
                f"/api/integrity/findings/{finding_id}/resolve",
                json={"reason": "Confirmado como lançamento legítimo e único"},
            )
            assert resolve.status_code == 200
            assert resolve.json()["status"] == "resolved"

            assert client.post(
                f"/api/integrity/findings/{finding_id}/resolve", json={"reason": "segunda tentativa"}
            ).status_code == 409

            with _TestSessionLocal() as db:
                transaction = db.get(Transaction, duplicate_id)
                # `resolve` never touched the underlying financial fact.
                assert transaction.possible_duplicate is True
                assert transaction.excluded is False
                assert transaction.amount == Decimal("-10.00")

            # Resolving the finding doesn't retroactively rewrite the already
            # completed run's stored summary -- trust must still fail.
            assert client.post(f"/api/monthly-closes/{PERIOD}/trust").status_code == 422

            # Cross-household isolation: a second, unrelated household cannot
            # see or act on this household's finding or monthly close.
            _create_household_admin(
                household_name="Outra Família", username="admin-outro", password="outra-senha-segura"
            )
            with TestClient(app) as other_client:
                other_login = other_client.post(
                    "/api/auth/login",
                    json={"username": "admin-outro", "password": "outra-senha-segura"},
                )
                assert other_login.status_code == 200
                assert other_client.get(f"/api/integrity/findings/{finding_id}").status_code == 404
                assert other_client.post(
                    f"/api/integrity/findings/{finding_id}/resolve", json={"reason": "tentativa indevida"}
                ).status_code == 404
                assert other_client.get(f"/api/monthly-closes/{PERIOD}").json()["status"] == "open"
                assert other_client.post(
                    f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "tentativa indevida"}
                ).status_code == 409

            # The duplicate transaction is still flagged (`resolve` never
            # cleared `possible_duplicate`), so a fresh run reopens the same
            # finding (reincidence) instead of silently staying "resolved"
            # forever.
            rerun = client.post(f"/api/monthly-closes/{PERIOD}/run")
            assert rerun.status_code == 200
            assert rerun.json()["pending"]["open_findings"] >= 1
            reopened_finding = client.get(f"/api/integrity/findings/{finding_id}").json()
            assert reopened_finding["status"] == "open"
            assert reopened_finding["occurrence_count"] >= 2

            assert client.post(
                f"/api/monthly-closes/{PERIOD}/reopen", json={"reason": "ainda não fechado"}
            ).status_code == 409
    finally:
        app.dependency_overrides.pop(get_db, None)
