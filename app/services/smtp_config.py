"""SMTP sender configuration stored via the Settings UI (MAIL-04,
`docs/WORK_ORDER_MAIL_04_UI_SMTP_CONFIG.md`, issue #110).

Owns two things, and nothing else:

1. Encrypting/decrypting the App Password at rest with a Fernet key
   exclusive to this module (`settings.smtp_encryption_key`) -- never
   `SECRET_KEY`, `FILE_ENCRYPTION_KEY`, `MFA_ENCRYPTION_KEY`, or
   `WHATSAPP_PHONE_ENCRYPTION_KEY`. Same "fail explicitly, never fall back
   to plaintext" discipline as `app.services.mfa`.
2. Resolving the *effective* SMTP configuration -- an enabled, complete
   `SmtpSenderConfig` row in the DB, or otherwise the `ALERT_SMTP_*` env
   vars on `app.config.Settings` -- as the single place that decision is
   made, so `POST /notification-settings/test-email` and
   `app.cli.notification_worker` can never disagree about which
   credential is "the" configured one.

Never decides *who* gets alerted or *when* (that is MAIL-02's
`app.services.notification_scheduler`) and never sends anything itself
(that is `app.services.email_delivery.SmtpEmailAdapter`, the one and only
send engine this module feeds a resolved config into).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import SmtpSenderConfig, User

# Fixed primary key, not `app.models.new_id()`: exactly one row must ever
# exist, the same "primary key itself is the concurrency barrier" idiom
# `app.services.notification_worker_status._SINGLETON_ID` already uses for
# `NotificationWorkerHeartbeat`.
_SINGLETON_ID = "smtp_sender_config"


class SmtpConfigurationError(RuntimeError):
    """Raised when `SMTP_ENCRYPTION_KEY` is missing/invalid while
    encrypting or decrypting a DB-stored App Password.

    Never caught to fall back to plaintext storage or to silently drop
    back to the env config from inside the crypto layer -- a caller that
    wants "no DB config configured" to look like "use env instead" makes
    that decision explicitly in `resolve_effective_smtp_settings`, not by
    swallowing this exception.
    """


class SmtpSenderConfigValidationError(ValueError):
    """Raised by `save_smtp_sender_config` when the submitted combination
    of fields cannot be persisted as requested (e.g. both replacing and
    removing the App Password in the same call, or enabling sending
    without every field a real send needs). `app.api` maps this to a 422,
    the same shape as every other `*Error` from a service module here."""


def _cipher(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    key = settings.smtp_encryption_key
    if not key:
        raise SmtpConfigurationError(
            "SMTP_ENCRYPTION_KEY não configurada neste ambiente -- defina-a em .env para "
            "usar a configuração de SMTP pela interface"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SmtpConfigurationError("SMTP_ENCRYPTION_KEY inválida") from exc


def encrypt_app_password(app_password: str, *, settings: Settings | None = None) -> str:
    return _cipher(settings).encrypt(app_password.encode()).decode()


def decrypt_app_password(token: str, *, settings: Settings | None = None) -> str:
    try:
        return _cipher(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Never happens from a normal application flow -- `app_password_encrypted`
        # is always written by `encrypt_app_password` in the same process
        # family sharing the same key. Distinct error (same idiom as
        # `app.services.mfa.decrypt_secret`) so a `SMTP_ENCRYPTION_KEY`
        # rotation-without-migration mistake is obvious in logs instead of
        # masquerading as an unrelated SMTP auth failure.
        raise SmtpConfigurationError("Não foi possível descriptografar a senha SMTP salva") from exc


def get_smtp_sender_config(db: Session) -> SmtpSenderConfig | None:
    """Read-only lookup. Returns `None` when no admin has ever saved a
    configuration -- callers must treat that identically to a disabled
    row (fall back to env), and must never create a row just by reading
    one, unlike the write path below."""

    return db.get(SmtpSenderConfig, _SINGLETON_ID)


def _get_or_create_for_update(db: Session) -> SmtpSenderConfig:
    row = db.get(SmtpSenderConfig, _SINGLETON_ID)
    if row is not None:
        return row
    try:
        with db.begin_nested():
            row = SmtpSenderConfig(id=_SINGLETON_ID)
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # Lost the race to a concurrent first-ever save -- same
        # begin_nested()/IntegrityError discipline as
        # `notification_worker_status._get_or_create`. The primary key
        # guarantees the other row exists now.
        row = db.get(SmtpSenderConfig, _SINGLETON_ID)
        if row is None:
            raise
        return row


@dataclass(frozen=True, slots=True)
class SmtpSenderConfigUpdate:
    enabled: bool
    host: str
    port: int
    username: str
    from_email: str
    use_starttls: bool
    timeout_seconds: int
    app_password: str | None = None  # None/empty = leave the saved secret unchanged
    remove_app_password: bool = False


def save_smtp_sender_config(
    db: Session,
    *,
    user: User,
    update: SmtpSenderConfigUpdate,
    settings: Settings | None = None,
) -> SmtpSenderConfig:
    """Applies `update` to the singleton row. Caller commits.

    Password handling (Work Order: "Edição com senha vazia preserva o
    segredo; substituição e remoção são explícitas"):
    - `remove_app_password=True` clears the secret. Mutually exclusive
      with providing `app_password` in the same call.
    - a non-empty `app_password` replaces the secret (encrypted here).
    - an empty/`None` `app_password` with `remove_app_password=False`
      leaves the previously saved secret untouched -- this is the default
      "edit host/port without retyping the App Password" path.

    Refuses to save `enabled=True` unless every field a real send needs
    (host, username, from_email, and an App Password -- new or
    previously saved) is actually present, so the row can never claim to
    be "enabled" while `resolve_effective_smtp_settings` would in fact
    fall back to env underneath it.
    """

    if update.app_password and update.remove_app_password:
        raise SmtpSenderConfigValidationError(
            "Não é possível substituir e remover a senha SMTP na mesma requisição"
        )

    row = _get_or_create_for_update(db)

    if update.remove_app_password:
        password_action = "remove"
    elif update.app_password:
        password_action = "replace"
    else:
        password_action = "keep"

    has_password_after_save = (
        password_action == "replace"
        or (password_action == "keep" and row.app_password_encrypted is not None)
    )

    if update.enabled:
        missing = [
            label
            for label, value in (("host", update.host), ("username", update.username), ("from_email", update.from_email))
            if not value
        ]
        if not has_password_after_save:
            missing.append("app_password")
        if missing:
            raise SmtpSenderConfigValidationError(
                "Configuração incompleta para habilitar o envio por SMTP: faltando " + ", ".join(missing)
            )

    row.enabled = update.enabled
    row.host = update.host
    row.port = update.port
    row.username = update.username
    row.from_email = update.from_email
    row.use_starttls = update.use_starttls
    row.timeout_seconds = update.timeout_seconds
    row.updated_by_user_id = user.id

    if password_action == "remove":
        row.app_password_encrypted = None
    elif password_action == "replace":
        row.app_password_encrypted = encrypt_app_password(update.app_password, settings=settings)
    # "keep": row.app_password_encrypted stays as-is.

    db.flush()
    return row


def serialize_smtp_sender_config(row: SmtpSenderConfig | None) -> dict:
    """Sanitized projection for the admin API/UI -- never the encrypted or
    decrypted App Password, only whether one is currently saved."""

    if row is None:
        return {
            "enabled": False,
            "host": "",
            "port": 587,
            "username": "",
            "from_email": "",
            "use_starttls": True,
            "timeout_seconds": 15,
            "app_password_configured": False,
            "updated_at": None,
        }
    return {
        "enabled": row.enabled,
        "host": row.host or "",
        "port": row.port,
        "username": row.username or "",
        "from_email": row.from_email or "",
        "use_starttls": row.use_starttls,
        "timeout_seconds": row.timeout_seconds,
        "app_password_configured": bool(row.app_password_encrypted),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@dataclass(frozen=True, slots=True)
class EffectiveSmtpSettings:
    """Structurally satisfies `app.services.email_delivery.SmtpSettingsLike`
    -- `SmtpEmailAdapter(settings=...)` accepts this exactly like it
    accepts `app.config.Settings`. `source` is observability metadata
    (`"database"` or `"env"`) for status endpoints; the adapter itself
    never reads it."""

    alert_email_enabled: bool
    alert_smtp_host: str
    alert_smtp_port: int
    alert_smtp_username: str
    alert_smtp_app_password: str
    alert_email_from: str
    alert_email_use_starttls: bool
    alert_email_timeout_seconds: int
    source: str


def _db_config_is_usable(row: SmtpSenderConfig | None) -> bool:
    return bool(
        row is not None
        and row.enabled
        and row.host
        and row.username
        and row.from_email
        and row.app_password_encrypted
    )


def resolve_effective_smtp_settings(db: Session, *, settings: Settings | None = None) -> EffectiveSmtpSettings:
    """The single place MAIL-04 decides DB-vs-env precedence (Work Order:
    "Resolver uma configuração SMTP efetiva compartilhada pelo teste e
    notification-worker"). Called fresh -- no module-level caching -- by
    both `POST /notification-settings/test-email` and every
    `app.cli.notification_worker.run_once()` pass, so a config change
    saved through the UI takes effect on the very next call/pass with no
    process restart.

    Precedence is all-or-nothing, never a field-by-field merge: an
    enabled DB row with every required field present (host, username,
    from_email, and an encrypted App Password) is used *entirely*;
    otherwise the `ALERT_SMTP_*` env vars are used *entirely*. Mixing a DB
    host with an env password (or vice versa) would produce a hybrid
    credential nobody asked for and nobody could audit -- the Work Order
    explicitly forbids it ("evitar misturar campos de duas fontes").
    """

    settings = settings or get_settings()
    row = get_smtp_sender_config(db)
    if _db_config_is_usable(row):
        assert row is not None and row.app_password_encrypted is not None  # narrows for type-checkers
        return EffectiveSmtpSettings(
            alert_email_enabled=True,
            alert_smtp_host=row.host or "",
            alert_smtp_port=row.port,
            alert_smtp_username=row.username or "",
            alert_smtp_app_password=decrypt_app_password(row.app_password_encrypted, settings=settings),
            alert_email_from=row.from_email or "",
            alert_email_use_starttls=row.use_starttls,
            alert_email_timeout_seconds=row.timeout_seconds,
            source="database",
        )
    return EffectiveSmtpSettings(
        alert_email_enabled=settings.alert_email_enabled,
        alert_smtp_host=settings.alert_smtp_host,
        alert_smtp_port=settings.alert_smtp_port,
        alert_smtp_username=settings.alert_smtp_username,
        alert_smtp_app_password=settings.alert_smtp_app_password,
        alert_email_from=settings.alert_email_from,
        alert_email_use_starttls=settings.alert_email_use_starttls,
        alert_email_timeout_seconds=settings.alert_email_timeout_seconds,
        source="env",
    )
