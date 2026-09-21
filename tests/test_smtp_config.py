"""Unit tests for `app.services.smtp_config` (MAIL-04,
`docs/WORK_ORDER_MAIL_04_UI_SMTP_CONFIG.md`, issue #110): encryption at
rest, the singleton save/read contract, and the DB-vs-env precedence
`resolve_effective_smtp_settings` decides. HTTP-level admin-gating,
audit-trail, and cross-household-isolation tests live in
`tests/test_smtp_config_api.py` / `tests/test_role_based_authorization.py`.
"""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "smtp-config-unit-test-secret-that-is-long-enough-ok")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-smtp-config-unit-data-{uuid.uuid4().hex}")

from app.config import Settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Household, SmtpSenderConfig, User  # noqa: E402
from app.services import smtp_config as svc  # noqa: E402
from app.services.email_delivery import SmtpSettingsLike  # noqa: E402


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return factory


def _make_user(session_factory) -> User:
    db = session_factory()
    household = Household(name="Família SMTP")
    db.add(household)
    db.flush()
    user = User(
        household_id=household.id,
        name="Admin",
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        is_admin=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user


def _settings(*, smtp_key: str | None, **env_alert_overrides) -> Settings:
    base = {
        "secret_key": "x" * 32,
        "file_encryption_key": Fernet.generate_key().decode(),
        "mfa_encryption_key": Fernet.generate_key().decode(),
        "smtp_encryption_key": smtp_key or "",
    }
    base.update(env_alert_overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_encrypt_then_decrypt_round_trips() -> None:
    settings = _settings(smtp_key=Fernet.generate_key().decode())
    token = svc.encrypt_app_password("super-secret-app-password", settings=settings)
    assert token != "super-secret-app-password"
    assert svc.decrypt_app_password(token, settings=settings) == "super-secret-app-password"


def test_encrypt_without_key_raises_configuration_error_not_plaintext_fallback() -> None:
    settings = _settings(smtp_key="")
    with pytest.raises(svc.SmtpConfigurationError):
        svc.encrypt_app_password("secret", settings=settings)


def test_encrypt_with_invalid_key_raises_configuration_error() -> None:
    settings = _settings(smtp_key="not-a-valid-fernet-key")
    with pytest.raises(svc.SmtpConfigurationError):
        svc.encrypt_app_password("secret", settings=settings)


def test_decrypt_with_wrong_key_raises_configuration_error() -> None:
    encrypt_settings = _settings(smtp_key=Fernet.generate_key().decode())
    token = svc.encrypt_app_password("secret", settings=encrypt_settings)
    wrong_settings = _settings(smtp_key=Fernet.generate_key().decode())
    with pytest.raises(svc.SmtpConfigurationError):
        svc.decrypt_app_password(token, settings=wrong_settings)


# ---------------------------------------------------------------------------
# save_smtp_sender_config
# ---------------------------------------------------------------------------


def test_save_creates_singleton_row_and_encrypts_password() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    row = svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True,
            host="smtp.gmail.com",
            port=587,
            username="remetente@gmail.com",
            from_email="remetente@gmail.com",
            use_starttls=True,
            timeout_seconds=15,
            app_password="minha-app-password",
        ),
        settings=settings,
    )
    db.commit()

    assert row.id == svc._SINGLETON_ID
    assert row.app_password_encrypted is not None
    assert row.app_password_encrypted != "minha-app-password"
    assert row.updated_by_user_id == user.id

    # Saving again must reuse the same row, never create a second one.
    again = svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True,
            host="smtp.gmail.com",
            port=465,
            username="remetente@gmail.com",
            from_email="remetente@gmail.com",
            use_starttls=False,
            timeout_seconds=20,
        ),
        settings=settings,
    )
    db.commit()
    assert again.id == row.id
    assert db.query(SmtpSenderConfig).count() == 1
    # Empty/omitted app_password on this second save preserved the secret.
    assert again.app_password_encrypted == row.app_password_encrypted
    assert again.port == 465


def test_empty_app_password_on_edit_preserves_existing_secret() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True, host="h", port=587, username="u", from_email="f@example.com",
            use_starttls=True, timeout_seconds=15, app_password="original-secret",
        ),
        settings=settings,
    )
    db.commit()
    original_ciphertext = svc.get_smtp_sender_config(db).app_password_encrypted

    svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True, host="h2", port=587, username="u", from_email="f@example.com",
            use_starttls=True, timeout_seconds=15, app_password=None,
        ),
        settings=settings,
    )
    db.commit()
    row = svc.get_smtp_sender_config(db)
    assert row.host == "h2"
    assert row.app_password_encrypted == original_ciphertext
    assert svc.decrypt_app_password(row.app_password_encrypted, settings=settings) == "original-secret"


def test_remove_app_password_clears_secret() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True, host="h", port=587, username="u", from_email="f@example.com",
            use_starttls=True, timeout_seconds=15, app_password="secret",
        ),
        settings=settings,
    )
    db.commit()

    row = svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=False, host="h", port=587, username="u", from_email="f@example.com",
            use_starttls=True, timeout_seconds=15, remove_app_password=True,
        ),
        settings=settings,
    )
    db.commit()
    assert row.app_password_encrypted is None


def test_replace_and_remove_in_same_call_is_rejected() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    with pytest.raises(svc.SmtpSenderConfigValidationError):
        svc.save_smtp_sender_config(
            db,
            user=user,
            update=svc.SmtpSenderConfigUpdate(
                enabled=False, host="", port=587, username="", from_email="",
                use_starttls=True, timeout_seconds=15,
                app_password="new-secret", remove_app_password=True,
            ),
            settings=settings,
        )


