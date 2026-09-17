from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Planejamento Financeiro Familiar"
    environment: str = "production"
    database_url: str = "postgresql+psycopg://family:family@db:5432/family_finance"
    secret_key: str = Field(min_length=32)
    file_encryption_key: str = Field(min_length=40)
    # Fase 4 (docs/WORK_ORDER_LOCAL_MFA_TOTP.md, "Separação de chaves"):
    # exclusive to encrypting the TOTP secret at rest. Never reused as
    # SECRET_KEY/FILE_ENCRYPTION_KEY, and never falls back to plaintext if
    # unset -- pydantic-settings raises at startup, matching the Work
    # Order's "falhar explicitamente" requirement.
    mfa_encryption_key: str = Field(min_length=40)
    # TTL for both the pending-auth cookie (`ffp_mfa_pending`) and an
    # unconfirmed TOTP secret's `setup_expires_at`. Kept as one value: the
    # cookie already forces re-login at this boundary, so a second,
    # independently-tracked expiry for the secret would only ever expire
    # after the cookie already invalidated the flow.
    mfa_pending_ttl_seconds: int = 300
    mfa_rate_limit_max_attempts: int = 5
    mfa_rate_limit_lockout_seconds: int = 300
    data_dir: Path = Path("/data")
    cookie_secure: bool = False
    session_hours: int = 12
    max_upload_mb: int = 25
    max_batch_files: int = 20
    max_batch_total_mb: int = 200
    default_timezone: str = "America/Sao_Paulo"
    whisper_model: str = "tiny"
    whisper_language: str = "pt"
    advisor_enabled: bool = True
    advisor_url: str = "http://advisor:8081"
    advisor_shared_secret: str = ""
    advisor_timeout_seconds: int = 75
    integrity_ui_enabled: bool = False
    capture_async_processing_enabled: bool = True
    capture_job_max_attempts: int = 3
    capture_job_stale_after_seconds: int = 600
    # MAIL-01 (docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md, issue #68): the
    # Gmail/SMTP sender credential lives exclusively here (env), never in
    # the DB or a request/response -- `NotificationSettings`/
    # `NotificationRecipient` (MAIL-00) have no column for it. `enabled`
    # defaults `False` so a deployment that never set these vars stays
    # silent instead of trying to send with an empty host/password.
    alert_email_enabled: bool = False
    alert_smtp_host: str = ""
    alert_smtp_port: int = 587
    alert_smtp_username: str = ""
    alert_smtp_app_password: str = ""
    alert_email_from: str = ""
    alert_email_use_starttls: bool = True
    alert_email_timeout_seconds: int = 15
    # Process-local throttle for `POST /notification-settings/test-email`
    # ("rate-limited de forma simples para evitar spam acidental" -- Work
    # Order). `scripts/entrypoint.sh` runs a single `uvicorn` process with
    # no `--workers`, so an in-memory-per-process counter (see
    # `app.services.email_delivery._EmailSendThrottle`) is an effective
    # control here, not just a best-effort one.
    alert_test_email_min_interval_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


@lru_cache
def get_settings() -> Settings:
    return Settings()
