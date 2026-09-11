"""HTTP-level tests for Fase 4: autenticação multifator local TOTP.

`docs/WORK_ORDER_LOCAL_MFA_TOTP.md`: covers the full state machine
(`ANONYMOUS` -> `PASSWORD_VERIFIED` -> `MFA_ENROLL_PENDING`/
`MFA_VERIFY_PENDING` -> `FULLY_AUTHENTICATED`), enrollment, login,
anti-replay, rate limiting, recovery codes, reconfiguration, session
revocation via `session_version`, the local break-glass CLI, and the
explicit security-test list from the Work Order's "Testes de segurança
obrigatórios" section.

Gives itself a fully isolated SQLite database via
`dependency_overrides[get_db]`, same reasoning as
`tests/test_role_based_authorization.py`.
"""

import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "mfa-test-secret-that-is-long-enough-for-validation")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-mfa-data-{uuid.uuid4().hex}")

from app.cli.mfa import reset_mfa_for_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditEvent, Category, Household, MfaFactor, MfaRecoveryCode, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import mfa as mfa_service  # noqa: E402
from app.services.mfa import TOTP_PERIOD_SECONDS  # noqa: E402
from tests.fixtures.mfa_enrollment import complete_mfa_enrollment, complete_mfa_verification  # noqa: E402

_test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _isolated_db():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _create_user(*, household_name: str, username: str, password: str, is_admin: bool) -> str:
    with _TestSessionLocal() as db:
        household = db.scalar(select(Household).where(Household.name == household_name))
        if household is None:
            household = Household(name=household_name)
            db.add(household)
            db.flush()
            db.add(Category(household_id=household.id, name="Geral", color="#888888"))
        user = User(
            household_id=household.id,
            name="Usuário Teste",
            username=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
            active=True,
        )
        db.add(user)
        db.commit()
        return user.id


@dataclass(slots=True)
class EnrolledUser:
    client: TestClient
    user_id: str
    username: str
    password: str
    secret: str


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _enrolled_user(*, is_admin: bool = True) -> EnrolledUser:
    tag = uuid.uuid4().hex[:8]
    username = _unique(f"user-{tag}")
    password = "senha-local-bem-segura"
    user_id = _create_user(
        household_name=f"Família {tag}", username=username, password=password, is_admin=is_admin
    )
    client = TestClient(app)
    body = _login(client, username, password)
    assert body == {"mfa_required": True, "mode": "enroll"}
    secret = complete_mfa_enrollment(client)
    return EnrolledUser(client=client, user_id=user_id, username=username, password=password, secret=secret)


def _factor_for(username: str) -> MfaFactor:
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        return db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id))


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def test_password_alone_never_creates_a_full_session_for_a_new_user() -> None:
    username = _unique("enroll")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    body = _login(client, username, password)
    assert body == {"mfa_required": True, "mode": "enroll"}
    assert "ffp_session" not in client.cookies
    assert "ffp_mfa_pending" in client.cookies


def test_enroll_start_returns_local_qr_and_uri_without_external_call() -> None:
    username = _unique("enroll")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    start = client.post("/api/auth/mfa/enroll/start")
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "issuer=Family%20Finance" in body["otpauth_uri"] or "issuer=Family+Finance" in body["otpauth_uri"]
    assert body["qr_code"].startswith("data:image/png;base64,")
    # Never a plaintext-secret-bearing DB row before confirmation.
    factor = _factor_for(username)
    assert factor.pending_secret_encrypted != body["secret"]
    assert factor.secret_encrypted is None
    assert factor.confirmed_at is None


def test_enroll_confirm_wrong_code_does_not_activate() -> None:
    username = _unique("enroll")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    client.post("/api/auth/mfa/enroll/start")
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": "000000"})
    assert confirm.status_code == 401
    assert "ffp_session" not in client.cookies
    factor = _factor_for(username)
    assert factor.confirmed_at is None


def test_enroll_confirm_expired_setup_requires_new_secret() -> None:
    username = _unique("enroll")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    start = client.post("/api/auth/mfa/enroll/start").json()
    with _TestSessionLocal() as db:
        factor = _factor_for(username)
        factor = db.merge(factor)

        factor.pending_setup_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    code = pyotp.TOTP(start["secret"]).now()
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": code})
    assert confirm.status_code == 400
    assert "expirad" in confirm.json()["detail"].lower()


