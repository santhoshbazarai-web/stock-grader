"""BSE public data: the equity scrip master (SPEC §3.5). BSE's API answers browser-like
requests that carry its site as referer; ``rate_limits.bse`` keeps it polite, and the router
retries with backoff. Every file is cached raw before parsing (§3.2a). Respect BSE's terms of
use; personal use only."""

import json

import requests

from app.core.config import BseConfig, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.providers.nse import BROWSER_HEADERS
from app.data.raw_store import RawStore, RawStoreError
from app.data.symbol_master import BseScrip, MasterFormatError, parse_bse_scrips


class BseProvider:
    """Implements BseScripMasterProvider."""

    name = Provider.BSE

    def __init__(
        self,
        config: BseConfig,
        *,
        raw_store: RawStore | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._cfg = config
        self._raw = raw_store
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


def build_bse_provider(
    config: ProvidersConfig, limiter: Limiter | None = None, raw_store: RawStore | None = None
) -> BseProvider:
    """One request per call: the router's rate-limit token covers it (``limiter`` unused)."""
    del limiter
    return BseProvider(config.bse, raw_store=raw_store)
