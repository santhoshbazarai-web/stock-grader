"""Browser-like sessions for exchange websites that block plain HTTP clients (SPEC §3.2a).

NSE's www.nseindia.com (and sometimes BSE) answer 403 to clients that don't look like a
browser: the TLS fingerprint, the header set and the cookies a homepage visit sets all count.
:class:`WebSession` tries the **methods** configured in ``providers.yaml`` (``nse.session``,
``bse.session``) in order until one gets a real answer:

- ``curl_cffi``: libcurl with a Chrome TLS / HTTP2 fingerprint (``impersonate``), a persistent
  cookie jar and the header set Chrome sends for a page navigation and for the page's API
  calls (sec-fetch-*, sec-ch-ua*, Referer, Origin where cross-site);
- ``playwright``: headless Chromium opens the warm-up pages and waits for the network to settle;
  its cookies and user agent are copied into an HTTP session for the API calls. If the API
  still refuses that session, the calls are made with ``fetch`` from inside the page itself;
- ``requests``: plain ``requests`` with browser headers (the original behaviour).

The method that last worked is remembered (``remember_ttl_s``) and tried first; a method that
was blocked is skipped for ``blocked_ttl_s``, so a blocked connection is not retried on every
call. When every method is blocked the call raises :class:`SiteBlocked` (a
``ProviderUnavailable``) whose message starts with :func:`blocked_message`, which the UI shows
with a link to the manual uploads. Every request (warm-up pages included) takes a token from
the shared Redis rate limiter, except a call's first (the router pays for that one).

Exchanges change their protection without notice: this can stop working, and the manual
upload routes (results XBRL files, Screener export) remain the fallback.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

import requests

from app.core.config import BrowserSessionConfig, Provider, SessionMethod
from app.core.rate_limiter import Limiter
from app.data.providers.base import ProviderError, ProviderUnavailable

logger = logging.getLogger(__name__)

METHODS: tuple[SessionMethod, ...] = ("curl_cffi", "playwright", "requests")

# The header set of the plain-requests method (and the user agent it sends).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,text/csv,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
NAV_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
API_ACCEPT = "application/json, text/plain, */*"
ACCEPT_LANGUAGE = "en-US,en;q=0.9,en-IN;q=0.8"


def blocked_message(site_label: str) -> str:
    """The sentence the UI looks for (``is blocking automated access``) and shows."""
    return (
        f"{site_label} is blocking automated access from this connection. Upload XBRL files "
        "or a Screener export instead"
    )


BLOCKED_MARKER = "is blocking automated access"


class MethodUnavailable(Exception):
    """The method cannot run here (library not installed, browser not installed)."""


class TransportError(Exception):
    """Network-level failure (DNS, TLS, timeout, connection reset)."""


class SiteBlocked(ProviderUnavailable):
    """Every configured method was refused by the site (or is marked blocked)."""


@dataclass
class WebResponse:
    url: str
    status_code: int
    headers: dict[str, str]  # lower-case names
    content: bytes
    cookie_names: list[str] = field(default_factory=list)  # set by this response; never values
    method: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)

    def is_json(self) -> bool:
        try:
            self.json()
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class SiteProfile:
    """One website: where to warm up, what the API calls look like."""

    key: str  # "nse" / "bse": rate-limit bucket and memory keys
    label: str  # "NSE" / "BSE" in messages
    provider: Provider
    methods: tuple[SessionMethod, ...]  # providers.yaml <site>.session, tried in order
    config: BrowserSessionConfig  # providers.yaml <site>.browser
    timeout_s: float
    cookie_ttl_s: float

    @property
    def api_referer(self) -> str:
        return self.config.api_referer

    def sec_fetch_site(self, url: str) -> str:
        """same-origin / same-site / cross-site of an API call made from the referer page."""
        a, b = urlsplit(self.config.api_referer), urlsplit(url)
        if (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port):
            return "same-origin"
        ha, hb = (a.hostname or "").split("."), (b.hostname or "").split(".")
        return "same-site" if ha[-2:] == hb[-2:] else "cross-site"

    def api_headers(self, url: str) -> dict[str, str]:
        site = self.sec_fetch_site(url)
        headers = {
            "Accept": API_ACCEPT,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Referer": self.config.api_referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": site,
        }
        if site != "same-origin":
            ref = urlsplit(self.config.api_referer)
            headers["Origin"] = f"{ref.scheme}://{ref.netloc}"
        return headers

    @staticmethod
    def nav_headers(first: bool) -> dict[str, str]:
        return {
            "Accept": NAV_ACCEPT,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Upgrade-Insecure-Requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none" if first else "same-origin",
            "sec-fetch-user": "?1",
        }


class Fetcher(Protocol):
    """One method of talking to a site."""

    method: SessionMethod

    def warm_up(self) -> list[WebResponse]:
        """Visit the warm-up pages in order (cookies); one response per page."""
        ...

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse: ...

    def close(self) -> None: ...


def _full_url(url: str, params: dict[str, str] | None) -> str:
    return f"{url}?{urlencode(params)}" if params else url


def _cookie_names(set_cookie_values: list[str]) -> list[str]:
    names = []
    for value in set_cookie_values:
        name = value.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


# ───────────────────────── requests ─────────────────────────


class RequestsFetcher:
    method: SessionMethod = "requests"

    def __init__(self, site: SiteProfile, session: requests.Session | None = None) -> None:
        self._site = site
        self._session = session or requests.Session()
        self._session.headers.update({**BROWSER_HEADERS, "Referer": site.api_referer})

    def _do(self, url: str, params: dict[str, str] | None, headers: dict[str, str]) -> WebResponse:
        try:
            resp = self._session.get(url, params=params, headers=headers,
                                     timeout=self._site.timeout_s)  # fmt: skip
        except requests.RequestException as exc:
            raise TransportError(type(exc).__name__) from exc
        raw = getattr(getattr(resp, "raw", None), "headers", None)
        setc = raw.getlist("Set-Cookie") if raw is not None and hasattr(raw, "getlist") else []
        names = _cookie_names(setc) or list(resp.cookies.keys())
        return WebResponse(resp.url or url, resp.status_code,
                           {k.lower(): v for k, v in resp.headers.items()}, resp.content,
                           names, self.method)  # fmt: skip

    def warm_up(self) -> list[WebResponse]:
        return [self._do(u, None, {}) for u in self._site.config.warmup_urls]

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
        return self._do(url, params, {"Referer": self._site.api_referer} if api else {})

    def close(self) -> None:
        self._session.close()


# ───────────────────────── curl_cffi ─────────────────────────


class CurlCffiFetcher:
    method: SessionMethod = "curl_cffi"

    def __init__(self, site: SiteProfile) -> None:
        try:
            from curl_cffi import requests as cr
        except ImportError as exc:  # pragma: no cover - installed in the image
            raise MethodUnavailable("curl_cffi is not installed") from exc
        self._cr = cr
        self._site = site
        # impersonate sets the User-Agent, sec-ch-ua* and Accept-Encoding of that Chrome
        # build, consistent with its TLS fingerprint; only the per-request headers are added.
        self._session: Any = cr.Session(impersonate=site.config.impersonate)

    def _do(self, url: str, params: dict[str, str] | None, headers: dict[str, str]) -> WebResponse:
        try:
            resp = self._session.get(url, params=params, headers=headers,
                                     timeout=self._site.timeout_s)  # fmt: skip
        except Exception as exc:  # curl_cffi raises its own RequestException tree
            raise TransportError(type(exc).__name__) from exc
        setc = list(resp.headers.get_list("set-cookie")) if hasattr(resp.headers, "get_list") \
            else []  # fmt: skip
        return WebResponse(str(resp.url or url), resp.status_code,
                           {k.lower(): v for k, v in resp.headers.items()}, resp.content,
                           _cookie_names(setc), self.method)  # fmt: skip

    def warm_up(self) -> list[WebResponse]:
        out = []
        for i, u in enumerate(self._site.config.warmup_urls):
            headers = SiteProfile.nav_headers(first=i == 0)
            if i > 0:
                headers["Referer"] = self._site.config.warmup_urls[i - 1]
            out.append(self._do(u, None, headers))
        return out

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
        headers = self._site.api_headers(url) if api else SiteProfile.nav_headers(first=False)
        return self._do(url, params, headers)

    def close(self) -> None:
        self._session.close()


# ───────────────────────── playwright ─────────────────────────

_PLAYWRIGHT_LOCK = threading.Lock()  # one browser at a time per process
_FETCH_JS = """async ([url, headers]) => {
  const r = await fetch(url, {credentials: "include", headers});
  const body = await r.text();
  const h = {};
  r.headers.forEach((v, k) => { h[k] = v; });
  return {status: r.status, headers: h, body, url: r.url};
}"""


class PlaywrightFetcher:
    """Headless Chromium for the warm-up; API calls with the browser's cookies and user agent
    from an HTTP session, or (``mode == "page"``) with fetch() inside the page."""

    method: SessionMethod = "playwright"

    def __init__(self, site: SiteProfile) -> None:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError as exc:  # pragma: no cover - installed in the image
            raise MethodUnavailable("playwright is not installed") from exc
        self._site = site
        self._cookies: list[dict[str, Any]] = []
        self._ua: str | None = None
        self._session: requests.Session | None = None
        self.mode: Literal["session", "page"] = "session"

    def _browser_run(self, work: Callable[[Any], Any]) -> Any:
        """Start Chromium, run ``work(page)``, keep its cookies and UA, close everything (no
        browser outlives a call, so no thread holds Playwright objects)."""
        from playwright.sync_api import Error as PwError
        from playwright.sync_api import sync_playwright

        cfg = self._site.config
        with _PLAYWRIGHT_LOCK:
            try:
                with sync_playwright() as pw:
                    # PLAYWRIGHT_CHROMIUM_EXECUTABLE: use an existing Chromium (e.g. a system
                    # one) instead of the browser `playwright install` downloaded
                    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
                    browser = pw.chromium.launch(headless=True, executable_path=exe)
                    try:
                        probe = browser.new_page()
                        ua = probe.evaluate("navigator.userAgent").replace("HeadlessChrome",
                                                                          "Chrome")  # fmt: skip
                        probe.close()
                        size = {"width": 1366, "height": 768}
                        context = browser.new_context(user_agent=ua, locale="en-US", viewport=size)  # type: ignore[arg-type]
                        if self._cookies:
                            context.add_cookies(self._cookies)  # type: ignore[arg-type]
                        if cfg.playwright_block_resources:
                            blocked = set(cfg.playwright_block_resources)
                            context.route("**/*", lambda route: route.abort()
                                          if route.request.resource_type in blocked
                                          else route.continue_())  # fmt: skip
                        page = context.new_page()
                        page.set_default_timeout(cfg.playwright_timeout_s * 1000)
                        result = work(page)
                        self._cookies = context.cookies()  # type: ignore[assignment]
                        self._ua = ua
                        return result
                    finally:
                        browser.close()
            except PwError as exc:
                text = str(exc).splitlines()[0][:200]
                if "Executable doesn't exist" in text or "playwright install" in text:
                    raise MethodUnavailable("Chromium for Playwright is not installed") from exc
                raise TransportError(f"playwright: {text}") from exc

    def warm_up(self) -> list[WebResponse]:
        def visit(page: Any) -> list[WebResponse]:
            out = []
            for u in self._site.config.warmup_urls:
                resp = page.goto(u, wait_until="networkidle")
                if resp is None:
                    raise TransportError(f"playwright: no response for {u}")
                setc = [v for k, v in ((h["name"], h["value"]) for h in resp.headers_array())
                        if k.lower() == "set-cookie"]  # fmt: skip
                out.append(WebResponse(u, resp.status, {k.lower(): v for k, v in
                                                        resp.headers.items()},
                                       resp.body(), _cookie_names(setc), self.method))  # fmt: skip
            return out

        responses = self._browser_run(visit)
        self._session = None  # rebuilt from the fresh cookies
        return list(responses)

    def _cookie_session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self._ua or BROWSER_HEADERS["User-Agent"],
                              "Accept-Encoding": "gzip, deflate"})  # fmt: skip
            for c in self._cookies:
                s.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                              path=c.get("path", "/"))  # fmt: skip
            self._session = s
        return self._session

    def _page_fetch(self, url: str) -> WebResponse:
        site = self._site

        def fetch(page: Any) -> dict[str, Any]:
            page.goto(site.api_referer, wait_until="networkidle")
            headers = {"Accept": API_ACCEPT}
            return dict(page.evaluate(_FETCH_JS, [url, headers]))

        r = self._browser_run(fetch)
        return WebResponse(r.get("url") or url, int(r["status"]), dict(r["headers"]),
                           str(r["body"]).encode(), [], self.method)  # fmt: skip

    def get(self, url: str, params: dict[str, str] | None, *, api: bool) -> WebResponse:
        full = _full_url(url, params)
        if self.mode == "page" and api:
            return self._page_fetch(full)
        headers = self._site.api_headers(url) if api else {}
        try:
            resp = self._cookie_session().get(full, headers=headers, timeout=self._site.timeout_s)
        except requests.RequestException as exc:
            raise TransportError(type(exc).__name__) from exc
        out = WebResponse(resp.url or full, resp.status_code,
                          {k.lower(): v for k, v in resp.headers.items()}, resp.content,
                          list(resp.cookies.keys()), self.method)  # fmt: skip
        if api and (out.status_code in (401, 403) or (out.status_code == 200
                                                      and not out.is_json())):  # fmt: skip
            # the site wants the call made by the page itself
            page = self._page_fetch(full)
            if page.status_code == 200:
                self.mode = "page"
            return page
        return out

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


FetcherFactory = Callable[[SiteProfile], Fetcher]
FACTORIES: dict[SessionMethod, FetcherFactory] = {
    "curl_cffi": CurlCffiFetcher,
    "playwright": PlaywrightFetcher,
    "requests": RequestsFetcher,
}


# ───────────────────────── memory ─────────────────────────


class MethodMemory(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_s: float) -> None: ...

    def delete(self, key: str) -> None: ...


class LocalMemory:
    """In-process memory (tests, and providers built without Redis)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        hit = self._data.get(key)
        if hit is None or hit[1] <= self._clock():
            return None
        return hit[0]

    def set(self, key: str, value: str, ttl_s: float) -> None:
        self._data[key] = (value, self._clock() + ttl_s)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RedisMemory:
    def __init__(self, redis: Any) -> None:
        self._r = redis

    def get(self, key: str) -> str | None:
        v = self._r.get(key)
        return v.decode() if isinstance(v, bytes) else v

    def set(self, key: str, value: str, ttl_s: float) -> None:
        self._r.set(key, value, ex=max(1, int(ttl_s)))

    def delete(self, key: str) -> None:
        self._r.delete(key)