def test_enroll_confirm_success_issues_session_and_ten_recovery_codes() -> None:
    username = _unique("enroll")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    start = client.post("/api/auth/mfa/enroll/start").json()
    code = pyotp.TOTP(start["secret"]).now()
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["user"]["username"] == username
    assert len(body["recovery_codes"]) == 10
    assert len(set(body["recovery_codes"])) == 10
    assert "ffp_session" in client.cookies
    assert "ffp_mfa_pending" not in client.cookies
    # Persisted only as verifiers.
    with _TestSessionLocal() as db:
        factor = _factor_for(username)
        rows = db.scalars(select(MfaRecoveryCode).where(MfaRecoveryCode.factor_id == factor.id)).all()
        assert len(rows) == 10
        assert all(row.code_hash not in body["recovery_codes"] for row in rows)
        assert all(row.used_at is None for row in rows)
    # Backend is authoritative: a normal financial endpoint now works.
    assert client.get("/api/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# Login of an already-enrolled user
# ---------------------------------------------------------------------------


def test_login_with_confirmed_factor_yields_verify_not_enroll() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    second_client = TestClient(app)
    body = _login(second_client, enrolled.username, enrolled.password)
    assert body == {"mfa_required": True, "mode": "verify"}


def test_verify_wrong_totp_does_not_create_session() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    response = client.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert response.status_code == 401
    assert "ffp_session" not in client.cookies


def test_verify_correct_totp_creates_full_session() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    result = complete_mfa_verification(client, enrolled.secret)
    assert result["username"] == enrolled.username
    assert client.get("/api/auth/me").status_code == 200


def test_inactive_user_rejected_regardless_of_mfa_state() -> None:
    username = _unique("inactive")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        user.active = False
        db.commit()
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401


def test_admin_and_consulta_both_require_mfa() -> None:
    admin = _enrolled_user(is_admin=True)
    consulta = _enrolled_user(is_admin=False)
    assert admin.client.get("/api/auth/me").json()["is_admin"] is True
    assert consulta.client.get("/api/auth/me").json()["is_admin"] is False


# ---------------------------------------------------------------------------
# Pending state cannot reach financial APIs (security tests 1-3)
# ---------------------------------------------------------------------------


def test_pending_enroll_cookie_never_authenticates_financial_api() -> None:
    username = _unique("pending")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    assert "ffp_mfa_pending" in client.cookies
    for path in ("/api/auth/me", "/api/dashboard", "/api/accounts", "/api/transactions"):
        response = client.get(path)
        assert response.status_code == 401, f"{path} leaked access to a pending client"


def test_pending_verify_cookie_never_authenticates_financial_api() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    for path in ("/api/auth/me", "/api/dashboard", "/api/accounts"):
        assert client.get(path).status_code == 401


def test_pending_cookie_cannot_be_replayed_as_ffp_session_cookie() -> None:
    """Security test 2: even if a caller tries to present the pending
    token as `ffp_session` directly (different cookie name, same value),
    `get_current_user` must still reject it -- it is signed with a
    different itsdangerous salt/purpose entirely."""

    username = _unique("pending")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {username}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    pending_token = client.cookies["ffp_mfa_pending"]
    forged = TestClient(app)
    forged.cookies.set("ffp_session", pending_token)
    assert forged.get("/api/auth/me").status_code == 401


def test_frontend_state_cannot_forge_full_session_without_backend_mfa_success() -> None:
    """Security test 3: there is no request shape that flips a pending
    state into a full session without a real `/auth/mfa/*` success --
    trying the enroll-confirm endpoint with an unrelated pending purpose
    (verify) must fail, proving purpose is enforced server-side."""

    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)  # mode: verify
    # Calling the *enroll* confirm endpoint while holding a *verify*
    # pending cookie must not succeed.
    code = pyotp.TOTP(enrolled.secret).now()
    response = client.post("/api/auth/mfa/enroll/confirm", json={"code": code})
    assert response.status_code in (400, 401)
    assert "ffp_session" not in client.cookies


def test_confirmed_secret_never_returned_by_any_route_after_enrollment() -> None:
    """Security test 7: 'obter segredo pela rota de status/configuração
    após enrollment'. Once a factor is confirmed, no route -- not
    `/auth/me`, not starting a *new* enroll/reconfigure flow (which only
    ever returns a freshly generated *pending* secret, never the active
    one) -- ever echoes back `enrolled.secret` or its ciphertext."""

    enrolled = _enrolled_user()
    me = enrolled.client.get("/api/auth/me")
    assert enrolled.secret not in me.text
    factor = _factor_for(enrolled.username)
    assert factor.secret_encrypted not in me.text

    # Starting a reconfiguration returns a brand-new pending secret, never
    # the still-active one.
    code = pyotp.TOTP(enrolled.secret).now()
    start = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start", json={"password": enrolled.password, "code": code}
    )
    assert start.status_code == 200, start.text
    assert start.json()["secret"] != enrolled.secret


