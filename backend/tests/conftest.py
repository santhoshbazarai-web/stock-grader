from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import get_config
from app.core.settings import get_settings

REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("CONFIG_DIR", str(REPO_CONFIG_DIR))
    get_settings.cache_clear()
    get_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_config.cache_clear()
