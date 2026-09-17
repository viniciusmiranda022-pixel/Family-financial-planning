"""HTTP-level tests for the WA-01 admin allowlist API
(`docs/WORK_ORDER_WA_01.md`, issue #73): `GET/POST/DELETE
/api/whatsapp/authorized-numbers`, exercised over the real `app.main.app`
(session-cookie auth, full household/user context) -- the isolated webhook
gateway (`app/whatsapp_gateway_app.py`) is covered separately in
`tests/test_whatsapp_gateway.py` and only ever *reads* this same table by
`phone_hash`.
"""

import os
import uuid

import pyotp
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "whatsapp-authz-api-test-secret-that-is-long-enough")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("WHATSAPP_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-whatsapp-authz-api-data-{uuid.uuid4().hex}")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, User, WhatsAppAuthorizedNumber  # noqa: E402
from app.security import hash_password  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment  # noqa: E402


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


def _setup_admin(client, *, username="admin-whatsapp", household_name="Família WhatsApp HTTP"):
    setup = client.post(
        "/api/auth/setup",
        json={
            "household_name": household_name,
            "name": "Admin",
            "username": username,
            "password": "senha-local-segura",
        },
    )
    assert setup.status_code == 201, setup.text
    complete_mfa_enrollment(client)


def _create_member(client, session_factory, *, name="Kelly", username="kelly-whatsapp"):
    with session_factory() as db:
        admin = db.scalar(select(User).where(User.is_admin.is_(True)))
        member = User(
            household_id=admin.household_id,
            name=name,
            username=username,
            password_hash=hash_password("senha-local-segura"),
            is_admin=False,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return member.id


def test_admin_links_number_then_lists_it() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)

        created = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+55 11 99999-8888"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["phone_last4"] == "8888"

        listed = client.get("/api/whatsapp/authorized-numbers").json()
        assert len(listed) == 1
        assert listed[0]["user_id"] == member_id
        assert listed[0]["phone_last4"] == "8888"
        assert listed[0]["active"] is True
        # Never leaks the decrypted number, its hash, or its ciphertext.
        assert "phone_number" not in listed[0]
        assert "phone_encrypted" not in listed[0]
        assert "phone_hash" not in listed[0]

    with session_factory() as db:
        row = db.scalar(select(WhatsAppAuthorizedNumber))
        assert row.phone_encrypted != "5511999998888"
        assert row.phone_hash != "5511999998888"
        audit_row = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "whatsapp.authorized_number.create")
        )
        assert audit_row is not None
        assert audit_row.details is not None and "8888" in audit_row.details


def test_admin_entered_and_webhook_style_numbers_hash_identically() -> None:
    """The whole point of `normalize_phone`: an admin typing
    `"+55 11 99999-8888"` and Meta's inbound `wa_id` `"5511999998888"` must
    resolve to the exact same allowlist row."""

    from app.services import whatsapp_gateway

    assert whatsapp_gateway.hash_phone(
        whatsapp_gateway.normalize_phone("+55 11 99999-8888")
    ) == whatsapp_gateway.hash_phone(whatsapp_gateway.normalize_phone("5511999998888"))


def _login_and_enroll(client, *, username: str) -> None:
    login = client.post("/api/auth/login", json={"username": username, "password": "senha-local-segura"})
    assert login.status_code == 200, login.text
    assert login.json().get("mfa_required") is True
    start = client.post("/api/auth/mfa/enroll/start")
    assert start.status_code == 200, start.text
    code = pyotp.TOTP(start.json()["secret"]).now()
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text


def test_non_admin_cannot_manage_authorized_numbers() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)

        assert client.post("/api/auth/logout").status_code == 200
        _login_and_enroll(client, username="kelly-whatsapp")

        rejected_create = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511999998888"},
        )
        assert rejected_create.status_code == 403
        assert client.get("/api/whatsapp/authorized-numbers").status_code == 403


def test_cannot_link_same_user_twice_while_active() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)

        first = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511999998888"},
        )
        assert first.status_code == 201, first.text

        conflict = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511988887777"},
        )
        assert conflict.status_code == 409


def test_cannot_link_same_phone_to_two_users() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)
        second_member_id = _create_member(client, session_factory, name="Segundo", username="segundo")

        first = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511999998888"},
        )
        assert first.status_code == 201, first.text

        conflict = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": second_member_id, "phone_number": "+5511999998888"},
        )
        assert conflict.status_code == 409


def test_deactivate_then_relink_same_user_to_new_number() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)

        created = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511999998888"},
        )
        authorized_id = created.json()["id"]

        deactivated = client.delete(f"/api/whatsapp/authorized-numbers/{authorized_id}")
        assert deactivated.status_code == 200, deactivated.text

        listed = client.get("/api/whatsapp/authorized-numbers").json()
        assert listed[0]["active"] is False
        assert listed[0]["deactivated_at"] is not None

        relinked = client.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_id, "phone_number": "+5511988887777"},
        )
        assert relinked.status_code == 201, relinked.text


def test_invalid_phone_number_returns_422() -> None:
    client, session_factory = _client()
    with client:
        _setup_admin(client)
        member_id = _create_member(client, session_factory)

        rejected = client.post(
            "/api/whatsapp/authorized-numbers", json={"user_id": member_id, "phone_number": "123"}
        )
        assert rejected.status_code == 422, rejected.text


def test_household_isolation_cannot_link_number_for_foreign_user() -> None:
    """An admin from household B must not be able to target a `user_id`
    that belongs to household A -- the lookup is scoped to the admin's own
    `household_id`, so a foreign id is indistinguishable from a nonexistent
    one (404, never 403 -- fail-closed without revealing the id is real)."""

    client_a, factory_a = _client()
    with client_a:
        _setup_admin(client_a, username="admin-household-a", household_name="Família A")
        member_a_id = _create_member(client_a, factory_a, name="A Membro", username="membro-a")

    client_b, factory_b = _client()
    with client_b:
        _setup_admin(client_b, username="admin-household-b", household_name="Família B")

        cross_household = client_b.post(
            "/api/whatsapp/authorized-numbers",
            json={"user_id": member_a_id, "phone_number": "+5511977776666"},
        )
        assert cross_household.status_code == 404
        assert client_b.get("/api/whatsapp/authorized-numbers").json() == []