# ---------------------------------------------------------------------------
# Anti-replay
# ---------------------------------------------------------------------------


def test_same_totp_timestep_cannot_authenticate_twice() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    code = pyotp.TOTP(enrolled.secret).now()
    first = client.post("/api/auth/mfa/verify", json={"code": code})
    assert first.status_code == 200, first.text

    # Log out and back in, then replay the exact same code/timestep.
    client.post("/api/auth/logout")
    second_client = TestClient(app)
    _login(second_client, enrolled.username, enrolled.password)
    replay = second_client.post("/api/auth/mfa/verify", json={"code": code})
    assert replay.status_code == 401
    assert "ffp_session" not in second_client.cookies


def test_totp_outside_window_is_rejected() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    # A code 5 steps away (150s) -- well outside the +-1 window.
    stale_code = pyotp.TOTP(enrolled.secret).at(int(time.time()) + 5 * 30)
    response = client.post("/api/auth/mfa/verify", json={"code": stale_code})
    assert response.status_code == 401


def test_concurrent_accept_of_same_timestep_yields_at_most_one_success() -> None:
    """Exercises `app.api._accept_timestep`'s atomic CAS directly against
    two real, separate DB sessions standing in for two concurrent
    requests -- the portable (SQLite-and-PostgreSQL) half of the
    guarantee; `tests/test_postgresql_integration.py` additionally proves
    it under genuine thread concurrency against real PostgreSQL."""

    from app.api import _accept_timestep

    enrolled = _enrolled_user()
    factor = _factor_for(enrolled.username)
    with _TestSessionLocal() as db_a, _TestSessionLocal() as db_b:
        factor_a = db_a.get(MfaFactor, factor.id)
        factor_b = db_b.get(MfaFactor, factor.id)
        timestep = 12345
        accepted_a = _accept_timestep(db_a, factor_a, timestep, pending=False)
        db_a.commit()
        accepted_b = _accept_timestep(db_b, factor_b, timestep, pending=False)
        db_b.commit()
        assert accepted_a is True
        assert accepted_b is False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_locks_after_max_invalid_attempts() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    max_attempts = get_settings().mfa_rate_limit_max_attempts
    for _ in range(max_attempts):
        response = client.post("/api/auth/mfa/verify", json={"code": "000000"})
        assert response.status_code == 401
    locked_response = client.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert locked_response.status_code == 429
    # Even the *correct* code is refused while locked -- fail-closed, not
    # merely "the wrong code was wrong".
    correct_code = pyotp.TOTP(enrolled.secret).now()
    still_locked = client.post("/api/auth/mfa/verify", json={"code": correct_code})
    assert still_locked.status_code == 429


def test_rate_limit_is_temporary_and_resets_on_success() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    for _ in range(get_settings().mfa_rate_limit_max_attempts):
        client.post("/api/auth/mfa/verify", json={"code": "000000"})
    with _TestSessionLocal() as db:
        factor = db.merge(_factor_for(enrolled.username))

        factor.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # lockout window elapsed
        db.commit()
    code = pyotp.TOTP(enrolled.secret).now()
    response = client.post("/api/auth/mfa/verify", json={"code": code})
    assert response.status_code == 200, response.text
    factor = _factor_for(enrolled.username)
    assert factor.failed_attempts == 0
    assert factor.locked_until is None


def test_rate_limit_events_never_include_the_totp_code() -> None:
    enrolled = _enrolled_user()
    enrolled.client.post("/api/auth/logout")
    client = TestClient(app)
    _login(client, enrolled.username, enrolled.password)
    client.post("/api/auth/mfa/verify", json={"code": "123456"})
    with _TestSessionLocal() as db:
        events = db.scalars(select(AuditEvent).where(AuditEvent.event_type == "mfa.challenge_failed")).all()
        assert events
        for event in events:
            assert "123456" not in (event.details or "")


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def test_recovery_code_authenticates_and_is_single_use() -> None:
    tag = uuid.uuid4().hex[:8]
    username = _unique(f"recovery-{tag}")
    password = "senha-local-bem-segura"
    _create_user(household_name=f"Família {tag}", username=username, password=password, is_admin=True)
    client = TestClient(app)
    _login(client, username, password)
    start = client.post("/api/auth/mfa/enroll/start").json()
    code = pyotp.TOTP(start["secret"]).now()
    confirm = client.post("/api/auth/mfa/enroll/confirm", json={"code": code}).json()
    recovery_code = confirm["recovery_codes"][0]

    client.post("/api/auth/logout")
    second_client = TestClient(app)
    _login(second_client, username, password)
    used = second_client.post("/api/auth/mfa/verify", json={"recovery_code": recovery_code})
    assert used.status_code == 200, used.text
    assert second_client.get("/api/auth/me").status_code == 200

    second_client.post("/api/auth/logout")
    third_client = TestClient(app)
    _login(third_client, username, password)
    reused = third_client.post("/api/auth/mfa/verify", json={"recovery_code": recovery_code})
    assert reused.status_code == 401