@pytest.mark.parametrize(
    "update_kwargs",
    [
        {"host": "", "username": "u", "from_email": "f@example.com", "app_password": "p"},
        {"host": "h", "username": "", "from_email": "f@example.com", "app_password": "p"},
        {"host": "h", "username": "u", "from_email": "", "app_password": "p"},
        {"host": "h", "username": "u", "from_email": "f@example.com", "app_password": None},
    ],
)
def test_enabling_with_incomplete_config_is_rejected(update_kwargs: dict) -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    with pytest.raises(svc.SmtpSenderConfigValidationError):
        svc.save_smtp_sender_config(
            db,
            user=user,
            update=svc.SmtpSenderConfigUpdate(
                enabled=True,
                port=587,
                use_starttls=True,
                timeout_seconds=15,
                **update_kwargs,
            ),
            settings=settings,
        )


def test_disabling_with_incomplete_config_is_allowed_as_a_draft() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    row = svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=False, host="", port=587, username="", from_email="",
            use_starttls=True, timeout_seconds=15,
        ),
        settings=settings,
    )
    db.commit()
    assert row.enabled is False


# ---------------------------------------------------------------------------
# resolve_effective_smtp_settings precedence
# ---------------------------------------------------------------------------


def test_no_db_row_falls_back_to_env() -> None:
    session_factory = _session_factory()
    db = session_factory()
    settings = _settings(
        smtp_key=Fernet.generate_key().decode(),
        alert_email_enabled=True,
        alert_smtp_host="env-host",
        alert_smtp_username="env-user",
        alert_smtp_app_password="env-password",
        alert_email_from="env@example.com",
    )
    effective = svc.resolve_effective_smtp_settings(db, settings=settings)
    assert effective.source == "env"
    assert effective.alert_smtp_host == "env-host"
    assert effective.alert_smtp_app_password == "env-password"


def test_disabled_db_row_falls_back_to_env_entirely_not_partially() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(
        smtp_key=Fernet.generate_key().decode(),
        alert_email_enabled=True,
        alert_smtp_host="env-host",
        alert_smtp_username="env-user",
        alert_smtp_app_password="env-password",
        alert_email_from="env@example.com",
    )
    svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=False, host="db-host", port=2525, username="db-user", from_email="db@example.com",
            use_starttls=True, timeout_seconds=15, app_password="db-password",
        ),
        settings=settings,
    )
    db.commit()

    effective = svc.resolve_effective_smtp_settings(db, settings=settings)
    # Entirely env -- never a mix of the disabled DB row's host with env's
    # password or any other cross-source combination.
    assert effective.source == "env"
    assert effective.alert_smtp_host == "env-host"
    assert effective.alert_smtp_port == settings.alert_smtp_port


def test_enabled_complete_db_row_takes_precedence_over_env() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(
        smtp_key=Fernet.generate_key().decode(),
        alert_email_enabled=True,
        alert_smtp_host="env-host",
        alert_smtp_username="env-user",
        alert_smtp_app_password="env-password",
        alert_email_from="env@example.com",
    )
    svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True, host="db-host", port=2525, username="db-user", from_email="db@example.com",
            use_starttls=False, timeout_seconds=30, app_password="db-password",
        ),
        settings=settings,
    )
    db.commit()

    effective = svc.resolve_effective_smtp_settings(db, settings=settings)
    assert effective.source == "database"
    assert effective.alert_smtp_host == "db-host"
    assert effective.alert_smtp_port == 2525
    assert effective.alert_smtp_username == "db-user"
    assert effective.alert_smtp_app_password == "db-password"
    assert effective.alert_email_from == "db@example.com"
    assert effective.alert_email_use_starttls is False
    assert effective.alert_email_timeout_seconds == 30


def test_effective_settings_satisfy_smtp_settings_like_protocol() -> None:
    """`app.services.email_delivery.SmtpEmailAdapter` accepts either
    `app.config.Settings` or MAIL-04's `EffectiveSmtpSettings` -- this
    guards that the second one keeps satisfying the Protocol structurally
    if either dataclass's fields ever drift."""

    session_factory = _session_factory()
    db = session_factory()
    settings = _settings(smtp_key=Fernet.generate_key().decode())
    effective = svc.resolve_effective_smtp_settings(db, settings=settings)
    assert isinstance(effective, SmtpSettingsLike)


# ---------------------------------------------------------------------------
# serialize_smtp_sender_config
# ---------------------------------------------------------------------------


def test_serialize_never_includes_password_material() -> None:
    session_factory = _session_factory()
    db = session_factory()
    user = _make_user(session_factory)
    settings = _settings(smtp_key=Fernet.generate_key().decode())

    row = svc.save_smtp_sender_config(
        db,
        user=user,
        update=svc.SmtpSenderConfigUpdate(
            enabled=True, host="h", port=587, username="u", from_email="f@example.com",
            use_starttls=True, timeout_seconds=15, app_password="a-real-secret-value",
        ),
        settings=settings,
    )
    db.commit()

    serialized = svc.serialize_smtp_sender_config(row)
    assert "app_password" not in serialized
    assert serialized["app_password_configured"] is True
    blob = str(serialized)
    assert "a-real-secret-value" not in blob
    assert row.app_password_encrypted not in blob


def test_serialize_none_row_reports_unconfigured_defaults() -> None:
    serialized = svc.serialize_smtp_sender_config(None)
    assert serialized["app_password_configured"] is False
    assert serialized["enabled"] is False
