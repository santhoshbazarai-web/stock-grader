import os

import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "abc")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.kite_api_key == "abc"
    assert s.fyers_app_id is None
    # secrets never appear in repr
    assert "test-password" not in repr(s)


def test_invalid_fernet_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FERNET_KEY", "not-a-key")
    with pytest.raises(ValidationError, match="FERNET_KEY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_missing_app_password_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_PASSWORD")
    with pytest.raises(ValidationError, match="app_password"):
        Settings(_env_file=None)  # type: ignore[call-arg]


PROD = {
    "APP_ENV": "production",
    "WEB_URL": "https://grader.example.com",
    "SESSION_COOKIE_SECURE": "true",
    "APP_PASSWORD": "a-long-enough-password",
    "DATABASE_URL": "postgresql+psycopg://stockgrader:s3cr3t-hex@db:5432/stockgrader",
    "FYERS_REDIRECT_URI": "https://grader.example.com/api/brokers/fyers/callback",
    "KITE_REDIRECT_URI": "https://grader.example.com/api/brokers/kite/callback",
}


def _prod(monkeypatch: pytest.MonkeyPatch, **changes: str | None) -> Settings:
    for key, value in {**PROD, **changes}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_settings_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _prod(monkeypatch)
    assert s.app_env == "production" and s.session_cookie_secure
    assert (
        _prod(monkeypatch, FYERS_REDIRECT_URI=None, KITE_REDIRECT_URI=None).fyers_redirect_uri
        is None
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"WEB_URL": "http://grader.example.com"}, "WEB_URL must be an https"),
        ({"WEB_URL": "https://grader.example.com/app"}, "bare origin"),
        ({"SESSION_COOKIE_SECURE": "false"}, "SESSION_COOKIE_SECURE must be true"),
        ({"APP_PASSWORD": "short"}, "at least 12 characters"),
        (
            {"DATABASE_URL": "postgresql+psycopg://stockgrader:stockgrader@db:5432/stockgrader"},
            "real password",
        ),
        ({"DATABASE_URL": "postgresql+psycopg://stockgrader@db:5432/stockgrader"}, "real password"),
        (
            {"FYERS_REDIRECT_URI": "http://localhost:8000/api/brokers/fyers/callback"},
            "FYERS_REDIRECT_URI must be https://grader.example.com/api/brokers/fyers/callback",
        ),
        (
            {"KITE_REDIRECT_URI": "https://other.example.com/api/brokers/kite/callback"},
            "KITE_REDIRECT_URI must be",
        ),
    ],
)
def test_production_rejects_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _prod(monkeypatch, **changes)


def test_production_reports_every_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:telegram-secret")
    with pytest.raises(ValidationError) as err:
        _prod(
            monkeypatch, WEB_URL="http://x", SESSION_COOKIE_SECURE="false", APP_PASSWORD="pw-secret"
        )
    text = str(err.value)
    assert "WEB_URL" in text and "SESSION_COOKIE_SECURE" in text and "APP_PASSWORD" in text
    # Errors reach container logs: no secret from the input is echoed.
    for secret in ("pw-secret", "telegram-secret", "s3cr3t-hex", os.environ["FERNET_KEY"]):
        assert secret not in text


def test_invalid_key_error_does_not_echo_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FERNET_KEY", "almost-a-secret-key")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "almost-a-secret-key" not in str(err.value)


def test_development_is_lenient(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _prod(monkeypatch, APP_ENV="development", WEB_URL="http://localhost:3000", APP_PASSWORD="x")
    assert s.app_env == "development"


HOME = {
    "APP_ENV": "home",
    "WEB_URL": "http://127.0.0.1:3000",
    "SESSION_COOKIE_SECURE": "false",
    "APP_PASSWORD": "a-long-enough-password",
    "DATABASE_URL": "postgresql+psycopg://stockgrader:s3cr3t-hex@db:5432/stockgrader",
    "FYERS_REDIRECT_URI": "http://127.0.0.1:8000/api/brokers/fyers/callback",
}


def _home(monkeypatch: pytest.MonkeyPatch, **changes: str | None) -> Settings:
    for key, value in {**HOME, **changes}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"WEB_URL": "http://localhost:3000"},
        # phone access through `tailscale serve`: HTTPS on the tailnet name
        {"WEB_URL": "https://desk.tail1234.ts.net", "SESSION_COOKIE_SECURE": "true",
         "FYERS_REDIRECT_URI": "https://desk.tail1234.ts.net/api/brokers/fyers/callback"},
        {"WEB_URL": "http://100.101.102.103:3000"},  # a Tailscale IP
    ],
)  # fmt: skip
def test_home_settings_accepted(monkeypatch: pytest.MonkeyPatch, changes: dict[str, str]) -> None:
    assert _home(monkeypatch, **changes).app_env == "home"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"WEB_URL": "http://grader.example.com"}, "never a public one"),
        ({"WEB_URL": "http://203.0.113.7:3000"}, "never a public one"),
        ({"WEB_URL": "https://desk.tail1234.ts.net"}, "SESSION_COOKIE_SECURE must be true"),
        ({"SESSION_COOKIE_SECURE": "true"}, "SESSION_COOKIE_SECURE must be false"),
        ({"APP_PASSWORD": "short"}, "at least 12 characters"),
        ({"DATABASE_URL": "postgresql+psycopg://stockgrader:stockgrader@db:5432/x"},
         "real password"),
        ({"FYERS_REDIRECT_URI": "http://localhost:8000/api/brokers/fyers/callback"},
         "FYERS_REDIRECT_URI must be one of"),
    ],
)  # fmt: skip
def test_home_rejects_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _home(monkeypatch, **changes)