# ───────────────────────── the strategy ─────────────────────────


def _refused(resp: WebResponse, expect_json: bool) -> bool:
    return resp.status_code in (401, 403) or (
        expect_json and resp.status_code == 200 and not resp.is_json()
    )


class WebSession:
    """Tries the configured methods in order (the remembered one first); see module doc."""

    def __init__(
        self,
        site: SiteProfile,
        *,
        memory: MethodMemory | None = None,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
        factories: dict[SessionMethod, FetcherFactory] | None = None,
        clock: Callable[[], float] = time.monotonic,
        methods: list[SessionMethod] | None = None,
    ) -> None:
        self.site = site
        self.methods: list[SessionMethod] = list(methods or site.methods)
        self._memory = memory or LocalMemory()
        self._limiter = limiter
        self._rl_timeout = rate_limit_timeout_s
        self._factories = factories or FACTORIES
        self._clock = clock
        self._fetchers: dict[SessionMethod, Fetcher] = {}
        self._warmed_at: dict[SessionMethod, float] = {}
        self._requests = 0
        self._lock = threading.RLock()

    # ───────────── memory ─────────────

    def _key(self, *parts: str) -> str:
        return ":".join(("websession", self.site.key, *parts))

    def remembered(self) -> SessionMethod | None:
        m = self._memory.get(self._key("method"))
        return m if m in self.methods else None

    def blocked(self, method: SessionMethod) -> str | None:
        return self._memory.get(self._key("blocked", method))

    def mark_blocked(self, method: SessionMethod, why: str) -> None:
        logger.info("%s via %s blocked: %s", self.site.label, method, why)
        self._memory.set(self._key("blocked", method), why, self.site.config.blocked_ttl_s)
        if self.remembered() == method:
            self._memory.delete(self._key("method"))
        self._fetchers.pop(method, None)
        self._warmed_at.pop(method, None)

    def remember(self, method: SessionMethod) -> None:
        self._memory.set(self._key("method"), method, self.site.config.remember_ttl_s)
        self._memory.delete(self._key("blocked", method))

    def order(self) -> list[SessionMethod]:
        """Methods to try now: the remembered one first, then the rest in configured order,
        leaving out those marked blocked."""
        first = self.remembered()
        ordered = ([first] if first else []) + [m for m in self.methods if m != first]
        return [m for m in ordered if not self.blocked(m)]

    # ───────────── requests ─────────────

    def begin_call(self) -> None:
        """A new provider call: its first request is paid for by the router."""
        self._requests = 0

    def take(self) -> None:
        """One request about to go out: a rate-limit token unless it is the call's first."""
        self._take()

    def _take(self) -> None:
        if self._requests > 0 and self._limiter is not None:
            self._limiter.acquire(self.site.provider, timeout=self._rl_timeout)
        self._requests += 1

    def fetcher(self, method: SessionMethod) -> Fetcher:
        if method not in self._fetchers:
            self._fetchers[method] = self._factories[method](self.site)
        return self._fetchers[method]

    def _stale(self, method: SessionMethod) -> bool:
        at = self._warmed_at.get(method)
        return at is None or self._clock() - at > self.site.cookie_ttl_s

    def warm_up(self, method: SessionMethod) -> list[WebResponse]:
        """Visit the warm-up pages with ``method``: one rate-limit token per page."""
        fetcher = self.fetcher(method)
        for _ in self.site.config.warmup_urls:
            self._take()
        pages = fetcher.warm_up()
        self._warmed_at[method] = self._clock()
        return pages

    def send(self, method: SessionMethod, url: str, params: dict[str, str] | None, *,
             api: bool) -> WebResponse:  # fmt: skip
        self._take()
        return self.fetcher(method).get(url, params, api=api)

    def fetch(self, url: str, params: dict[str, str] | None = None, *,
              expect_json: bool = True) -> WebResponse | None:  # fmt: skip
        """A site API call (needs the warm-up cookies). ``None`` for 404. Raises
        :class:`SiteBlocked` when the methods are refused (401/403, a challenge page instead of
        JSON) or marked blocked, and ``ProviderError`` for 429, 5xx, and when every method only
        failed at the network level (connection reset, timeout: the router retries those; a
        connection error moves on to the next method but does not mark one blocked, since it
        can be an outage)."""
        with self._lock:
            tried: list[str] = []
            network: list[str] = []
            for method in self.order():
                outcome = self._try(method, url, params, expect_json, tried, network)
                if outcome is not _NEXT:
                    return outcome  # type: ignore[return-value]
            skipped = [f"{m}: {self.blocked(m)} (recently)" for m in self.methods
                       if m not in {t.split(":")[0] for t in tried + network}
                       and self.blocked(m)]  # fmt: skip
            if network and not tried and not skipped:
                raise ProviderError(f"{self.site.label} {url}: unreachable ("
                                    + "; ".join(network) + ")")  # fmt: skip
            detail = "; ".join(tried + network + skipped) or "no methods"
            raise SiteBlocked(f"{blocked_message(self.site.label)} ({detail}).")

    def _try(self, method: SessionMethod, url: str, params: dict[str, str] | None,
             expect_json: bool, tried: list[str], network: list[str]) -> object:  # fmt: skip
        try:
            if self._stale(method):
                pages = self.warm_up(method)
                bad = next((p for p in pages if p.status_code >= 400), None)
                if bad is not None:
                    if bad.status_code == 429:
                        raise ProviderError(f"{self.site.label} warm-up: HTTP 429 (rate-limited)")
                    if bad.status_code in (401, 403):
                        why = f"warm-up HTTP {bad.status_code}"
                        tried.append(f"{method}: {why}")
                        self.mark_blocked(method, why)
                        return _NEXT
                    raise ProviderError(f"{self.site.label} warm-up: HTTP {bad.status_code}")
            resp = self.send(method, url, params, api=True)
            if _refused(resp, expect_json):
                logger.info("%s %s via %s → HTTP %s; refreshing cookies once",
                            self.site.label, url, method, resp.status_code)  # fmt: skip
                self.warm_up(method)
                resp = self.send(method, url, params, api=True)
            if _refused(resp, expect_json):
                why = f"HTTP {resp.status_code}" + ("" if resp.status_code != 200
                                                   else " (not JSON: challenge page)")  # fmt: skip
                tried.append(f"{method}: {why}")
                self.mark_blocked(method, why)
                return _NEXT
        except MethodUnavailable as exc:
            tried.append(f"{method}: {exc}")
            self.mark_blocked(method, str(exc))
            return _NEXT
        except TransportError as exc:
            logger.info("%s %s via %s: network error %s", self.site.label, url, method, exc)
            network.append(f"{method}: network error {exc}")
            self._warmed_at.pop(method, None)
            return _NEXT
        self.remember(method)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            raise ProviderError(f"{self.site.label} {url}: HTTP 429 (rate-limited)")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.site.label} {url}: HTTP {resp.status_code}")
        return resp

    def close(self) -> None:
        for f in self._fetchers.values():
            f.close()
        self._fetchers.clear()


_NEXT = object()
