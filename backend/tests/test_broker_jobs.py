"""broker_token_check (SPEC §3.3: morning reminder) — only enabled, configured brokers with an
expired or missing token are reminded; nothing is sent to the broker."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import JobName
from app.core.security import get_cipher
from app.core.settings import get_settings
from app.data.broker_tokens import BrokerTokenStore
from app.db.enums import Broker
from app.db.models import Notification
from app.jobs.registry import REGISTRY
from app.jobs.runner import run_job
from tests.jobs_support import Env


@pytest.fixture
def fyers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FYERS_APP_ID", "APP-100")
    monkeypatch.setenv("FYERS_SECRET", "s3cret")
    monkeypatch.setenv("FYERS_REDIRECT_URI", "http://127.0.0.1:8000/api/brokers/fyers/callback")
    monkeypatch.setenv("KITE_API_KEY", "kite-key")  # configured, but disabled in providers.yaml
    monkeypatch.setenv("KITE_API_SECRET", "kite-secret")
    get_settings.cache_clear()


def notes(env: Env) -> list[Notification]:
    with env.session() as s:
        return list(s.scalars(select(Notification)))


def test_reminds_when_fyers_token_expired(env: Env, fyers_env: None) -> None:
    store = BrokerTokenStore(env.Session, get_cipher())
    store.save(Broker.FYERS, "tok", datetime.now(UTC) - timedelta(hours=1))
    rec = run_job(REGISTRY[JobName.BROKER_TOKEN_CHECK], env.ctx)
    assert rec.outcome is not None
    assert rec.outcome.details == {
        "brokers": {"fyers": "token expired — reconnect", "kite": "disabled"},
        "reminded": ["fyers"],
    }
    [n] = notes(env)
    assert n.kind == "broker_token" and n.title == "Fyers token expired: reconnect"
    assert "Settings → Brokers" in n.body and n.telegram == "disabled"


def test_no_reminder_when_connected_or_not_configured(env: Env, fyers_env: None) -> None:
    store = BrokerTokenStore(env.Session, get_cipher())
    store.save(Broker.FYERS, "tok", datetime.now(UTC) + timedelta(hours=5))
    rec = run_job(REGISTRY[JobName.BROKER_TOKEN_CHECK], env.ctx)
    assert rec.outcome is not None and rec.outcome.details["reminded"] == []
    assert notes(env) == []


def test_not_configured_is_not_reminded(env: Env) -> None:
    rec = run_job(REGISTRY[JobName.BROKER_TOKEN_CHECK], env.ctx)
    assert rec.outcome is not None
    assert rec.outcome.details["brokers"]["fyers"] == "not configured"
    assert notes(env) == []
