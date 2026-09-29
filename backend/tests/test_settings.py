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
