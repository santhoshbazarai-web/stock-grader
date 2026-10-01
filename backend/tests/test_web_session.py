"""Browser-like session strategy (web_session.py): method order, fallback, the remembered and
the blocked methods, cookie refresh, rate limiting and the blocked message. Fake fetchers only;
nothing touches the network."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from app.core.config import Provider, SessionMethod, load_config
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.providers.web_session import (
    BLOCKED_MARKER,
    LocalMemory,
    MethodUnavailable,
    SiteBlocked,
    SiteProfile,
    TransportError,
    WebResponse,
    WebSession,
    blocked_message,
)
from tests.conftest import REPO_CONFIG_DIR

NSE = load_config(REPO_CONFIG_DIR).providers.nse
SITE = SiteProfile("nse", "NSE", Provider.NSE, tuple(NSE.session), NSE.browser,
                   NSE.request_timeout_s, NSE.cookie_ttl_s)  # fmt: skip
API = "https://www.nseindia.com/api/corporate-share-holdings-master"
OK_JSON = json.dumps([{"symbol": "HDFCBANK"}]).encode()


def resp(status: int, body: bytes = OK_JSON, method: str = "") -> WebResponse:
    return WebResponse(API, status, {"content-type": "application/json"}, body, [], method)


@dataclass
class Fake:
    """A fetcher whose warm-up and API answers are scripted (the last answer repeats)."""

    method: SessionMethod
    warm: list[int] = field(default_factory=lambda: [200])
    api: list[WebResponse] = field(default_factory=lambda: [resp(200)])
    log: list[str] = field(default_factory=list)

    def warm_up(self) -> list[WebResponse]:
        self.log.append(f"{self.method}:warm")
        status = self.warm.pop(0) if len(self.warm) > 1 else self.warm[0]
        return [resp(status, b"<html/>") for _ in SITE.config.warmup_urls]

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
        self.log.append(f"{self.method}:get")
        r = self.api.pop(0) if len(self.api) > 1 else self.api[0]
        return WebResponse(r.url, r.status_code, r.headers, r.content, r.cookie_names,
                           self.method)  # fmt: skip

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Counter:
    calls: list[Provider] = field(default_factory=list)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        self.calls.append(provider)


def session(fakes: dict[SessionMethod, Fake | Exception], *, memory: LocalMemory | None = None,
            clock: Clock | None = None, limiter: Counter | None = None) -> WebSession:  # fmt: skip
    def factory(m: SessionMethod) -> Callable[[SiteProfile], Fake]:
        def build(site: SiteProfile) -> Fake:
            f = fakes[m]
            if isinstance(f, Exception):
                raise f
            return f

        return build

    clock = clock or Clock()
    return WebSession(SITE, memory=memory or LocalMemory(clock), limiter=limiter,
                      factories={m: factory(m) for m in fakes}, clock=clock,
                      methods=list(fakes))  # fmt: skip


def test_default_order_from_config() -> None:
    assert NSE.session == ["curl_cffi", "playwright", "requests"]
    assert session({m: Fake(m) for m in NSE.session}).order() == NSE.session


def test_first_method_that_works_is_used_and_remembered() -> None:
    memory = LocalMemory(Clock())
    curl = Fake("curl_cffi", warm=[403])
    pw = Fake("playwright")
    req = Fake("requests")
    s = session({"curl_cffi": curl, "playwright": pw, "requests": req}, memory=memory)
    r = s.fetch(API)
    assert r is not None and r.method == "playwright" and r.json() == [{"symbol": "HDFCBANK"}]
    assert curl.log == ["curl_cffi:warm"] and req.log == []
    assert s.remembered() == "playwright" and s.blocked("curl_cffi") == "warm-up HTTP 403"
    # a new session (another process) starts with the remembered method, skips the blocked one
    s2 = session({"curl_cffi": Fake("curl_cffi"), "playwright": Fake("playwright"),
                  "requests": Fake("requests")}, memory=memory)  # fmt: skip
    assert s2.order() == ["playwright", "requests"]
    assert s2.fetch(API).method == "playwright"  # type: ignore[union-attr]


def test_blocked_method_is_retried_after_its_ttl() -> None:
    clock = Clock()
    memory = LocalMemory(clock)
    s = session({"curl_cffi": Fake("curl_cffi", warm=[403]), "requests": Fake("requests")},
                memory=memory, clock=clock)  # fmt: skip
    s.fetch(API)
    assert s.order() == ["requests"]
    clock.t += NSE.browser.blocked_ttl_s + 1
    assert s.order() == ["requests", "curl_cffi"]  # remembered first, then the others again


def test_forbidden_api_refreshes_cookies_once_then_falls_through() -> None:
    curl = Fake("curl_cffi", api=[resp(403), resp(200)])
    s = session({"curl_cffi": curl, "requests": Fake("requests")})
    assert s.fetch(API).method == "curl_cffi"  # type: ignore[union-attr]
    assert curl.log == ["curl_cffi:warm", "curl_cffi:get", "curl_cffi:warm", "curl_cffi:get"]

    curl2 = Fake("curl_cffi", api=[resp(403)])
    req = Fake("requests")
    s2 = session({"curl_cffi": curl2, "requests": req})
    assert s2.fetch(API).method == "requests"  # type: ignore[union-attr]
    assert s2.blocked("curl_cffi") == "HTTP 403"


def test_challenge_page_instead_of_json_counts_as_blocked() -> None:
    curl = Fake("curl_cffi", api=[resp(200, b"<html>Access Denied</html>")])
    s = session({"curl_cffi": curl, "requests": Fake("requests")})
    assert s.fetch(API).method == "requests"  # type: ignore[union-attr]
    assert "not JSON" in (s.blocked("curl_cffi") or "")
    # a plain document (expect_json=False) is fine as HTML
    s2 = session({"curl_cffi": Fake("curl_cffi", api=[resp(200, b"<html/>")])})
    assert s2.fetch(API, expect_json=False).method == "curl_cffi"  # type: ignore[union-attr]


def test_missing_library_falls_through() -> None:
    s = session({"curl_cffi": MethodUnavailable("curl_cffi is not installed"),
                 "playwright": MethodUnavailable("Chromium for Playwright is not installed"),
                 "requests": Fake("requests")})  # fmt: skip
    assert s.fetch(API).method == "requests"  # type: ignore[union-attr]
    assert s.blocked("playwright") == "Chromium for Playwright is not installed"


def test_all_methods_blocked_raises_a_clear_unavailable() -> None:
    memory = LocalMemory(Clock())
    s = session({"curl_cffi": Fake("curl_cffi", warm=[403]),
                 "playwright": Fake("playwright", api=[resp(403)]),
                 "requests": Fake("requests", api=[resp(401)])}, memory=memory)  # fmt: skip
    with pytest.raises(SiteBlocked) as info:
        s.fetch(API)
    msg = str(info.value)
    assert isinstance(info.value, ProviderUnavailable)  # the router does not retry it
    assert msg.startswith(blocked_message("NSE")) and BLOCKED_MARKER in msg
    assert "Upload XBRL files or a Screener export instead" in msg
    assert "curl_cffi: warm-up HTTP 403" in msg and "playwright: HTTP 403" in msg
    assert "requests: HTTP 401" in msg
    # next call: nothing is tried again until the blocks expire (no network at all)
    calls = Fake("requests")
    s2 = session({"curl_cffi": Fake("curl_cffi"), "playwright": Fake("playwright"),
                  "requests": calls}, memory=memory)  # fmt: skip
    with pytest.raises(SiteBlocked, match="recently"):
        s2.fetch(API)
    assert calls.log == []


def test_transient_errors_are_retryable_not_blocks() -> None:
    s = session({"curl_cffi": Fake("curl_cffi", api=[resp(429)]), "requests": Fake("requests")})
    with pytest.raises(ProviderError, match="429") as info:
        s.fetch(API)
    assert not isinstance(info.value, ProviderUnavailable)
    assert s.blocked("curl_cffi") is None  # rate-limited is not blocked

    s2 = session({"curl_cffi": Fake("curl_cffi", warm=[503])})
    with pytest.raises(ProviderError, match="warm-up: HTTP 503"):
        s2.fetch(API)

    class Broken(Fake):
        def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
            raise TransportError("ConnectTimeout")

    s3 = session({"curl_cffi": Broken("curl_cffi")})
    with pytest.raises(
        ProviderError, match=r"unreachable \(curl_cffi: network error ConnectTimeout"
    ) as e3:
        s3.fetch(API)
    assert not isinstance(e3.value, ProviderUnavailable)  # an outage is not a block


def test_network_error_moves_on_without_blocking() -> None:
    class Reset(Fake):
        def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
            raise TransportError("ConnectionError")

    s = session({"curl_cffi": Reset("curl_cffi"), "requests": Fake("requests")})
    assert s.fetch(API).method == "requests"  # type: ignore[union-attr]
    assert s.blocked("curl_cffi") is None and s.remembered() == "requests"
    # refused by one method, unreachable with the other: still reported as blocking
    s2 = session({"curl_cffi": Reset("curl_cffi"), "requests": Fake("requests", api=[resp(403)])})
    with pytest.raises(SiteBlocked, match="requests: HTTP 403; curl_cffi: network error"):
        s2.fetch(API)


def test_not_found_is_none() -> None:
    assert session({"curl_cffi": Fake("curl_cffi", api=[resp(404, b"")])}).fetch(API) is None


def test_cookies_reused_until_ttl() -> None:
    clock = Clock()
    curl = Fake("curl_cffi")
    s = session({"curl_cffi": curl}, clock=clock)
    s.fetch(API)
    s.fetch(API)
    assert curl.log.count("curl_cffi:warm") == 1
    clock.t += NSE.cookie_ttl_s + 1
    s.fetch(API)
    assert curl.log.count("curl_cffi:warm") == 2


def test_every_request_after_the_first_takes_a_token() -> None:
    limiter = Counter()
    s = session({"curl_cffi": Fake("curl_cffi")}, limiter=limiter)
    s.begin_call()
    s.fetch(API)  # warm-up pages + the API call; the router paid for the first
    assert len(limiter.calls) == len(NSE.browser.warmup_urls)
    s.begin_call()
    s.fetch(API)  # warm cookies: one request, paid by the router
    assert len(limiter.calls) == len(NSE.browser.warmup_urls)


def test_api_headers_are_browser_like() -> None:
    h = SITE.api_headers(API)
    assert h["Referer"] == NSE.browser.api_referer
    assert h["sec-fetch-site"] == "same-origin" and "Origin" not in h
    bse = load_config(REPO_CONFIG_DIR).providers.bse
    site = SiteProfile("bse", "BSE", Provider.BSE, tuple(bse.session), bse.browser, 30, 300)
    h2 = site.api_headers("https://api.bseindia.com/BseIndiaAPI/api/x")
    assert h2["sec-fetch-site"] == "same-site" and h2["Origin"] == "https://www.bseindia.com"
    nav = SiteProfile.nav_headers(first=True)
    assert nav["sec-fetch-mode"] == "navigate" and nav["sec-fetch-site"] == "none"
