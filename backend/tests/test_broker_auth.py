"""Encrypted token storage and the Fyers OAuth endpoints (SPEC §3.3)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_fyers_auth, get_token_store
from app.core.security import InvalidStateError, StateSigner, TokenCipher, get_state_signer
from app.core.settings import get_settings
from app.data.broker_tokens import BrokerTokenStore
from app.data.providers.fyers import FyersAuth
from app.data.providers.kite import IST as IST_TZ
from app.db.enums import Broker
from app.db.models import BrokerToken
from app.main import create_app
from tests.api_support import login

FIXTURES = Path(__file__).parent / "fixtures" / "fyers"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
APP_ID = "ABCD1234-100"
REDIRECT = "http://localhost:8000/api/brokers/fyers/callback"
NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)


def _store(db: Session, cipher: TokenCipher, now: datetime = NOW) -> BrokerTokenStore:
    return BrokerTokenStore(lambda: db, cipher, clock=lambda: now)


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key())


# ───────────────────────── token store ─────────────────────────


def test_token_encrypted_at_rest(db: Session, cipher: TokenCipher) -> None:
    store = _store(db, cipher)
    store.save(Broker.FYERS, "plain-access-token", NOW + timedelta(hours=10))

    raw = db.scalars(select(BrokerToken.access_token_encrypted)).one()
    assert b"plain-access-token" not in raw
    assert store.get_valid(Broker.FYERS) == "plain-access-token"
    status = store.status(Broker.FYERS)
    assert status.connected and status.expires_at == NOW + timedelta(hours=10)


def test_expired_token_not_returned(db: Session, cipher: TokenCipher) -> None:
    _store(db, cipher).save(Broker.FYERS, "t", NOW + timedelta(hours=1))
    later = _store(db, cipher, now=NOW + timedelta(hours=1))
    assert later.get_valid(Broker.FYERS) is None
    status = later.status(Broker.FYERS)
    assert not status.connected and "reconnect" in status.reason


def test_missing_token(db: Session, cipher: TokenCipher) -> None:
    store = _store(db, cipher)
    assert store.get_valid(Broker.KITE) is None
    assert store.status(Broker.KITE).reason == "not connected"


def test_reconnect_replaces_token(db: Session, cipher: TokenCipher) -> None:
    store = _store(db, cipher)
    store.save(Broker.FYERS, "day1", NOW + timedelta(hours=1))
    store.save(Broker.FYERS, "day2", NOW + timedelta(hours=20))
    assert store.get_valid(Broker.FYERS) == "day2"
    assert len(db.scalars(select(BrokerToken)).all()) == 1


def test_wrong_key_treated_as_disconnected(db: Session, cipher: TokenCipher) -> None:
    _store(db, cipher).save(Broker.FYERS, "t", NOW + timedelta(hours=1))
    rotated = _store(db, TokenCipher(Fernet.generate_key()))
    assert rotated.get_valid(Broker.FYERS) is None


def test_naive_expiry_rejected(db: Session, cipher: TokenCipher) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _store(db, cipher).save(Broker.FYERS, "t", datetime(2024, 1, 1))


def test_delete(db: Session, cipher: TokenCipher) -> None:
    store = _store(db, cipher)
    store.save(Broker.FYERS, "t", NOW + timedelta(hours=1))
    store.delete(Broker.FYERS)
    assert store.get_valid(Broker.FYERS) is None


# ───────────────────────── OAuth state ─────────────────────────


def test_state_roundtrip_and_tampering() -> None:
    signer = StateSigner("k", clock=lambda: 1000.0)
    state = signer.issue("fyers-login")
    signer.verify(state, "fyers-login", max_age_s=600)
    with pytest.raises(InvalidStateError, match="signature"):
        signer.verify(state, "kite-login", max_age_s=600)
    with pytest.raises(InvalidStateError, match="signature"):
        StateSigner("other").verify(state, "fyers-login", max_age_s=600)
    with pytest.raises(InvalidStateError, match="malformed"):
        signer.verify("garbage", "fyers-login", max_age_s=600)
    with pytest.raises(InvalidStateError, match="expired"):
        StateSigner("k", clock=lambda: 1601.0).verify(state, "fyers-login", max_age_s=600)


# ───────────────────────── endpoints ─────────────────────────


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def store(db: Session) -> BrokerTokenStore:
    # Real clock: the fixture token's `exp` is 2024, so status tests save their own tokens.
    return BrokerTokenStore(lambda: db, TokenCipher(get_settings().fernet_key.get_secret_value()))


@pytest.fixture
def client(store: BrokerTokenStore) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: store
    app.dependency_overrides[get_fyers_auth] = lambda: FyersAuth(APP_ID, "s3cret", REDIRECT)
    with TestClient(app, follow_redirects=False) as c:
        login(c)
        yield c


def _state(client: TestClient) -> str:
    res = client.get("/api/brokers/fyers/login")
    return parse_qs(urlparse(res.headers["location"]).query)["state"][0]


def _redirect_params(res: object) -> dict[str, str]:
    location = res.headers["location"]  # type: ignore[attr-defined]
    assert location.startswith("http://localhost:3000/settings?")
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def test_login_redirects_to_fyers(client: TestClient) -> None:
    res = client.get("/api/brokers/fyers/login")
    assert res.status_code == 307
    url = urlparse(res.headers["location"])
    assert url.netloc == "api-t1.fyers.in" and url.path == "/api/v3/generate-authcode"
    q = parse_qs(url.query)
    assert q["client_id"] == [APP_ID] and q["redirect_uri"] == [REDIRECT]
    get_state_signer().verify(q["state"][0], "fyers-login", max_age_s=600)


def test_callback_stores_encrypted_token(
    client: TestClient, http: responses.RequestsMock, store: BrokerTokenStore, db: Session
) -> None:
    token_body = json.loads((FIXTURES / "token_ok.json").read_text())
    http.add(responses.POST, TOKEN_URL, json=token_body)

    res = client.get(
        "/api/brokers/fyers/callback",
        params={"s": "ok", "code": "200", "auth_code": "abc", "state": _state(client)},
    )

    assert res.status_code == 303
    assert _redirect_params(res) == {"broker": "fyers", "status": "connected"}
    row = db.scalars(select(BrokerToken)).one()
    assert row.broker is Broker.FYERS
    assert row.expires_at == datetime(2024, 3, 30, 0, 30, tzinfo=UTC)  # JWT exp
    assert token_body["access_token"].encode() not in row.access_token_encrypted


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"s": "ok", "auth_code": "abc", "state": "forged.state.value"}, "invalid_state"),
        ({"s": "ok", "auth_code": "abc"}, "invalid_state"),
        ({"s": "error", "code": "-1", "message": "user cancelled"}, "login_declined"),
    ],
)
def test_callback_rejections_make_no_token_call(
    client: TestClient,
    http: responses.RequestsMock,
    db: Session,
    params: dict[str, str],
    reason: str,
) -> None:
    if reason != "invalid_state":
        params = {**params, "state": _state(client)}
    res = client.get("/api/brokers/fyers/callback", params=params)
    assert _redirect_params(res) == {"broker": "fyers", "status": "error", "reason": reason}
    assert len(http.calls) == 0
    assert db.scalars(select(BrokerToken)).first() is None


def test_callback_expired_state(client: TestClient, http: responses.RequestsMock) -> None:
    old = StateSigner(get_settings().fernet_key.get_secret_value(), clock=lambda: 0).issue(
        "fyers-login"
    )
    res = client.get(
        "/api/brokers/fyers/callback", params={"s": "ok", "auth_code": "abc", "state": old}
    )
    assert _redirect_params(res)["reason"] == "invalid_state"
    assert len(http.calls) == 0


def test_callback_exchange_failure(
    client: TestClient, http: responses.RequestsMock, db: Session
) -> None:
    http.add(
        responses.POST,
        TOKEN_URL,
        json=json.loads((FIXTURES / "token_invalid_code.json").read_text()),
        status=400,
    )
    res = client.get(
        "/api/brokers/fyers/callback",
        params={"s": "ok", "auth_code": "bad", "state": _state(client)},
    )
    assert _redirect_params(res)["reason"] == "exchange_failed"
    assert db.scalars(select(BrokerToken)).first() is None


def test_status(client: TestClient, store: BrokerTokenStore) -> None:
    store.save(Broker.FYERS, "secret-token-xyz", datetime.now(UTC) + timedelta(hours=5))
    res = client.get("/api/brokers/status")
    assert "secret-token-xyz" not in res.text
    body = res.json()
    by_broker = {b["broker"]: b for b in body}
    assert by_broker["fyers"]["connected"] is True
    assert by_broker["kite"] == {
        "broker": "kite",
        "configured": False,  # no KITE_* env in tests
        "connected": False,
        "expires_at": None,
        "reason": "not connected",
    }


def test_login_when_not_configured(store: BrokerTokenStore) -> None:
    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: store
    with TestClient(app, follow_redirects=False) as c:
        login(c)
        res = c.get("/api/brokers/fyers/login")
    assert res.status_code == 503
    assert "FYERS_APP_ID" in res.json()["detail"]


# ───────────────────────── Kite endpoints ─────────────────────────

KITE_SESSION_URL = "https://api.kite.trade/session/token"
KITE_FIXTURES = Path(__file__).parent / "fixtures" / "kite"


@pytest.fixture
def kite_client(store: BrokerTokenStore) -> Iterator[TestClient]:
    from datetime import time

    from app.api.deps import get_kite_auth
    from app.data.providers.kite import KiteAuth

    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: store
    app.dependency_overrides[get_kite_auth] = lambda: KiteAuth("kitekey", "sec", time(6, 0))
    with TestClient(app, follow_redirects=False) as c:
        login(c)
        yield c


def _kite_state(client: TestClient) -> str:
    res = client.get("/api/brokers/kite/login")
    assert res.status_code == 307
    q = parse_qs(urlparse(res.headers["location"]).query)
    return parse_qs(q["redirect_params"][0])["state"][0]


def test_kite_login_redirect(kite_client: TestClient) -> None:
    res = kite_client.get("/api/brokers/kite/login")
    url = urlparse(res.headers["location"])
    assert url.netloc == "kite.zerodha.com" and url.path == "/connect/login"
    get_state_signer().verify(_kite_state(kite_client), "kite-login", max_age_s=600)


def test_kite_callback_stores_token(
    kite_client: TestClient, http: responses.RequestsMock, store: BrokerTokenStore, db: Session
) -> None:
    http.add(
        responses.POST,
        KITE_SESSION_URL,
        json=json.loads((KITE_FIXTURES / "session_ok.json").read_text()),
    )
    res = kite_client.get(
        "/api/brokers/kite/callback",
        params={
            "request_token": "req",
            "action": "login",
            "status": "success",
            "state": _kite_state(kite_client),
        },
    )
    assert _redirect_params(res) == {"broker": "kite", "status": "connected"}
    assert store.get_valid(Broker.KITE) == "kite-access-token-abc123"
    row = db.scalars(select(BrokerToken).where(BrokerToken.broker == Broker.KITE)).one()
    assert row.expires_at.astimezone(IST_TZ).time() == datetime.min.time().replace(hour=6)
    assert b"kite-access-token-abc123" not in row.access_token_encrypted


def test_kite_state_not_valid_for_fyers(
    kite_client: TestClient, http: responses.RequestsMock
) -> None:
    fyers_state = StateSigner(get_settings().fernet_key.get_secret_value()).issue("fyers-login")
    res = kite_client.get(
        "/api/brokers/kite/callback",
        params={"request_token": "req", "status": "success", "state": fyers_state},
    )
    assert _redirect_params(res)["reason"] == "invalid_state"
    assert len(http.calls) == 0


def test_kite_callback_declined(kite_client: TestClient, http: responses.RequestsMock) -> None:
    res = kite_client.get(
        "/api/brokers/kite/callback",
        params={"status": "cancelled", "state": _kite_state(kite_client)},
    )
    assert _redirect_params(res) == {
        "broker": "kite",
        "status": "error",
        "reason": "login_declined",
    }
    assert len(http.calls) == 0


def test_kite_login_when_not_configured(store: BrokerTokenStore) -> None:
    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: store
    with TestClient(app, follow_redirects=False) as c:
        login(c)
        res = c.get("/api/brokers/kite/login")
    assert res.status_code == 503
    assert "KITE_API_KEY" in res.json()["detail"]
