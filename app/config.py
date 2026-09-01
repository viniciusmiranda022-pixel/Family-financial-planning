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
    data_dir: Path = Path("/data")
    cookie_secure: bool = False
    session_hours: int = 12
    max_upload_mb: int = 25
    default_timezone: str = "America/Sao_Paulo"
    whisper_model: str = "tiny"
    whisper_language: str = "pt"
    advisor_enabled: bool = True
    advisor_url: str = "http://advisor:8081"
    advisor_shared_secret: str = ""
    advisor_timeout_seconds: int = 75
    integrity_ui_enabled: bool = False

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
