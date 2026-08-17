import base64
import hashlib
import hmac
import os

from fastapi import Cookie, Depends, HTTPException, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

settings = get_settings()
serializer = URLSafeTimedSerializer(settings.secret_key, salt="family-finance-session")


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


def set_session_cookie(response: Response, user_id: str) -> None:
    token = serializer.dumps({"uid": user_id})
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
    return user
