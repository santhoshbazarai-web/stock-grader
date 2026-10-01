"""SPEC §3.10 home deployment invariants in docker-compose.home.yml: only the web app and the
API are published, on 127.0.0.1 only (never port-forwardable); every long-running service
restarts unless stopped; backups need an explicit path; the doctor is a tools-profile one-shot."""

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
LONG_RUNNING = {"db", "redis", "api", "worker", "web", "backup"}


def compose() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.home.yml").read_text())
    return doc


def test_only_web_and_api_published_on_localhost() -> None:
    services = compose()["services"]
    published = {name: svc["ports"] for name, svc in services.items() if svc.get("ports")}
    assert published == {"api": ["127.0.0.1:8000:8000"], "web": ["127.0.0.1:3000:3000"]}


def test_restart_policies() -> None:
    services = compose()["services"]
    for name in LONG_RUNNING:
        assert services[name]["restart"] == "unless-stopped", name
    assert services["migrate"]["restart"] == "no"
    assert services["doctor"]["restart"] == "no" and services["doctor"]["profiles"] == ["tools"]


def test_backups_and_app_env() -> None:
    text = (REPO / "docker-compose.home.yml").read_text()
    assert "${BACKUP_PATH:?" in text  # no silent default: backups belong on another drive
    backend = compose()["x-backend"]["environment"]
    assert backend["APP_ENV"] == "home"
    assert backend["FYERS_REDIRECT_URI"].endswith(
        ":-http://127.0.0.1:8000/api/brokers/fyers/callback}"
    )
    assert (REPO / ".env.home.example").is_file()


def test_worker_is_the_scheduler() -> None:
    assert compose()["services"]["worker"]["command"] == ["python", "-m", "app.jobs.worker"]