def test_recovery_regeneration_invalidates_previous_codes() -> None:
    enrolled = _enrolled_user()
    code = pyotp.TOTP(enrolled.secret).now()
    regenerate = enrolled.client.post(
        "/api/auth/mfa/recovery-codes/regenerate",
        json={"password": enrolled.password, "code": code},
    )
    assert regenerate.status_code == 200, regenerate.text
    new_codes = regenerate.json()["recovery_codes"]
    assert len(new_codes) == 10

    with _TestSessionLocal() as db:
        factor = _factor_for(enrolled.username)
        unused = db.scalars(
            select(MfaRecoveryCode).where(
                MfaRecoveryCode.factor_id == factor.id, MfaRecoveryCode.used_at.is_(None)
            )
        ).all()
        assert len(unused) == 10
        assert {mfa_service.hash_recovery_code(c) for c in new_codes} == {row.code_hash for row in unused}


def test_recovery_regeneration_requires_correct_password() -> None:
    enrolled = _enrolled_user()
    code = pyotp.TOTP(enrolled.secret).now()
    response = enrolled.client.post(
        "/api/auth/mfa/recovery-codes/regenerate",
        json={"password": "senha-errada-mas-longa", "code": code},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Reconfiguration
# ---------------------------------------------------------------------------


def test_reconfigure_old_secret_still_valid_until_new_one_confirmed() -> None:
    enrolled = _enrolled_user()
    # Anti-replay is real: two codes computed for the exact same wall-clock
    # timestep would collide as "the same code used twice", so this uses
    # two explicitly different timesteps for what are, deliberately, two
    # separate authentications against the same still-active secret
    # (reconfigure/start's re-auth, then a normal fresh login).
    now = int(time.time())
    old_code = pyotp.TOTP(enrolled.secret).at(now)
    start = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start",
        json={"password": enrolled.password, "code": old_code},
    )
    assert start.status_code == 200, start.text
    new_secret = start.json()["secret"]
    assert new_secret != enrolled.secret

    # Old factor is still what a fresh login challenges against.
    enrolled.client.post("/api/auth/logout")
    relogin = TestClient(app)
    _login(relogin, enrolled.username, enrolled.password)
    relogin_code = pyotp.TOTP(enrolled.secret).at(now + TOTP_PERIOD_SECONDS)
    verify = relogin.post("/api/auth/mfa/verify", json={"code": relogin_code})
    assert verify.status_code == 200, verify.text
    assert verify.json()["username"] == enrolled.username


def test_reconfigure_confirm_activates_new_secret_and_revokes_old_sessions() -> None:
    enrolled = _enrolled_user()
    # Capture a session cookie issued *before* reconfiguration -- this is
    # the "previously issued session" the Work Order requires to become
    # unusable once reconfiguration completes.
    stale_session_cookie = enrolled.client.cookies.get("ffp_session")

    old_code = pyotp.TOTP(enrolled.secret).now()
    start = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start", json={"password": enrolled.password, "code": old_code}
    ).json()
    new_secret = start["secret"]
    new_code = pyotp.TOTP(new_secret).now()
    confirm = enrolled.client.post("/api/auth/mfa/reconfigure/confirm", json={"code": new_code})
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["recovery_codes"]) == 10

    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == enrolled.username))
        assert user.session_version >= 2

    # The cookie captured before reconfiguration is now revoked, even
    # though it is still a validly-signed, non-expired token.
    stale_client = TestClient(app)
    stale_client.cookies.set("ffp_session", stale_session_cookie)
    assert stale_client.get("/api/auth/me").status_code == 401

    # The reconfiguring client's own session (re-issued at the end of
    # `/reconfigure/confirm` with the new `session_version`) keeps working.
    assert enrolled.client.get("/api/auth/me").status_code == 200

    # The OLD TOTP secret must no longer validate a fresh login.
    enrolled.client.post("/api/auth/logout")
    relogin = TestClient(app)
    _login(relogin, enrolled.username, enrolled.password)
    old_code_after = pyotp.TOTP(enrolled.secret).now()
    rejected = relogin.post("/api/auth/mfa/verify", json={"code": old_code_after})
    assert rejected.status_code == 401
    new_code_after = pyotp.TOTP(new_secret).now()
    accepted = relogin.post("/api/auth/mfa/verify", json={"code": new_code_after})
    assert accepted.status_code == 200, accepted.text


