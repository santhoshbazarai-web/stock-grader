"""Provider interfaces (SPEC §3.1) and the errors the router understands.

DataFrame conventions (so the router can judge freshness):
- ``daily_ohlcv``: ``DatetimeIndex`` of trading dates; columns ``open, high, low, close, volume``;
  raw (unadjusted) prices — adjustment happens in ``data/adjust.py``.
- ``annual`` / ``quarterly``: one row per period, ``DatetimeIndex`` of period-end dates.
- A provider may set ``df.attrs["as_of"]`` (tz-aware ``datetime``) to state freshness
  explicitly; it takes precedence over the index.
"""

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

from app.core.config import Provider


class ProviderError(Exception):
    """Transient provider failure (network, 5xx, throttled). The router retries with backoff."""


class ProviderUnavailable(ProviderError):
    """Permanent for this call: no valid broker token, missing entitlement, unsupported symbol.
    Not retried; the router falls through to the next provider immediately."""


@runtime_checkable
class PriceProvider(Protocol):
    name: Provider

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...

    def ltp(self, symbols: list[str]) -> dict[str, float]: ...


@runtime_checkable
class IndexPriceProvider(Protocol):
    """Index candles (Nifty 500, sectoral) for RS and beta. ``index`` is the canonical index
    code, e.g. ``"NIFTY500"``; each provider maps it to its own symbology."""

    name: Provider

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    name: Provider

    def annual(self, symbol: str) -> pd.DataFrame: ...  # rows = fiscal years

    def quarterly(self, symbol: str) -> pd.DataFrame: ...
