"""Provider interfaces (SPEC §3.1) and the errors the router understands.

DataFrame conventions (so the router can judge freshness):
- ``daily_ohlcv``: ``DatetimeIndex`` of trading dates; columns ``open, high, low, close, volume``;
  raw (unadjusted) prices — adjustment happens in ``data/adjust.py``.
- ``annual`` / ``quarterly``: one row per period, ``DatetimeIndex`` of period-end dates.
- A provider may set ``df.attrs["as_of"]`` (tz-aware ``datetime``) to state freshness
  explicitly; it takes precedence over the index.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from app.core.config import Provider
from app.data.symbol_master import BseScrip, FyersSymbol, NameChange, NseListing, SymbolChange


class ProviderError(Exception):
    """Transient provider failure (network, 5xx, throttled). The router retries with backoff."""


class ProviderUnavailable(ProviderError):
    """Permanent for this call: no valid broker token, missing entitlement, unsupported symbol.
    Not retried; the router falls through to the next provider immediately."""


@dataclass(frozen=True)
class IssuedToken:
    """A broker access token from an OAuth exchange, with its expiry."""

    access_token: str
    expires_at: datetime

    def __repr__(self) -> str:  # never expose the token
        return f"IssuedToken(access_token=<redacted>, expires_at={self.expires_at.isoformat()})"


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


# ───────────────────────── NSE-style datasets ─────────────────────────
# Column contracts (all frames may carry attrs["as_of"] and attrs["warnings"]: list[str]):
# - delivery:           symbol, series, date, traded_qty, deliverable_qty, delivery_pct,
#                       traded_value_cr
# - index_constituents: symbol, name, industry, series, isin
# - surveillance:       symbol, list_name (SurveillanceList value), stage
# - corporate_actions:  ex_date, action_type (CorporateActionType value), ratio_old, ratio_new,
#                       dividend_per_share, record_date, description
# - shareholding:       index = period end; canonical shareholding fields + filing_date
# - results_filings:    url, period_start, period_end, statement_type, audited, is_bank,
#                       disseminated_at (tz-aware; when the exchange published the filing)
# - annual_reports:     url, fiscal_year (the FY ending in that calendar year), disseminated_at


@runtime_checkable
class DeliveryProvider(Protocol):
    name: Provider

    def delivery(self, day: date) -> pd.DataFrame: ...


@runtime_checkable
class ConstituentsProvider(Protocol):
    name: Provider

    def index_constituents(self, index: str) -> pd.DataFrame: ...


@runtime_checkable
class SurveillanceProvider(Protocol):
    name: Provider

    def surveillance(self) -> pd.DataFrame: ...


@runtime_checkable
class CorporateActionsProvider(Protocol):
    name: Provider

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...


@runtime_checkable
class ShareholdingProvider(Protocol):
    name: Provider

    def shareholding(self, symbol: str) -> pd.DataFrame: ...


@runtime_checkable
class ResultsFilingsProvider(Protocol):
    """Exchange financial-results filings: the list for a symbol, and each XBRL document."""

    name: Provider

    def results_filings(self, symbol: str) -> pd.DataFrame: ...

    def results_document(self, url: str) -> bytes: ...


@runtime_checkable
class AnnualReportsProvider(Protocol):
    """Annual reports: the list for a symbol, and each document (PDF, or a ZIP holding it)."""

    name: Provider

    def annual_reports(self, symbol: str) -> pd.DataFrame: ...

    def annual_report_document(self, url: str) -> bytes: ...


# Symbol master (SPEC §3.5): each exchange / broker supplies its own file; the symbol_master
# job reads all three and joins them on ISIN (app.data.symbol_master).


@runtime_checkable
class NseSymbolFilesProvider(Protocol):
    name: Provider

    def equity_list(self) -> list[NseListing]: ...

    def symbol_changes(self) -> list[SymbolChange]: ...

    def name_changes(self) -> list[NameChange]: ...


@runtime_checkable
class BseScripMasterProvider(Protocol):
    name: Provider

    def scrip_master(self) -> list[BseScrip]: ...


@runtime_checkable
class FyersSymbolMasterProvider(Protocol):
    name: Provider

    def symbol_master(self) -> list[FyersSymbol]: ...
