"""``nse-diagnose`` / ``bse-diagnose``: which exchange endpoints answer this connection, and how.

For every configured session method (``nse.session`` / ``bse.session``) the warm-up pages and
each endpoint the app uses are requested once, in order, through the shared rate limiter. One
row per request: URL, method, HTTP status, ``server`` and ``content-type``, the **names** of
the cookies the response sets (never their values), body length and a verdict:

- ``OK``: a 2xx answer with content (JSON where JSON is expected);
- ``blocked-403`` / ``blocked-401``: refused;
- ``rate-limited-429``;
- ``empty``: 2xx without content, or JSON with no rows;
- ``other: …``: anything else (another status, an HTML challenge page instead of JSON, a
  network error, a method that can't run here).

The result is stored in Redis for the Settings → Data sources card, and the first method
that answered every endpoint is remembered for the jobs (a method refused at the warm-up is
marked blocked), exactly as a normal call would.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.core.config import BseConfig, NseConfig, Provider, SessionMethod
from app.core.rate_limiter import Limiter
from app.data.providers.bse import bse_site
from app.data.providers.nse import CORPORATE_ACTIONS_PATH, SHAREHOLDING_PATH, nse_site
from app.data.providers.web_session import (
    FACTORIES,
    FetcherFactory,
    MethodMemory,
    MethodUnavailable,
    SiteProfile,
    TransportError,
    WebResponse,
    WebSession,
)

REDIS_KEY = "diagnose:{site}"
RUNNING_KEY = "diagnose:running"


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    params: dict[str, str] | None = None
    expect_json: bool = True

    def full_url(self) -> str:
        from urllib.parse import urlencode

        return f"{self.url}?{urlencode(self.params)}" if self.params else self.url


@dataclass
class DiagRow:
    site: str
    method: str
    endpoint: str
    url: str
    status: int | None
    server: str | None
    content_type: str | None
    cookie_names: list[str]
    length: int
    verdict: str


@dataclass
class DiagReport:
    site: str
    checked_at: str
    methods: list[str]
    rows: list[DiagRow] = field(default_factory=list)
    # endpoint → {"verdict", "method"}: OK with the first method that got it, else the last
    # verdict seen
    summary: dict[str, dict[str, str]] = field(default_factory=dict)
    working_method: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ───────────────────────── endpoints ─────────────────────────


def nse_endpoints(cfg: NseConfig, symbol: str, today: date) -> list[Endpoint]:
    sym = symbol.strip().upper()
    window = {"index": "equities", "from_date": f"{today - timedelta(days=7):%d-%m-%Y}",
              "to_date": f"{today:%d-%m-%Y}"}  # fmt: skip
    return [
        Endpoint("results filing list", f"{cfg.base_url}{cfg.results.index_path}",
                 {"index": "equities", "symbol": sym, "period": cfg.results.periods[0]}),
        Endpoint("corporate actions", f"{cfg.base_url}{CORPORATE_ACTIONS_PATH}",
                 {"index": "equities", "symbol": sym}),
        Endpoint("shareholding", f"{cfg.base_url}{SHAREHOLDING_PATH}",
                 {"index": "equities", "symbol": sym}),
        Endpoint("industry (quote)", f"{cfg.base_url}{cfg.quote_path}", {"symbol": sym}),
        Endpoint("board meetings", f"{cfg.base_url}{cfg.events.board_meetings_path}", window),
        Endpoint("announcements", f"{cfg.base_url}{cfg.events.announcements_path}", window),
    ]  # fmt: skip


def bse_endpoints(cfg: BseConfig, today: date) -> list[Endpoint]:
    return [
        Endpoint("scrip master", f"{cfg.api_url}{cfg.scrip_master_path}"),
        Endpoint("announcements", f"{cfg.api_url}{cfg.announcements_path}",
                 {"pageno": "1", "strCat": "-1", "strPrevDate": f"{today:%Y%m%d}",
                  "strScrip": "", "strSearch": "P", "strToDate": f"{today:%Y%m%d}",
                  "strType": "C", "subcategory": "-1"}),
    ]  # fmt: skip


# ───────────────────────── verdicts (pure) ─────────────────────────


def _no_rows(payload: Any) -> bool:
    if payload in (None, [], {}, ""):
        return True
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("Table"))
        return isinstance(rows, list) and not rows
    return False


def verdict(status: int | None, content: bytes, expect_json: bool) -> str:
    if status is None:
        return "other: no response"
    if status in (401, 403):
        return f"blocked-{status}"
    if status == 429:
        return "rate-limited-429"
    if not 200 <= status < 300:
        return f"other: HTTP {status}"
    if not content.strip():
        return "empty"
    if expect_json:
        try:
            payload = json.loads(content)
        except ValueError:
            return "other: not JSON (challenge page?)"
        if _no_rows(payload):
            return "empty"
    return "OK"


def _row(site: str, method: str, name: str, url: str, r: WebResponse | None,
         expect_json: bool, failure: str | None = None) -> DiagRow:  # fmt: skip
    if r is None:
        return DiagRow(site, method, name, url, None, None, None, [], 0,
                       f"other: {failure}" if failure else "other: no response")  # fmt: skip
    return DiagRow(site, method, name, url, r.status_code, r.headers.get("server"),
                   r.headers.get("content-type"), list(r.cookie_names), len(r.content),
                   verdict(r.status_code, r.content, expect_json))  # fmt: skip


# ───────────────────────── run ─────────────────────────


def diagnose(
    site: SiteProfile,
    endpoints: list[Endpoint],
    *,
    methods: list[SessionMethod] | None = None,
    factories: dict[SessionMethod, FetcherFactory] | None = None,
    limiter: Limiter | None = None,
    rate_limit_timeout_s: float = 60.0,
    memory: MethodMemory | None = None,
    now: datetime,
) -> DiagReport:
    """Request every endpoint with each method; see the module docstring."""
    use = list(methods or site.methods)
    factories = factories or FACTORIES
    report = DiagReport(site.key, now.isoformat(), [str(m) for m in use])

    def take() -> None:
        if limiter is not None:
            limiter.acquire(site.provider, timeout=rate_limit_timeout_s)

    for method in use:
        try:
            fetcher = factories[method](site)
        except MethodUnavailable as exc:
            report.rows.append(DiagRow(site.key, method, "session", "", None, None, None, [],
                                       0, f"other: {exc}"))  # fmt: skip
            continue
        try:
            for _ in site.config.warmup_urls:
                take()
            try:
                pages = fetcher.warm_up()
            except (TransportError, MethodUnavailable) as exc:
                pages = []
                home = site.config.warmup_urls[0]
                report.rows.append(_row(site.key, method, "homepage", home, None, False,
                                        str(exc)))  # fmt: skip
            for i, (url, page) in enumerate(zip(site.config.warmup_urls, pages, strict=False)):
                name = "homepage" if i == 0 else f"warm-up page {i}"
                report.rows.append(_row(site.key, method, name, url, page, False))
            for ep in endpoints:
                take()
                try:
                    r = fetcher.get(ep.url, ep.params, api=True)
                except (TransportError, MethodUnavailable) as exc:
                    report.rows.append(_row(site.key, method, ep.name, ep.full_url(), None,
                                            ep.expect_json, str(exc)))  # fmt: skip
                    continue
                report.rows.append(_row(site.key, method, ep.name, ep.full_url(), r,
                                        ep.expect_json))  # fmt: skip
        finally:
            fetcher.close()
    _summarise(report, [e.name for e in endpoints])
    if memory is not None:
        _remember(site, report, memory)
    return report


def _summarise(report: DiagReport, names: list[str]) -> None:
    for name in ["homepage", *names]:
        rows = [r for r in report.rows if r.endpoint == name]
        ok = next((r for r in rows if r.verdict == "OK"), None)
        pick = ok or (rows[-1] if rows else None)
        if pick is not None:
            report.summary[name] = {"verdict": pick.verdict, "method": pick.method}
    for method in report.methods:
        rows = [r for r in report.rows if r.method == method and r.endpoint in names]
        if rows and all(r.verdict in ("OK", "empty") for r in rows):
            report.working_method = method
            break


def _remember(site: SiteProfile, report: DiagReport, memory: MethodMemory) -> None:
    web = WebSession(site, memory=memory)
    for method in report.methods:
        home = next((r for r in report.rows if r.method == method and r.endpoint == "homepage"),
                    None)  # fmt: skip
        if home is not None and home.verdict.startswith("blocked"):
            web.mark_blocked(method, f"warm-up HTTP {home.status} (diagnose)")  # type: ignore[arg-type]
    if report.working_method:
        web.remember(report.working_method)  # type: ignore[arg-type]


# ───────────────────────── output / storage ─────────────────────────


def render(report: DiagReport) -> str:
    """Human-readable table. Cookie values and tokens are never part of a row."""
    out = [f"{report.site.upper()} diagnose at {report.checked_at}"]
    for method in report.methods:
        out.append(f"\n  via {method}")
        for r in (x for x in report.rows if x.method == method):
            cookies = f"[{', '.join(r.cookie_names)}]" if r.cookie_names else "[]"
            out.append(
                f"    {r.endpoint:<20} {r.status if r.status is not None else '---':>3}  "
                f"{r.verdict:<18} {r.length:>9,} B  server={r.server or '-'}  "
                f"type={(r.content_type or '-').split(';')[0]}  set-cookie={cookies}"
            )
            if r.url:
                out.append(f"      {r.url}")
    out.append("\n  summary")
    for name, s in report.summary.items():
        out.append(f"    {name:<20} {s['verdict']:<18} ({s['method']})")
    out.append(f"  working method: {report.working_method or 'none: use the manual uploads'}")
    return "\n".join(out)


def store(redis: Any, report: DiagReport) -> None:
    redis.set(REDIS_KEY.format(site=report.site), json.dumps(report.to_json()))


def load(redis: Any, site: str) -> dict[str, Any] | None:
    raw = redis.get(REDIS_KEY.format(site=site))
    return json.loads(raw) if raw else None


def run_site(
    site_key: str,
    config: Any,
    *,
    symbol: str,
    today: date,
    now: datetime,
    limiter: Limiter | None,
    rate_limit_timeout_s: float,
    memory: MethodMemory | None,
    methods: list[SessionMethod] | None = None,
    factories: dict[SessionMethod, FetcherFactory] | None = None,
) -> DiagReport:
    """``config``: the ProvidersConfig. ``site_key``: "nse" or "bse"."""
    if site_key == "nse":
        site, eps = nse_site(config.nse), nse_endpoints(config.nse, symbol, today)
    else:
        site, eps = bse_site(config.bse), bse_endpoints(config.bse, today)
    return diagnose(site, eps, methods=methods, factories=factories, limiter=limiter,
                    rate_limit_timeout_s=rate_limit_timeout_s, memory=memory, now=now)  # fmt: skip


SITE_PROVIDERS: dict[str, Provider] = {"nse": Provider.NSE, "bse": Provider.BSE}
Clock = Callable[[], datetime]