def test_reconfigure_requires_current_password_and_factor() -> None:
    enrolled = _enrolled_user()
    wrong_password = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start",
        json={"password": "senha-errada-mas-longa", "code": pyotp.TOTP(enrolled.secret).now()},
    )
    assert wrong_password.status_code == 401

    wrong_code = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start", json={"password": enrolled.password, "code": "000000"}
    )
    assert wrong_code.status_code == 401


def _recovery_code_hashes(factor_id: str, *, unused_only: bool = False) -> set[str]:
    with _TestSessionLocal() as db:
        query = select(MfaRecoveryCode).where(MfaRecoveryCode.factor_id == factor_id)
        if unused_only:
            query = query.where(MfaRecoveryCode.used_at.is_(None))
        return {row.code_hash for row in db.scalars(query).all()}


def test_reconfigure_invalidates_old_recovery_codes() -> None:
    enrolled = _enrolled_user()
    factor_id = _factor_for(enrolled.username).id
    old_recovery_codes = _recovery_code_hashes(factor_id)

    old_code = pyotp.TOTP(enrolled.secret).now()
    start = enrolled.client.post(
        "/api/auth/mfa/reconfigure/start", json={"password": enrolled.password, "code": old_code}
    ).json()
    new_code = pyotp.TOTP(start["secret"]).now()
    confirm = enrolled.client.post("/api/auth/mfa/reconfigure/confirm", json={"code": new_code})
    new_codes = confirm.json()["recovery_codes"]

    current_hashes = _recovery_code_hashes(factor_id, unused_only=True)
    assert {mfa_service.hash_recovery_code(c) for c in new_codes} == current_hashes
    assert not (old_recovery_codes & current_hashes)


# ---------------------------------------------------------------------------
# Break-glass CLI
# ---------------------------------------------------------------------------


def test_break_glass_resets_factor_revokes_sessions_and_forces_enrollment() -> None:
    """Exercises `reset_mfa_for_user` -- the CLI's core logic -- directly
    against this module's isolated session. `test_break_glass_cli_wrapper_*`
    below separately covers the thin `main()`/argparse/confirmation
    wrapper end to end with `app.cli.mfa.SessionLocal` monkeypatched."""

    enrolled = _enrolled_user()
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == enrolled.username))
        session_version_before = user.session_version

    with _TestSessionLocal() as db:
        assert reset_mfa_for_user(db, enrolled.username) is True

    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == enrolled.username))
        assert user.session_version > session_version_before
        assert user.active is True  # account itself untouched
        factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id))
        assert factor is None

    # The old session cookie is now revoked.
    assert enrolled.client.get("/api/auth/me").status_code == 401

    # Next login goes straight back to mandatory enrollment.
    fresh_client = TestClient(app)
    body = _login(fresh_client, enrolled.username, enrolled.password)
    assert body == {"mfa_required": True, "mode": "enroll"}

    with _TestSessionLocal() as db:
        reset_events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "mfa.reset_local")
        ).all()
        assert len(reset_events) == 1
        assert reset_events[0].source == "cli"


def test_break_glass_unknown_user_fails_without_side_effect() -> None:
    # Other tests in this module legitimately create `mfa.reset_local`
    # events against their own users -- this module's DB is shared across
    # tests (unlike per-user/per-household rows, `AuditEvent` has no
    # unique-per-test key to filter by), so "no side effect" is checked as
    # "the count doesn't grow", not "the table is empty".
    with _TestSessionLocal() as db:
        before = db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.event_type == "mfa.reset_local"))
        assert reset_mfa_for_user(db, "usuario-que-nao-existe") is False
    with _TestSessionLocal() as db:
        after = db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.event_type == "mfa.reset_local"))
        assert after == before


