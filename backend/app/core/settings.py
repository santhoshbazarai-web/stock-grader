"""Environment-driven settings (secrets, connection strings, paths).

Tunable model parameters live in ``config/*.yaml`` (see ``app.core.config``), never here.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/settings.py -> repo root is three levels above backend/app
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Startup errors end up in container logs: never echo the (secret-bearing) input.
        hide_input_in_errors=True,
    )

    # "production" turns on the deployment checks in ``_production_checks`` (fail fast).
    app_env: Literal["development", "production"] = "development"

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
    # Reverse proxies (IPs or CIDRs, JSON list) allowed to report the client IP in
    # X-Forwarded-For, e.g. the web container's network. Empty = use the direct peer.
    trusted_proxies: list[str] = Field(default_factory=list)
    # Screener Excel uploads larger than this are rejected.
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)

    fyers_app_id: str | None = None
    fyers_secret: SecretStr | None = None
    fyers_redirect_uri: str | None = None
    kite_api_key: str | None = None
    kite_api_secret: SecretStr | None = None
    kite_redirect_uri: str | None = None

    # Optional Telegram delivery for alert notifications (never logged).
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

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

    @model_validator(mode="after")
    def _production_checks(self) -> "Settings":
        """In production, refuse to start with settings that are only safe on localhost.
        Every problem is reported at once."""
        if self.app_env != "production":
            return self
        problems: list[str] = []
        web = urlsplit(self.web_url)
        if web.scheme != "https" or not web.hostname:
            problems.append("WEB_URL must be an https:// URL (the public address of the app)")
        elif web.path.strip("/") or web.query:
            problems.append("WEB_URL must be the bare origin, e.g. https://grader.example.com")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true (the session cookie is HTTPS-only)")
        if len(self.app_password.get_secret_value()) < MIN_PROD_PASSWORD:
            problems.append(f"APP_PASSWORD must be at least {MIN_PROD_PASSWORD} characters")
        db_password = urlsplit(self.database_url).password
        if db_password in (None, "", "stockgrader"):
            problems.append("DATABASE_URL must use a real password (not the development default)")
        for broker, uri in (("fyers", self.fyers_redirect_uri), ("kite", self.kite_redirect_uri)):
            if uri is None:
                continue
            expected = f"{self.web_url.rstrip('/')}/api/brokers/{broker}/callback"
            if uri != expected:
                problems.append(
                    f"{broker.upper()}_REDIRECT_URI must be {expected} (and registered exactly "
                    "like that in the broker's developer console)"
                )
        if problems:
            raise ValueError("APP_ENV=production: " + "; ".join(problems))
        return self


MIN_PROD_PASSWORD = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
