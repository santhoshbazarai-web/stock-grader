"""NSE Market Lens (beta): the JSON its page loads for a company's financials, read only by the
reconciliation (SPEC v0.2 §0, §3.2a, §3.9) and only when ``providers.market_lens.enabled``.

The JSON is undocumented and may change or be restricted, so every field name is config
(``market_lens.field_map`` and the ``*_field`` keys) and the adapter is off by default: check
the names against the page's own requests before enabling it. A payload of another shape
fails the source (the reconciliation then reports it as unavailable), never guesses.
"""

import json
import re
from typing import Any

import pandas as pd
import requests

from app.core.config import MarketLensConfig, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.events import parse_date
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.providers.nse import BROWSER_HEADERS
from app.data.raw_store import RawStore, RawStoreError

REFERENCE_COLUMNS = ["period_end", "period_type", "basis", "item_code", "value_inr"]


class MarketLensFormatError(ValueError):
    pass


_OTHER_SPANS = {"half", "nine", "9m", "6m", "h1", "h2"}  # half-year, nine months: not compared


def _kind(text: Any, words: dict[str, tuple[str, ...]], default: str) -> str | None:
    """The key whose words appear as whole words in ``text``; None for anything else."""
    if text is None:
        return default
    tokens = set(re.findall(r"[a-z0-9]+", str(text).lower()))
    if tokens & _OTHER_SPANS:
        return None
    return next((k for k, ws in words.items() if tokens & set(ws)), None)


def parse_market_lens(payload: Any, cfg: MarketLensConfig) -> pd.DataFrame:
    """The page's JSON → long reference rows (rupees). Periods whose type or basis is not
    recognised are skipped; values that are not numbers are left out (never 0)."""
    records = payload.get(cfg.records_key) if cfg.records_key and isinstance(payload, dict) \
        else payload  # fmt: skip
    if not isinstance(records, list):
        raise MarketLensFormatError(f"no list at {cfg.records_key or 'the top level'}")
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        end = parse_date(rec.get(cfg.period_end_field))
        ptype = _kind(
            rec.get(cfg.period_type_field) if cfg.period_type_field else None,
            {
                "quarter": ("quarter", "quarterly", "q", "qtr"),
                "year": ("annual", "year", "yearly", "fy", "12m"),
            },
            "year",
        )
        basis = _kind(rec.get(cfg.basis_field) if cfg.basis_field else None,
                      {"standalone": ("standalone",), "consolidated": ("consolidated",)},
                      "consolidated")  # fmt: skip
        if end is None or ptype is None or basis is None:
            continue
        for code, fld in cfg.field_map.items():
            raw = rec.get(fld)
            try:
                value = float(str(raw).replace(",", "")) if raw not in (None, "", "-") else None
            except ValueError:
                value = None
            if value is not None:
                rows.append(
                    {
                        "period_end": end,
                        "period_type": ptype,
                        "basis": basis,
                        "item_code": code,
                        "value_inr": value * cfg.amount_unit_inr,
                    }
                )
    return pd.DataFrame(rows, columns=REFERENCE_COLUMNS)


class MarketLensProvider:
    """Implements ReferenceFinancialsProvider."""

    name = Provider.MARKET_LENS

    def __init__(self, config: MarketLensConfig, *, raw_store: RawStore | None = None,
                 session: requests.Session | None = None) -> None:  # fmt: skip
        self._cfg = config
        self._raw = raw_store
        self._http = session or requests.Session()
        self._http.headers.update({**BROWSER_HEADERS, "Referer": f"{config.base_url}/"})

    def reference_financials(self, symbol: str) -> pd.DataFrame:
        sym = symbol.strip().upper()
        url = f"{self._cfg.base_url}{self._cfg.financials_path.replace('{symbol}', sym)}"
        try:
            resp = self._http.get(url, timeout=self._cfg.request_timeout_s)
        except requests.RequestException as exc:
            raise ProviderError(f"Market Lens {url}: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise ProviderUnavailable(f"Market Lens has no page for {sym}")
        if resp.status_code >= 400:
            raise ProviderError(f"Market Lens {url}: HTTP {resp.status_code}")
        if self._raw is not None:  # cache before parsing (§3.2a)
            try:
                self._raw.save("market_lens", f"financials_{sym}.json", resp.content)
            except RawStoreError as exc:
                raise ProviderUnavailable(str(exc)) from exc
        try:
            return parse_market_lens(json.loads(resp.content), self._cfg)
        except (ValueError, MarketLensFormatError) as exc:
            raise ProviderUnavailable(f"Market Lens {sym}: unexpected response ({exc})") from exc


def build_market_lens_provider(
    config: ProvidersConfig, limiter: Limiter | None = None, raw_store: RawStore | None = None
) -> MarketLensProvider | None:
    """None unless ``market_lens.enabled`` (SPEC §0: optional, reconciliation only)."""
    del limiter  # one request per call: the router's token covers it
    if not config.market_lens.enabled:
        return None
    return MarketLensProvider(config.market_lens, raw_store=raw_store)
