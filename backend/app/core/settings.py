"""Environment-driven settings (secrets, connection strings, paths).

Tunable model parameters live in ``config/*.yaml`` (see ``app.core.config``), never here.
"""

import os
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

    # "production" (a public server behind Caddy) and "home" (SPEC §3.10: localhost plus
    # Tailscale) turn on the deployment checks below (fail fast).
    app_env: Literal["development", "production", "home"] = "development"

    database_url: str = "postgresql+psycopg://stockgrader:stockgrader@localhost:5432/stockgrader"
    redis_url: str = "redis://localhost:6379/0"
    config_dir: Path = Field(default=_REPO_ROOT / "config")  # CONFIG_DIR
    # Raw files (XBRL documents, filing lists, uploads) are cached here before parsing (SPEC
    # §3.2a): <raw_data_dir>/<source>/<yyyy>/<mm>/<dd>/<file>.
    raw_data_dir: Path = Field(default=_REPO_ROOT / "data" / "raw")  # RAW_DATA_DIR
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

    # Development / acceptance test only: the worker uses the deterministic offline exchange
    # (app/devtools/offline_exchange.py) instead of NSE and the brokers. Refused otherwise.
    offline_exchange: bool = False

    # Optional LLM thesis (SPEC §8a): a local Ollama server, e.g. http://127.0.0.1:11434 (or
    # http://host.docker.internal:11434 from the containers). Only local / private hosts: the
    # report's numbers never leave your network and no paid API is used. "fake" (development
    # only) is a built-in stand-in that writes the thesis from the facts, for tests and demos.
    thesis_llm_url: str | None = None

    # Optional Indian API key (stock.indianapi.in, bulk fundamentals). Sent only as the
    # X-Api-Key header; never logged or stored.
    indianapi_key: SecretStr | None = None

    # Optional Google Gemini key for the AI thesis (default provider). Sent only in the
    # x-goog-api-key header; never logged or stored.
    gemini_api_key: SecretStr | None = None

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

    @field_validator("thesis_llm_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _local_llm_only(self) -> "Settings":
        url = self.thesis_llm_url
        if url is None:
            return self
        if url == "fake":
            if self.app_env != "development":
                raise ValueError("THESIS_LLM_URL=fake is for APP_ENV=development only")
            return self
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("THESIS_LLM_URL must be an http(s) URL, e.g. http://127.0.0.1:11434")
        if not _local_host(parts.hostname):
            raise ValueError(
                "THESIS_LLM_URL must be a local or private host (Ollama on this machine or "
                "your network): the report's numbers never leave it, and no paid API is used"
            )
        return self

    @model_validator(mode="after")
    def _offline_only_in_development(self) -> "Settings":
        if self.offline_exchange and self.app_env != "development":
            raise ValueError("OFFLINE_EXCHANGE is for APP_ENV=development only (synthetic data)")
        return self

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

    @model_validator(mode="after")
    def _home_checks(self) -> "Settings":
        """SPEC §3.10 home deployment: reachable only on this machine or the tailnet (never
        port-forwarded), with real secrets and the broker redirect URIs the consoles expect."""
        if self.app_env != "home":
            return self
        problems: list[str] = []
        web = urlsplit(self.web_url)
        if not web.hostname or not _private_host(web.hostname):
            problems.append(
                "WEB_URL must be http://127.0.0.1:3000 or your Tailscale address "
                "(https://<machine>.<tailnet>.ts.net); never a public one"
            )
        elif web.scheme == "https" and not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true when WEB_URL is https")
        elif web.scheme == "http" and self.session_cookie_secure:
            problems.append(
                "SESSION_COOKIE_SECURE must be false when WEB_URL is plain http "
                "(the browser would drop the session cookie)"
            )
        if len(self.app_password.get_secret_value()) < MIN_PROD_PASSWORD:
            problems.append(f"APP_PASSWORD must be at least {MIN_PROD_PASSWORD} characters")
        if urlsplit(self.database_url).password in (None, "", "stockgrader"):
            problems.append("DATABASE_URL must use a real password (not the development default)")
        for broker, uri in (("fyers", self.fyers_redirect_uri), ("kite", self.kite_redirect_uri)):
            allowed = {HOME_REDIRECT.format(broker=broker),
                       f"{self.web_url.rstrip('/')}/api/brokers/{broker}/callback"}  # fmt: skip
            if uri is not None and uri not in allowed:
                problems.append(
                    f"{broker.upper()}_REDIRECT_URI must be one of "
                    f"{sorted(allowed)} (registered exactly so in the console)"
                )
        if problems:
            raise ValueError("APP_ENV=home: " + "; ".join(problems))
        return self


MIN_PROD_PASSWORD = 12
HOME_REDIRECT = "http://127.0.0.1:8000/api/brokers/{broker}/callback"  # SPEC §3.10


def _private_host(host: str) -> bool:
    """Loopback, or a Tailscale name / address: what a home deployment may be reached at."""
    import ipaddress

    if host in ("localhost",) or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")  # Tailscale CGNAT


def _local_host(host: str) -> bool:
    """A host on this machine or a private network: loopback, RFC 1918 / ULA addresses, a
    Docker service name (no dot), host.docker.internal, *.local, or the tailnet."""
    import ipaddress

    if _private_host(host) or "." not in host or host == "host.docker.internal":
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private


def config_dir_from_env() -> Path:
    """``CONFIG_DIR`` or the repo's config/, without requiring the secrets ``Settings`` needs
    (for offline tools such as ``python -m app.jobs xbrl-inspect``)."""
    return Path(os.environ.get("CONFIG_DIR") or _REPO_ROOT / "config")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
