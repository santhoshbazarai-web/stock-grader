"""nse-diagnose / bse-diagnose (app/data/diagnose.py) on recorded responses: verdicts, the row
format, cookie values never shown, the stored result, the remembered method, the CLI and the
Settings API. Fake fetchers serve tests/fixtures; nothing touches the network."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from app.api import data_sources
from app.core.config import SessionMethod, load_config
from app.data import diagnose as diag
from app.data.providers.web_session import (
    LocalMemory,
    MethodUnavailable,
    SiteProfile,
    TransportError,
    WebResponse,
    WebSession,
    _cookie_names,
)
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

PC = load_config(REPO_CONFIG_DIR).providers
FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
TODAY = date(2026, 10, 1)


def recorded(name: str, url: str = "") -> WebResponse:
    rec = json.loads((FIX / "web" / name).read_text())
    return WebResponse(url, rec["status"], dict(rec["headers"]), rec["body"].encode(),
                       _cookie_names(rec["set_cookie"]))  # fmt: skip


def nse_json(name: str) -> WebResponse:
    return WebResponse("", 200, {"server": "AkamaiGHost", "content-type": "application/json"},
                       (FIX / "nse" / name).read_bytes())  # fmt: skip


@dataclass
class Replay:
    """Serves recorded responses: ``pages`` for the warm-up, ``api`` by URL substring."""

    method: SessionMethod
    pages: list[WebResponse]
    api: dict[str, WebResponse] = field(default_factory=dict)
    default: WebResponse | None = None
    calls: list[str] = field(default_factory=list)

    def warm_up(self) -> list[WebResponse]:
        self.calls.append("warm")
        return self.pages

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
        self.calls.append(url)
        for key, r in self.api.items():
            if key in url:
                return r
        if self.default is None:
            raise TransportError("ConnectTimeout")
        return self.default

    def close(self) -> None:
        pass


def factories(**by_method: Replay | Exception) -> dict[SessionMethod, Callable[..., Replay]]:
    def make(f: Replay | Exception) -> Callable[..., Replay]:
        def build(site: SiteProfile) -> Replay:
            if isinstance(f, Exception):
                raise f
            return f

        return build

    return {m: make(f) for m, f in by_method.items()}  # type: ignore[misc]


def blocked_everywhere(method: SessionMethod) -> Replay:
    deny = recorded("nse_akamai_403.json")
    return Replay(method, [deny, deny], default=deny)


def working(method: SessionMethod) -> Replay:
    home = recorded("nse_home_ok.json")
    return Replay(method, [home, home], api={
        "share-holdings": nse_json("shareholding_master_sampleind.json"),
        "corporateActions": nse_json("corporate_actions_sampleind.json"),
        "financial-results": nse_json("financial_results_acme.json"),
    }, default=WebResponse("", 200, {"content-type": "application/json"}, b"[]"))  # fmt: skip


@pytest.mark.parametrize(
    ("status", "body", "expect_json", "expected"),
    [
        (200, b'[{"a": 1}]', True, "OK"),
        (200, b"<html/>", False, "OK"),
        (403, b"denied", True, "blocked-403"),
        (401, b"", True, "blocked-401"),
        (429, b"", True, "rate-limited-429"),
        (200, b"", True, "empty"),
        (200, b"[]", True, "empty"),
        (200, b'{"data": []}', True, "empty"),
        (200, b"<html>Access Denied</html>", True, "other: not JSON (challenge page?)"),
        (503, b"", True, "other: HTTP 503"),
        (None, b"", True, "other: no response"),
    ],
)
def test_verdicts(status: int | None, body: bytes, expect_json: bool, expected: str) -> None:
    assert diag.verdict(status, body, expect_json) == expected


def test_nse_diagnose_rows_and_summary() -> None:
    memory = LocalMemory()
    report = diag.run_site(
        "nse", PC, symbol="hdfcbank", today=TODAY, now=NOW, limiter=None,
        rate_limit_timeout_s=1, memory=memory,
        factories=factories(curl_cffi=blocked_everywhere("curl_cffi"),
                            playwright=MethodUnavailable("Chromium for Playwright is not "
                                                         "installed"),
                            requests=working("requests")),
    )  # fmt: skip
    assert report.methods == ["curl_cffi", "playwright", "requests"]
    curl = [r for r in report.rows if r.method == "curl_cffi"]
    # homepage + filings page + 5 endpoints, every one refused by Akamai
    assert [r.endpoint for r in curl] == ["homepage", "warm-up page 1", "results filing list",
                                          "corporate actions", "shareholding",
                                          "industry (quote)", "board meetings",
                                          "announcements"]  # fmt: skip
    assert all(r.verdict == "blocked-403" and r.server == "AkamaiGHost" for r in curl)
    assert curl[0].cookie_names == ["_abck", "bm_sz"]
    results = curl[2]
    assert results.url.startswith("https://www.nseindia.com/api/corporates-financial-results?")
    assert "symbol=HDFCBANK" in results.url
    pw = [r for r in report.rows if r.method == "playwright"]
    assert len(pw) == 1 and pw[0].verdict == "other: Chromium for Playwright is not installed"
    req = {r.endpoint: r for r in report.rows if r.method == "requests"}
    assert req["shareholding"].verdict == "OK" and req["board meetings"].verdict == "empty"
    assert req["homepage"].cookie_names == ["nsit", "nseappid"]
    assert report.summary["shareholding"] == {"verdict": "OK", "method": "requests"}
    assert report.summary["homepage"] == {"verdict": "OK", "method": "requests"}
    assert report.working_method == "requests"
    # the jobs now try requests first and skip curl_cffi (refused at the warm-up)
    web = WebSession(diag.nse_site(PC.nse), memory=memory)
    assert web.order() == ["requests", "playwright"]


def test_render_never_shows_cookie_values() -> None:
    report = diag.run_site("nse", PC, symbol="HDFCBANK", today=TODAY, now=NOW, limiter=None,
                           rate_limit_timeout_s=1, memory=None,
                           factories=factories(curl_cffi=blocked_everywhere("curl_cffi"),
                                               playwright=blocked_everywhere("playwright"),
                                               requests=working("requests")))  # fmt: skip
    text = diag.render(report)
    blob = json.dumps(report.to_json())
    for secret in ("PLACEHOLDER-SECRET", "Domain=", "HttpOnly"):
        assert secret not in text and secret not in blob
    assert "set-cookie=[_abck, bm_sz]" in text and "set-cookie=[nsit, nseappid]" in text
    assert "blocked-403" in text and "server=AkamaiGHost" in text
    assert "https://www.nseindia.com/" in text
    assert "working method: requests" in text


def test_all_blocked_and_network_errors() -> None:
    report = diag.run_site("nse", PC, symbol="HDFCBANK", today=TODAY, now=NOW, limiter=None,
                           rate_limit_timeout_s=1, memory=None,
                           factories=factories(curl_cffi=blocked_everywhere("curl_cffi"),
                                               playwright=blocked_everywhere("playwright"),
                                               requests=Replay("requests", [
                                                   recorded("nse_home_ok.json")] * 2)))  # fmt: skip
    req = [r for r in report.rows if r.method == "requests" and r.endpoint == "shareholding"]
    assert req[0].verdict == "other: ConnectTimeout" and req[0].status is None
    assert report.working_method is None
    assert report.summary["shareholding"]["verdict"] == "other: ConnectTimeout"
    assert "working method: none: use the manual uploads" in diag.render(report)


def test_bse_diagnose_and_rate_limit() -> None:
    taken: list[object] = []

    class Limiter:
        def acquire(self, provider: object, *, timeout: float) -> None:
            taken.append(provider)

    deny = recorded("bse_403.json")
    report = diag.run_site("bse", PC, symbol="", today=TODAY, now=NOW, limiter=Limiter(),
                           rate_limit_timeout_s=1, memory=None, methods=["requests"],
                           factories=factories(requests=Replay("requests", [
                               recorded("nse_home_ok.json")], default=deny)))  # fmt: skip
    assert [r.endpoint for r in report.rows] == ["homepage", "scrip master", "announcements"]
    assert report.summary["scrip master"]["verdict"] == "blocked-403"
    assert "strPrevDate=20261001" in report.rows[2].url
    assert len(taken) == 3  # every request of a diagnose goes through the limiter


# ───────────────────────── storage, CLI, API ─────────────────────────


@pytest.fixture
def fake_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_site with replayed fetchers (curl_cffi blocked, requests working)."""
    real = diag.run_site

    def run_site(site: str, config: object, **kw: object) -> diag.DiagReport:
        kw["factories"] = factories(curl_cffi=blocked_everywhere("curl_cffi"),
                                    playwright=blocked_everywhere("playwright"),
                                    requests=working("requests"))  # fmt: skip
        kw["limiter"] = None
        return real(site, config, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(diag, "run_site", run_site)


def test_cli_prints_and_stores(fake_sites: None, redis_client: Redis,
                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from app.jobs.cli import main

    class Ctx:
        config = load_config(REPO_CONFIG_DIR)
        redis = redis_client

        def today(self) -> date:
            return TODAY

        def now(self) -> datetime:
            return NOW

    redis_client.delete("diagnose:nse", "websession:nse:method")
    code = main(["nse-diagnose", "--symbol", "TCS"], context_factory=lambda: Ctx())  # type: ignore[arg-type,return-value]
    out = capsys.readouterr().out
    assert code == 0 and "NSE diagnose at" in out and "symbol=TCS" in out
    assert "PLACEHOLDER-SECRET" not in out
    stored = diag.load(redis_client, "nse")
    assert stored is not None and stored["working_method"] == "requests"
    assert redis_client.get("websession:nse:method") == b"requests"
    code = main(["nse-diagnose", "--json", "--method", "curl_cffi"], context_factory=lambda: Ctx())  # type: ignore[arg-type,return-value]
    body = json.loads(capsys.readouterr().out)
    assert code == 1 and body["methods"] == ["curl_cffi"] and body["working_method"] is None
    redis_client.delete("diagnose:nse", "websession:nse:method", "websession:nse:blocked:curl_cffi")


@pytest.fixture
def client(db: object, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient", "diagnose:nse", "diagnose:bse",
                        diag.RUNNING_KEY)  # fmt: skip
    yield from app_client(db)  # type: ignore[arg-type]
    for key in redis_client.scan_iter("websession:*"):
        redis_client.delete(key)
    redis_client.delete("diagnose:nse", "diagnose:bse", diag.RUNNING_KEY)


def test_api_status_and_recheck(fake_sites: None, client: TestClient,
                                redis_client: Redis) -> None:  # fmt: skip
    empty = client.get("/api/data-sources").json()
    api = empty.pop("indianapi")
    assert empty == {"running": False, "nse": None, "bse": None}
    # no INDIANAPI_KEY in the test settings: not configured, and the UI says what to do
    assert api["configured"] is False and api["message"] == "Add INDIANAPI_KEY in .env"
    assert api["budget"] == 500 and api["stop_at"] == 450 and api["used"] == 0
    res = client.post("/api/data-sources/check")
    assert res.status_code == 202
    # TestClient runs background tasks before returning; the flag is cleared afterwards
    body = client.get("/api/data-sources").json()
    assert body["running"] is False
    assert body["nse"]["summary"]["results filing list"] == {"verdict": "OK",
                                                             "method": "requests"}  # fmt: skip
    assert body["bse"]["site"] == "bse"
    assert all("PLACEHOLDER" not in json.dumps(r) for r in body["nse"]["rows"])
    # a check already running: not started twice
    redis_client.set(diag.RUNNING_KEY, "1")
    assert client.post("/api/data-sources/check").json()["running"] is True
    assert data_sources.RUNNING_TTL_S > 0
