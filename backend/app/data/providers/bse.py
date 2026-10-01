"""BSE public data: the equity scrip master (SPEC §3.5) and corporate announcements (§3.8).
BSE's API answers browser-like requests that carry its site as referer; ``rate_limits.bse``
keeps it polite (every request after a call's first takes a token; the router pays for the
first), and the router retries with backoff. Every file is cached raw before parsing (§3.2a).
Respect BSE's terms of use; personal use only."""

import json
from datetime import date
from typing import Any

import pandas as pd
import requests

from app.core.config import BseConfig, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.events import EVENT_COLUMNS, EventFormatError, parse_bse_announcements
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.providers.nse import BROWSER_HEADERS
from app.data.raw_store import RawStore, RawStoreError
from app.data.symbol_master import BseScrip, MasterFormatError, parse_bse_scrips
from app.db.enums import EventKind


class BseProvider:
    """Implements BseScripMasterProvider and EventsProvider (announcements; the "Result"
    category comes back as results filings)."""

    name = Provider.BSE

    def __init__(
        self,
        config: BseConfig,
        *,
        raw_store: RawStore | None = None,
        session: requests.Session | None = None,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
    ) -> None:
        self._cfg = config
        self._raw = raw_store
        self._limiter = limiter
        self._rl_timeout = rate_limit_timeout_s
        self._http = session or requests.Session()
        self._http.headers.update({**BROWSER_HEADERS, "Referer": config.referer,
                                   "Origin": config.referer.rstrip("/")})  # fmt: skip

    def scrip_master(self) -> list[BseScrip]:
        url = f"{self._cfg.api_url}{self._cfg.scrip_master_path}"
        try:
            resp = self._http.get(url, timeout=self._cfg.request_timeout_s)
        except requests.RequestException as exc:
            raise ProviderError(f"BSE {url}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"BSE scrip master: HTTP {resp.status_code}")
        if self._raw is not None:  # cache before parsing (§3.2a)
            try:
                self._raw.save("bse", "scrip_master.json", resp.content)
            except RawStoreError as exc:
                raise ProviderUnavailable(str(exc)) from exc
        try:
            return parse_bse_scrips(json.loads(resp.content))
        except (ValueError, MasterFormatError) as exc:
            raise ProviderError(f"BSE scrip master: {exc}") from exc

    def _get_json(self, url: str, params: dict[str, str], first: bool) -> tuple[bytes, Any]:
        if not first and self._limiter is not None:
            self._limiter.acquire(Provider.BSE, timeout=self._rl_timeout)
        try:
            resp = self._http.get(url, params=params, timeout=self._cfg.request_timeout_s)
        except requests.RequestException as exc:
            raise ProviderError(f"BSE {url}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"BSE {url}: HTTP {resp.status_code}")
        try:
            return resp.content, json.loads(resp.content)
        except ValueError as exc:
            raise ProviderError(f"BSE {url}: response is not JSON (blocked?)") from exc

    def events(self, kind: EventKind, start: date, end: date) -> pd.DataFrame:
        """Announcements of all companies disseminated in [start, end], newest first, up to
        ``announcements_max_pages`` pages. Rows in ``results_categories`` are results filings."""
        if kind is not EventKind.ANNOUNCEMENT:
            raise ProviderUnavailable(f"BSE: no {kind.value} feed (announcements only)")
        url = f"{self._cfg.api_url}{self._cfg.announcements_path}"
        frames, warnings, seen = [], [], 0
        for page in range(1, self._cfg.announcements_max_pages + 1):
            params = {"pageno": str(page), "strCat": "-1", "strPrevDate": f"{start:%Y%m%d}",
                      "strScrip": "", "strSearch": "P", "strToDate": f"{end:%Y%m%d}",
                      "strType": "C", "subcategory": "-1"}  # fmt: skip
            content, payload = self._get_json(url, params, first=page == 1)
            raw = None
            if self._raw is not None:  # cache before parsing (§3.2a)
                try:
                    raw = self._raw.relative(self._raw.save(
                        "bse", f"announcements_{start:%Y%m%d}_{end:%Y%m%d}_p{page}.json",
                        content))  # fmt: skip
                except RawStoreError as exc:
                    raise ProviderUnavailable(str(exc)) from exc
            try:
                df, total = parse_bse_announcements(
                    payload, results_categories=self._cfg.results_categories,
                    attachment_url=self._cfg.attachment_url)  # fmt: skip
            except EventFormatError as exc:
                raise ProviderError(f"BSE announcements: {exc}") from exc
            df["raw_path"] = raw
            frames.append(df)
            warnings += df.attrs.get("warnings", [])
            seen += len(payload.get("Table") or [])
            if df.empty or (total is not None and seen >= total):
                break
        else:
            warnings.append(f"stopped after {self._cfg.announcements_max_pages} pages; older "
                            "announcements in the window were not read")  # fmt: skip
        out = pd.concat(frames, ignore_index=True) if frames else \
            pd.DataFrame(columns=[*EVENT_COLUMNS, "raw_path"])  # fmt: skip
        out = out.drop_duplicates(["kind", "source_id"], ignore_index=True)
        out.attrs["warnings"] = warnings[:50]
        return out


def build_bse_provider(
    config: ProvidersConfig, limiter: Limiter | None = None, raw_store: RawStore | None = None
) -> BseProvider:
    return BseProvider(config.bse, raw_store=raw_store, limiter=limiter,
                       rate_limit_timeout_s=config.retry.rate_limit_timeout_s)  # fmt: skip
