"""``make doctor`` (SPEC §3.10): every check reports independently; failures set exit code 1;
secrets are never printed."""

import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
import responses
from sqlalchemy import Engine

from app import doctor
from app.core.settings import get_settings
from app.doctor import Doctor, main, render
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

NSE = "https://www.nseindia.com/"
ARCHIVES = "https://nsearchives.nseindia.com/"


@pytest.fixture
def ok_env(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("APP_PASSWORD", "a-long-enough-password")
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    (backups / "daily" / "stockgrader-2024-06-14T020000.dump").write_bytes(b"x")
    (backups / ".last-success").write_text(str(int(time.time()) - 3600))
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    get_settings.cache_clear()
    return backups


def by_area(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.area: c for c in checks}


@responses.activate
def test_healthy_stack(ok_env: Path) -> None:
    responses.add(responses.GET, NSE, status=200)
    responses.add(responses.GET, ARCHIVES, status=403)  # bare archives root: still reachable
    checks = by_area(Doctor().run())
    assert list(checks)[:2] == ["env", "config"]
    assert checks["config"].status == "ok"
    assert checks["database"].status == "ok" and "latest migration" in checks["database"].detail
    assert checks["redis"].status == "ok"
    assert checks["nse site"].status == "ok" and checks["nse archives"].status == "ok"
    assert checks["backups"].status == "ok"
    assert checks["backups"].detail.startswith("last backup 1.0 h ago, 1 daily dump(s)")
    assert checks["broker fyers"].status == "warn"  # no FYERS_* credentials in tests
    assert checks["worker"].status in ("ok", "warn")
    # Telegram is optional: only a warning, and no secret appears anywhere
    assert checks["env"].status == "warn" and "Telegram not configured" in checks["env"].detail
    text = render(list(checks.values()))
    assert "a-long-enough-password" not in text and "0 failure(s)" in text


@responses.activate
def test_failures_set_exit_code(ok_env: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    responses.add(responses.GET, NSE, body=requests.ConnectionError("down"))
    responses.add(responses.GET, ARCHIVES, status=200)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda _p: usage(100, 99, 2 * 1024**3))
    (ok_env / ".last-success").write_text(str(int(time.time()) - 72 * 3600))
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "✗ disk (raw data)" in out and "2.0 GB free" in out
    assert "! backups" in out and "72.0 h ago" in out
    assert "! nse site" in out and "unreachable" in out


def test_bad_settings_are_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FERNET_KEY", "not-a-key")
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    get_settings.cache_clear()
    checks = by_area(Doctor(http=_NoNet()).run())
    assert checks["env"].status == "fail" and "FERNET_KEY" in checks["env"].detail
    assert "not-a-key" not in checks["env"].detail
    assert checks["config"].status == "ok"  # still checked
    assert "database" not in checks  # needs settings
    assert checks["backups"].status == "warn"


def test_worker_idle_is_a_warning(ok_env: Path, migrated_engine: Engine) -> None:
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.db.enums import JobStatus
    from app.db.models import JobRun

    with Session(migrated_engine) as s:
        run = JobRun(job_name="eod_prices", status=JobStatus.SUCCESS,
                     started_at=datetime.now(UTC) - timedelta(hours=1))  # fmt: skip
        s.add(run)
        s.commit()
        try:
            fresh = by_area(Doctor(http=_NoNet()).run())["worker"]
            later = datetime.now(UTC) + timedelta(days=30)
            stale = by_area(Doctor(now=lambda: later, http=_NoNet()).run())["worker"]
        finally:
            s.execute(delete(JobRun).where(JobRun.id == run.id))
            s.commit()
    assert fresh.status == "ok"
    assert stale.status == "warn" and "is the worker running?" in stale.detail


class _NoNet:
    def get(self, url: str, **_kw: object) -> object:
        raise requests.ConnectionError("offline")


def test_a_crashing_check_is_reported_not_raised(
    ok_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Doctor, *_a: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(Doctor, "_disk", boom)
    checks = by_area(Doctor(http=_NoNet()).run())
    assert checks["disk"].status == "fail" and "RuntimeError: kaput" in checks["disk"].detail
    assert checks["backups"].status == "ok"  # later checks still ran
