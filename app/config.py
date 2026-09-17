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
    # MAIL-02 (docs/WORK_ORDER_DUE_DATE_EMAIL_ALERTS.md, issue #69): the
    # outbox/scheduler worker's own tuning. `notification_worker_poll_seconds`
    # is how often `python -m app.cli.notification_worker` sweeps for newly
    # eligible D-1/D0 events when run as a persistent process ("polling leve
    # e barato" -- Work Order); it never affects when a delivery is
    # *eligible* (that is always local calendar-date + `send_time_local`),
    # only how promptly the worker notices. `notification_delivery_max_attempts`
    # and `notification_delivery_retry_backoff_seconds` bound retry of a
    # transient SMTP failure ("no máximo 3 tentativas", "backoff crescente");
    # `notification_delivery_stale_after_seconds` is how long a delivery may
    # sit in `sending` before it is considered an abandoned/crashed claim and
    # offered for reclaim -- mirrors `capture_job_stale_after_seconds` above.
    notification_worker_poll_seconds: int = 120
    notification_delivery_max_attempts: int = 3
    notification_delivery_retry_backoff_seconds: int = 300
    notification_delivery_stale_after_seconds: int = 900
    # Issue #85 (`docs/WORK_ORDER_PRIVILEGE_DI_CVM.md`): the daily CVM fund
    # valuation job. `cvm_valuation_enabled` defaults `False` -- unlike
    # `advisor_enabled` above, this feature ships dormant on purpose: it is a
    # new external-data ingestion path feeding financial valuation, so an
    # operator opts in deliberately once rather than it running unattended
    # from the first deploy. The source (CVM Open Data Portal, `fi-doc-
    # inf_diario` dataset) and its schema-variance handling are verified --
    # see "Live-source verification" in
    # `docs/PRIVILEGE_DI_CVM_INGESTION_INVESTIGATION.md`. `cvm_quota_base_url`
    # is the CVM Open Data Portal's documented bulk-CSV path (`{base}/
    # inf_diario_fi_{YYYYMM}.csv`), overridable for tests/fixtures.
    # `cvm_valuation_worker_poll_seconds` mirrors
    # `notification_worker_poll_seconds`'s role for the persistent-loop
    # variant of `python -m app.cli.cvm_valuation_worker` -- CVM only
    # publishes a new quota at most once per business day, so this is
    # intentionally coarse (hours, not minutes).
    cvm_valuation_enabled: bool = False
    cvm_quota_base_url: str = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS"
    cvm_quota_timeout_seconds: int = 60
    cvm_valuation_worker_poll_seconds: int = 21600
    # WA-01 (docs/WORK_ORDER_WA_01.md, issue #73): the WhatsApp webhook
    # gateway ships **disabled by default** -- unlike `advisor_enabled`, this
    # is a new public-facing surface (`docs/ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`
    # §4.2), so an operator opts in deliberately once real Meta credentials
    # exist, exactly like `cvm_valuation_enabled`'s "ships effectively idle"
    # rollout control above. No field here has a mandatory (no-default)
    # value the way `mfa_encryption_key` does, precisely because this
    # feature must never break an existing deployment's startup that has
    # not configured WhatsApp at all -- `app/services/whatsapp_gateway.py`
    # fails closed (raises `WhatsAppConfigurationError`) the first time an
    # unconfigured key is actually *used* (hashing/encrypting a phone
    # number, or verifying a webhook signature), the same "fail explicitly,
    # never fall back to plaintext/unsigned" contract as
    # `app.services.mfa._cipher`, just checked at use-time instead of at
    # process startup since the feature is opt-in.
    whatsapp_gateway_enabled: bool = False
    # Meta app secret used to verify `X-Hub-Signature-256` (HMAC-SHA256 over
    # the raw request body) on every inbound webhook POST -- never the same
    # value as `whatsapp_verify_token` below (different purpose: this one
    # authenticates the sender of every event; the token below only proves
    # ownership during the one-time GET subscription handshake).
    whatsapp_app_secret: str = ""
    # The `hub.verify_token` Meta's dashboard sends back during the webhook
    # subscription GET handshake -- compared to what the operator configured
    # in Meta's App Dashboard when registering the callback URL.
    whatsapp_verify_token: str = ""
    # Fernet key exclusive to encrypting a linked phone number at rest for
    # admin display (`app.services.whatsapp_gateway.encrypt_phone`) *and*,
    # reusing the same "Key separation" + HMAC idiom as
    # `app.services.mfa.hash_recovery_code`, as the HMAC key that derives
    # the deterministic `phone_hash` used to look up an inbound sender
    # without ever storing or logging the number in the clear. Never
    # `SECRET_KEY`/`FILE_ENCRYPTION_KEY`/`MFA_ENCRYPTION_KEY`.
    whatsapp_phone_encryption_key: str = ""
    # Fixed-window rate limit applied per sender (`phone_hash`), enforced by
    # `app.services.whatsapp_gateway.check_rate_limit` via an atomically
    # locked `WhatsAppRateLimitBucket` row -- abuse/flood protection for the
    # one genuinely public endpoint in this project, not a financial control.
    whatsapp_rate_limit_max_per_window: int = 30
    whatsapp_rate_limit_window_seconds: int = 60
    # WA-03 (docs/WORK_ORDER_WA_03.md, issue #75): outbound send credentials
    # for the real Meta Cloud API provider (`app.services.whatsapp_gateway.
    # MetaCloudApiProvider`) -- absent by default, same "ships idle until an
    # operator opts in" contract as every other WhatsApp setting above.
    # `whatsapp_access_token` is the permanent/system-user access token;
    # `whatsapp_phone_number_id` is the Cloud API sender identity replies are
    # sent from (`POST /{phone_number_id}/messages`). Never reused for
    # inbound verification -- those stay `whatsapp_app_secret`/
    # `whatsapp_verify_token` above, a distinct credential pair for a
    # distinct direction.
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_base_url: str = "https://graph.facebook.com/v21.0"
    whatsapp_send_timeout_seconds: int = 10

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
