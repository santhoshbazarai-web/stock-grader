"""Environment-driven settings (secrets, connection strings, paths).

Tunable model parameters live in ``config/*.yaml`` (see ``app.core.config``), never here.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/settings.py -> repo root is three levels above backend/app
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://stockgrader:stockgrader@localhost:5432/stockgrader"
    redis_url: str = "redis://localhost:6379/0"
    config_dir: Path = Field(default=_REPO_ROOT / "config")
    log_level: str = "INFO"
    # Browser-facing URL of the web app; broker OAuth callbacks redirect back here.
    web_url: str = "http://localhost:3000"

    fernet_key: SecretStr
    app_password: SecretStr
    # Single-user session: signed cookie lifetime, and Secure flag (enable behind HTTPS).
    session_ttl_hours: float = Field(default=12.0, gt=0)
    session_cookie_secure: bool = False
    # Login brute-force protection: failures per client IP before a lockout of this long.
    login_max_failures: int = Field(default=5, ge=1)
    login_lockout_s: int = Field(default=900, ge=1)
    # Screener Excel uploads larger than this are rejected.
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)

    fyers_app_id: str | None = None
    fyers_secret: SecretStr | None = None
    fyers_redirect_uri: str | None = None
    kite_api_key: str | None = None
    kite_api_secret: SecretStr | None = None
    kite_redirect_uri: str | None = None

    @field_validator("fernet_key")
    @classmethod
    def _valid_fernet_key(cls, v: SecretStr) -> SecretStr:
        try:
            Fernet(v.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("FERNET_KEY must be a 32-byte url-safe base64 Fernet key") from exc
        return v

    @field_validator("app_password")
    @classmethod
    def _non_empty_password(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value():
            raise ValueError("APP_PASSWORD must not be empty")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
