from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.core.config import ConfigError
from app.main import create_app


def test_health_ok() -> None:
    with TestClient(create_app()) as client:
        res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "version": __version__}


def test_startup_fails_fast_on_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))  # empty dir: every file missing
    with pytest.raises(ConfigError, match="missing config file"), TestClient(create_app()):
        pass
