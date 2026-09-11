"""Local TOTP MFA primitives (docs/WORK_ORDER_LOCAL_MFA_TOTP.md).

Deterministic, backend-only cryptography and encoding -- no network call,
no external QR/Google service, no AI involvement in any decision here.
This module never decides *who* is authenticated; `app.security` and the
`/auth/mfa/*` routes in `app.api` own that state machine and call into
these pure/near-pure helpers.

Key separation: the TOTP secret is encrypted at rest with
`settings.mfa_encryption_key`, a Fernet key exclusive to this module --
never `SECRET_KEY` (session/pending-cookie signing) or
`FILE_ENCRYPTION_KEY` (uploaded-document storage, `app.services.crypto`).
"""

import base64
import hashlib
import hmac
import io
import secrets
from datetime import UTC, datetime

import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
# RFC 6238 SHA-1: chosen for interoperability with Google Authenticator,
# Microsoft Authenticator and other widely deployed TOTP apps (Work Order
# decision 1) -- not a cryptographic weakness for this purpose, since the
# shared secret itself carries >=160 bits of entropy and every code has a
# 30s validity window enforced server-side regardless of digest.
TOTP_DIGEST = hashlib.sha1
# "Aceitar no máximo uma janela pequena de relógio (+-1 timestep)."
TOTP_WINDOW_STEPS = (0, -1, 1)
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # Crockford-like, no 0/O/1/I/L ambiguity
_RECOVERY_CODE_CHARS = 16  # 16 * log2(32) = 80 bits of entropy


class MfaConfigurationError(RuntimeError):
    """Raised when MFA_ENCRYPTION_KEY is missing/invalid.

    Never caught to fall back to plaintext storage (Work Order: "A
    implementação deve falhar explicitamente ... não cair para
    plaintext.") -- this is meant to propagate into a 500, refusing the
    enrollment/login request outright.
    """


def _cipher() -> Fernet:
    settings = get_settings()
    try:
        return Fernet(settings.mfa_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise MfaConfigurationError("MFA_ENCRYPTION_KEY ausente ou inválida") from exc


def generate_totp_secret() -> str:
    """A fresh, cryptographically random base32 secret (160 bits)."""

    return pyotp.random_base32(length=32)  # 32 base32 chars * 5 bits = 160 bits


def encrypt_secret(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Never happens from a normal application flow -- a factor row's
        # `secret_encrypted`/`pending_secret_encrypted` is always written by
        # `encrypt_secret` in the same process family sharing the same key.
        # Surfacing this distinctly (rather than as an unrelated 500) makes
        # a `MFA_ENCRYPTION_KEY` rotation-without-migration mistake obvious
        # in logs instead of masquerading as a wrong TOTP code.
        raise MfaConfigurationError("Não foi possível descriptografar o segredo MFA") from exc


def provisioning_uri(secret: str, *, username: str, issuer: str = "Family Finance") -> str:
    """Local `otpauth://` URI -- never sent to any external service."""

    return pyotp.TOTP(
        secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS, digest=TOTP_DIGEST
    ).provisioning_uri(name=username, issuer_name=issuer)


def qr_code_data_uri(uri: str) -> str:
    """Render `uri` as a PNG QR code entirely in-process and return it as a
    `data:image/png;base64,...` URI -- never uploaded to a public QR
    generator (Work Order decision 13 / security test 9)."""

    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def verify_totp(secret: str, code: str, *, now: datetime | None = None) -> int | None:
    """Return the accepted timestep index if `code` matches within the
    +-1 step window, else `None`. The caller is responsible for anti-replay
    (rejecting a timestep already recorded) and for timing-safe handling of
    the failure path (this function itself uses `pyotp`'s constant-time
    comparison internally for each candidate)."""

    if not code or not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS, digest=TOTP_DIGEST)
    moment = now or datetime.now(UTC)
    base_timestep = totp.timecode(moment)
    for offset in TOTP_WINDOW_STEPS:
        candidate = totp.at(moment, counter_offset=offset)
        if hmac.compare_digest(candidate, code):
            return base_timestep + offset
    return None


def generate_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_CHARS))
    return "-".join(raw[i : i + 4] for i in range(0, _RECOVERY_CODE_CHARS, 4))


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    return [generate_recovery_code() for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    """Keyed (HMAC) one-way hash -- looked up by direct equality (see
    `MfaRecoveryCode` docstring for why no per-row salt is needed) but still
    unusable by anyone who has database read access without also holding
    `MFA_ENCRYPTION_KEY`."""

    settings = get_settings()
    # Tolerant of how a human retypes it (dashes, spaces, lowercase) --
    # normalized to exactly the alphanumeric characters `generate_recovery_code`
    # produces before hashing, so `hash_recovery_code("abcd-efgh...")` and
    # `hash_recovery_code("ABCDEFGH...")` are the same lookup key.
    normalized = "".join(ch for ch in code.upper() if ch.isalnum())
    return hmac.new(settings.mfa_encryption_key.encode(), normalized.encode(), hashlib.sha256).hexdigest()
