import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

from fastapi import Cookie, Depends, HTTPException, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

settings = get_settings()
serializer = URLSafeTimedSerializer(settings.secret_key, salt="family-finance-session")
# Fase 4 (docs/WORK_ORDER_LOCAL_MFA_TOTP.md): a distinct itsdangerous *salt*
# on the same SECRET_KEY is enough to make `ffp_mfa_pending` tokens
# cryptographically independent of `ffp_session` tokens -- that is what the
# `salt` parameter is for -- so this reuses SECRET_KEY rather than adding a
# third key. `MFA_ENCRYPTION_KEY` (see `app.services.mfa`) is reserved
# exclusively for encrypting the TOTP secret at rest, per the Work Order's
# "Separação de chaves" decision, which is about that secret, not about
# cookie signing.
_pending_serializer = URLSafeTimedSerializer(settings.secret_key, salt="family-finance-mfa-pending")

PENDING_COOKIE_NAME = "ffp_mfa_pending"
PENDING_PURPOSE_ENROLL = "mfa_enroll"
PENDING_PURPOSE_VERIFY = "mfa_verify"


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("A senha deve possuir pelo menos 10 caracteres")
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(hash_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def set_session_cookie(response: Response, user: User) -> None:
    """Issue the *complete* session cookie. Only ever called after MFA has
    fully succeeded (`app.api`'s enrollment-confirm, verify, and
    reconfiguration-confirm routes) -- never directly after password
    validation (Work Order decision 4: "Nenhuma sessão completa depois da
    senha").

    Embeds `user.session_version` so `get_current_user` can fail closed on
    any cookie issued before a reset/reconfiguration revoked it, or before
    this column existed at all.
    """

    token = serializer.dumps({"uid": user.id, "sv": user.session_version})
    response.set_cookie(
        "ffp_session",
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie("ffp_session", path="/")


def get_current_user(ffp_session: str | None = Cookie(default=None), db: Session = Depends(get_db)) -> User:
    if not ffp_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão não autenticada")
    try:
        payload = serializer.loads(ffp_session, max_age=settings.session_hours * 3600)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão expirada") from exc
    user = db.scalar(select(User).where(User.id == payload.get("uid"), User.active.is_(True)))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inválido")
    # Fase 4: a cookie signed before `session_version` existed carries no
    # "sv" key at all -- `payload.get("sv")` is `None`, which never equals
    # an integer column, so it fails closed exactly like a cookie whose
    # version was actively revoked. This is deliberate (Work Order:
    # "cookies antigos sem versão devem falhar fechados após o rollout"),
    # not a bug to "fix" by defaulting missing versions to the current one.
    if payload.get("sv") != user.session_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão revogada")
    return user


def bump_session_version(db: Session, user: User) -> None:
    """Revoke every previously issued `ffp_session` cookie for `user` by
    incrementing `session_version` server-side.

    Uses an atomic `UPDATE ... SET session_version = session_version + 1`
    (the same compare-free increment idiom as
    `app.services.capture_worker`'s counters) rather than a Python
    read-modify-write on the ORM attribute, so two concurrent revocations
    of the same user (e.g. a reconfiguration racing the local break-glass
    CLI against the same database) cannot lose one of the two increments.
    Refreshes the passed-in `user` object afterward so a caller that goes
    on to call `set_session_cookie(response, user)` in the same request
    (reconfiguration re-authenticates the *current* session immediately
    after revoking every session, including its own) embeds the new value
    instead of the stale one it loaded at the start of the request.
    """

    db.execute(update(User).where(User.id == user.id).values(session_version=User.session_version + 1))
    db.flush()
    db.refresh(user)


def set_pending_cookie(response: Response, user_id: str, purpose: str) -> None:
    """Issue the short-TTL, single-purpose pending-auth cookie used between
    password validation and completed MFA (`MFA_ENROLL_PENDING` /
    `MFA_VERIFY_PENDING` in the Work Order's state diagram).

    Deliberately a separate cookie name from `ffp_session`, not a flag
    inside it: every route that depends on `get_current_user` reads only
    `ffp_session`, so a pending cookie is structurally incapable of
    authenticating a financial API regardless of any bug in that route's
    own logic (security test 2: "usar pending cookie/token como
    `ffp_session` completo"). The payload carries only `uid` and `purpose`
    -- no role/household -- so it cannot be used to forge privilege even if
    somehow presented to the wrong endpoint.
    """

    token = _pending_serializer.dumps({"uid": user_id, "purpose": purpose})
    response.set_cookie(
        PENDING_COOKIE_NAME,
        token,
        max_age=settings.mfa_pending_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_pending_cookie(response: Response) -> None:
    response.delete_cookie(PENDING_COOKIE_NAME, path="/")


def _load_pending_user(token: str | None, db: Session, expected_purpose: str) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação pendente ausente")
    try:
        payload = _pending_serializer.loads(token, max_age=settings.mfa_pending_ttl_seconds)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação pendente expirada"
        ) from exc
    if payload.get("purpose") != expected_purpose:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação pendente inválida")
    user = db.scalar(select(User).where(User.id == payload.get("uid"), User.active.is_(True)))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inválido")
    return user


def get_enroll_pending_user(
    ffp_mfa_pending: str | None = Cookie(default=None), db: Session = Depends(get_db)
) -> User:
    return _load_pending_user(ffp_mfa_pending, db, PENDING_PURPOSE_ENROLL)


def get_verify_pending_user(
    ffp_mfa_pending: str | None = Cookie(default=None), db: Session = Depends(get_db)
) -> User:
    return _load_pending_user(ffp_mfa_pending, db, PENDING_PURPOSE_VERIFY)


def mfa_pending_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=settings.mfa_pending_ttl_seconds)