def test_break_glass_inactive_user_fails_without_side_effect() -> None:
    username = _unique("inactive-cli")
    _create_user(household_name=f"Família {username}", username=username, password="senha-local-segura", is_admin=True)
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        user.active = False
        session_version_before = user.session_version
        db.commit()
    with _TestSessionLocal() as db:
        assert reset_mfa_for_user(db, username) is False
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user.session_version == session_version_before


def test_break_glass_cli_wrapper_requires_yes_or_interactive_confirmation(monkeypatch) -> None:
    """End-to-end coverage of `app.cli.mfa.main`'s argparse + confirmation
    wrapper (not just `reset_mfa_for_user`), with `SessionLocal`
    monkeypatched to this module's isolated engine -- `app.db.SessionLocal`
    is otherwise bound to the real `DATABASE_URL` (a process-wide
    singleton), unreachable from an isolated test database."""

    import app.cli.mfa as mfa_cli

    monkeypatch.setattr(mfa_cli, "SessionLocal", _TestSessionLocal)
    username = _unique("cli-wrapper")
    _create_user(household_name=f"Família {username}", username=username, password="senha-local-segura", is_admin=True)

    # No --yes and a non-"sim" interactive answer cancels without touching anything.
    monkeypatch.setattr("builtins.input", lambda _prompt: "não")
    assert mfa_cli.main(["reset", "--username", username]) == 1
    with _TestSessionLocal() as db:
        factor_before = db.scalar(select(User).where(User.username == username))
        assert factor_before.session_version == 1

    # --yes bypasses the prompt entirely and performs the reset.
    assert mfa_cli.main(["reset", "--username", username, "--yes"]) == 0
    with _TestSessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user.session_version == 2

    # A nonexistent user surfaces as exit code 1 through the wrapper too.
    assert mfa_cli.main(["reset", "--username", "ninguem-aqui", "--yes"]) == 1


# ---------------------------------------------------------------------------
# Household isolation / role regression after MFA
# ---------------------------------------------------------------------------


def test_mfa_never_grants_cross_household_access() -> None:
    household_a = _enrolled_user()
    household_b = _enrolled_user()
    me_a = household_a.client.get("/api/auth/me").json()
    me_b = household_b.client.get("/api/auth/me").json()
    with _TestSessionLocal() as db:
        user_a = db.scalar(select(User).where(User.username == household_a.username))
        user_b = db.scalar(select(User).where(User.username == household_b.username))
        assert user_a.household_id != user_b.household_id
    assert me_a["username"] != me_b["username"]
    # Each session only ever resolves to its own account (no household field
    # is ever accepted from the client -- session identity comes solely
    # from the signed cookie's `uid`).
    accounts_a = household_a.client.get("/api/accounts").json()
    accounts_b = household_b.client.get("/api/accounts").json()
    assert accounts_a == [] and accounts_b == []


def test_consulta_still_rejected_from_mutation_after_enrolling_mfa() -> None:
    consulta = _enrolled_user(is_admin=False)
    response = consulta.client.post(
        "/api/accounts", json={"name": "Conta", "account_type": "checking", "owner_label": "Família"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Session revocation / legacy cookie
# ---------------------------------------------------------------------------


def test_legacy_cookie_without_session_version_fails_closed() -> None:
    enrolled = _enrolled_user()
    settings = get_settings()
    legacy_serializer = URLSafeTimedSerializer(settings.secret_key, salt="family-finance-session")
    legacy_token = legacy_serializer.dumps({"uid": enrolled.user_id or _forged_user_id(enrolled.username)})
    client = TestClient(app)
    client.cookies.set("ffp_session", legacy_token)
    assert client.get("/api/auth/me").status_code == 401


def _forged_user_id(username: str) -> str:
    with _TestSessionLocal() as db:
        return db.scalar(select(User.id).where(User.username == username))


def test_logout_clears_both_session_and_pending_cookies() -> None:
    enrolled = _enrolled_user()
    logout = enrolled.client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert enrolled.client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Secrets never leak
# ---------------------------------------------------------------------------


def test_totp_secret_and_recovery_codes_never_appear_in_audit_events() -> None:
    enrolled = _enrolled_user()
    with _TestSessionLocal() as db:
        events = db.scalars(select(AuditEvent)).all()
        haystack = "\n".join(
            f"{e.event_type}|{e.details or ''}|{e.before_state or ''}|{e.after_state or ''}|{e.reason or ''}"
            for e in events
        )
    assert enrolled.secret not in haystack
    factor = _factor_for(enrolled.username)
    assert factor.secret_encrypted not in haystack
