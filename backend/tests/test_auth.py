"""Single-user auth: login, cookie / bearer sessions, lockout, logout, protected routes."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

from app.core.auth import SESSION_COOKIE, SessionSigner, get_session_signer
from app.core.settings import get_settings
from tests.api_support import PASSWORD, app_client


@pytest.fixture
def anon(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db, authed=False)
    redis_client.delete("auth:fail:testclient")


def test_health_is_public(anon: TestClient) -> None:
    assert anon.get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/me",
        "/api/stocks/search?q=TCS",
        "/api/stocks/TCS/report",
        "/api/screener",
        "/api/watchlist",
        "/api/alerts",
        "/api/config",
        "/api/jobs",
        "/api/brokers/status",
        "/api/stocks/TCS/technical/debug",
    ],
)
def test_protected_routes_need_a_session(anon: TestClient, path: str) -> None:
    res = anon.get(path)
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"


def test_login_sets_http_only_cookie_and_returns_token(anon: TestClient) -> None:
    res = anon.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 200
    body = res.json()
    assert body["expires_in_s"] == 12 * 3600
    cookie = res.headers["set-cookie"]
    assert f"{SESSION_COOKIE}={body['token']}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert anon.get("/api/auth/me").json() == {"authenticated": True}  # cookie jar


def test_bearer_token_works_without_cookie(anon: TestClient) -> None:
    token = anon.post("/api/auth/login", json={"password": PASSWORD}).json()["token"]
    anon.cookies.clear()
    assert anon.get("/api/auth/me").status_code == 401
    ok = anon.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_wrong_password_and_lockout(anon: TestClient) -> None:
    for _ in range(get_settings().login_max_failures):
        assert anon.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    locked = anon.post("/api/auth/login", json={"password": PASSWORD})
    assert locked.status_code == 429  # even the right password, while locked out
    assert int(locked.headers["retry-after"]) > 0


def test_successful_login_resets_failures(anon: TestClient, redis_client: Redis) -> None:
    anon.post("/api/auth/login", json={"password": "nope"})
    assert redis_client.get("auth:fail:testclient") == b"1"
    anon.post("/api/auth/login", json={"password": PASSWORD})
    assert redis_client.get("auth:fail:testclient") is None


def test_logout_clears_cookie(anon: TestClient) -> None:
    anon.post("/api/auth/login", json={"password": PASSWORD})
    res = anon.post("/api/auth/logout")
    assert res.status_code == 204
    assert f'{SESSION_COOKIE}=""' in res.headers["set-cookie"]
    assert anon.get("/api/auth/me").status_code == 401


def test_tokens_are_forged_expired_or_revoked_by_password_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = get_session_signer()
    token = signer.issue()
    assert signer.valid(token)
    assert not signer.valid(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert not signer.valid("garbage") and not signer.valid(None)

    settings = get_settings()
    ttl_s = settings.session_ttl_hours * 3600
    at_login = SessionSigner(settings, clock=lambda: 1_000_000.0).issue()
    assert SessionSigner(settings, clock=lambda: 1_000_000.0 + ttl_s).valid(at_login)
    assert not SessionSigner(settings, clock=lambda: 1_000_001.0 + ttl_s).valid(at_login)

    monkeypatch.setenv("APP_PASSWORD", "a-new-password")
    get_settings.cache_clear()
    assert not SessionSigner(get_settings()).valid(token)


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        ("10.0.0.5", None, [], "10.0.0.5"),  # no proxy config: the peer
        ("10.0.0.5", "1.2.3.4", [], "10.0.0.5"),  # untrusted peer: header ignored (spoofable)
        ("172.18.0.3", "1.2.3.4", ["172.18.0.0/16"], "1.2.3.4"),  # web container → client
        # client-supplied junk on the left is ignored; right-most untrusted hop wins
        ("172.18.0.3", "6.6.6.6, 1.2.3.4, 172.18.0.2", ["172.18.0.0/16"], "1.2.3.4"),
        ("127.0.0.1", "not-an-ip", ["127.0.0.1"], "not-an-ip"),
        ("172.18.0.3", "172.18.0.2", ["172.18.0.0/16"], "172.18.0.3"),  # all hops trusted
        (None, "1.2.3.4", ["0.0.0.0/0"], "unknown"),
    ],
)
def test_client_ip(peer: str | None, xff: str | None, trusted: list[str], expected: str) -> None:
    from app.core.auth import client_ip

    assert client_ip(peer, xff, trusted) == expected


def test_lockout_is_per_forwarded_client(
    db: Session, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The web app's proxy sits on loopback here; trust it so X-Forwarded-For counts.
    monkeypatch.setenv("TRUSTED_PROXIES", '["127.0.0.1"]')
    get_settings.cache_clear()
    for ip in ("203.0.113.7", "198.51.100.9"):
        redis_client.delete(f"auth:fail:{ip}")
    for c in app_client(db, authed=False, client=("127.0.0.1", 50000)):
        attacker = {"x-forwarded-for": "203.0.113.7"}
        for _ in range(get_settings().login_max_failures):
            c.post("/api/auth/login", json={"password": "nope"}, headers=attacker)
        assert (
            c.post("/api/auth/login", json={"password": PASSWORD}, headers=attacker).status_code
            == 429
        )
        owner = {"x-forwarded-for": "198.51.100.9"}
        assert (
            c.post("/api/auth/login", json={"password": PASSWORD}, headers=owner).status_code == 200
        )
    redis_client.delete("auth:fail:203.0.113.7")
