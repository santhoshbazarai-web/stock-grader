"""yfinance: price fallback (``.NS`` equities, ``^`` index tickers), corporate actions and
partial fundamentals.

Caveats surfaced to callers via ``df.attrs["warnings"]`` (the router copies them into
``reasons``):
- Annual statements cover only ~4 fiscal years (quarterly ~5 quarters), not the 10 years
  SPEC §4 needs; ``attrs["limited_history"] = True``. Screener uploads are the primary source.
- Yahoo does not state the statement basis; treated as consolidated.
- Yahoo reports bonus issues as splits.

Prices: Yahoo's OHLC is split-adjusted even with ``auto_adjust=False``. To honour the provider
contract (raw prices; ``data/adjust.py`` adjusts from corporate actions) the split adjustment
is reversed using the full split history: raw = adjusted x (product of split ratios after the
bar), raw volume = volume / that product. Dividends are not adjusted (auto_adjust=False).
"""

import logging
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol

import pandas as pd
import yfinance as yf

from app.core.config import Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.canonical import (
    CANONICAL_FIELDS,
    YFINANCE_SCALE,
    Table,
    fiscal_year,
    labels_for,
    pick,
)
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.db.enums import CorporateActionType

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
CA_COLUMNS = [
    "ex_date",
    "action_type",
    "ratio_old",
    "ratio_new",
    "dividend_per_share",
    "record_date",
    "description",
]
LIMITED_ANNUAL = (
    "yfinance annual statements cover only ~4 fiscal years; 10-year metrics need a Screener upload"
)
LIMITED_QUARTERLY = "yfinance quarterly statements cover only ~5 quarters"
BASIS_ASSUMED = "Yahoo does not state the statement basis; assumed consolidated"
BONUS_AS_SPLIT = "Yahoo reports bonus issues as splits"


class TickerLike(Protocol):
    """The subset of ``yfinance.Ticker`` used here."""

    def history(self, **kwargs: Any) -> pd.DataFrame: ...

    @property
    def splits(self) -> pd.Series: ...

    @property
    def dividends(self) -> pd.Series: ...

    @property
    def fast_info(self) -> Any: ...

    def get_income_stmt(self, *, pretty: bool = ..., freq: str = ...) -> pd.DataFrame: ...

    def get_balance_sheet(self, *, pretty: bool = ..., freq: str = ...) -> pd.DataFrame: ...

    def get_cashflow(self, *, pretty: bool = ..., freq: str = ...) -> pd.DataFrame: ...


def default_ticker_factory(ticker: str) -> TickerLike:
    t: TickerLike = yf.Ticker(ticker)
    return t


def to_yf_symbol(symbol: str) -> str:
    """``TCS`` → ``TCS.NS``; ``M&M`` → ``M&M.NS``; already-suffixed tickers pass through."""
    s = symbol.strip().upper()
    return s if s.endswith((".NS", ".BO")) or s.startswith("^") else f"{s}.NS"


def _naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    return idx.normalize()


def unadjust_splits(df: pd.DataFrame, splits: pd.Series) -> pd.DataFrame:
    """Reverse Yahoo's split adjustment (see module docstring)."""
    if df.empty or splits is None or splits.empty:
        return df
    split_dates = _naive_dates(splits.index)
    ratios = pd.Series(splits.to_numpy(dtype=float), index=split_dates)
    ratios = ratios[ratios > 0]
    factor = pd.Series(1.0, index=df.index)
    for when, ratio in ratios.items():
        factor[df.index < when] *= ratio
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    out["volume"] = (out["volume"] / factor).round().astype("int64")
    return out


class YFinanceProvider:
    """Implements PriceProvider, IndexPriceProvider, FundamentalsProvider and
    CorporateActionsProvider. Rate limiting: the router pays for the first request of a call;
    each further yfinance request in the same call takes its own token."""

    name = Provider.YFINANCE

    def __init__(
        self,
        index_tickers: Mapping[str, str],
        *,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
        ticker_factory: Callable[[str], TickerLike] = default_ticker_factory,
    ) -> None:
        self._index_tickers = {k.upper(): v for k, v in index_tickers.items()}
        self._limiter = limiter
        self._timeout = rate_limit_timeout_s
        self._ticker_factory = ticker_factory
        self._requests = 0

    # ───────────── plumbing ─────────────

    def _begin(self) -> None:
        self._requests = 0

    def _call[T](self, fn: Callable[[], T], what: str) -> T:
        if self._requests > 0 and self._limiter is not None:
            self._limiter.acquire(self.name, timeout=self._timeout)
        self._requests += 1
        try:
            return fn()
        except ProviderError:
            raise
        except Exception as exc:  # yfinance raises many types (rate limit, HTTP, parsing)
            raise ProviderError(f"yfinance {what}: {type(exc).__name__}: {exc}") from exc

    # ───────────── prices ─────────────

    def _ohlcv(self, ticker: str, start: date, end: date, *, unadjust: bool) -> pd.DataFrame:
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        self._begin()
        t = self._ticker_factory(ticker)
        raw = self._call(
            lambda: t.history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
                interval="1d",
                auto_adjust=False,
                actions=False,
            ),
            f"history {ticker}",
        )
        if raw is None or raw.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))
        df = raw.rename(columns=str.lower)[OHLCV_COLUMNS].copy()
        df.index = pd.DatetimeIndex(_naive_dates(raw.index), name="date")
        df = df.dropna(subset=["close"])
        df["volume"] = df["volume"].fillna(0).astype("int64")
        if unadjust:
            df = unadjust_splits(df, self._call(lambda: t.splits, f"splits {ticker}"))
        return df

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._ohlcv(to_yf_symbol(symbol), start, end, unadjust=True)

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        ticker = self._index_tickers.get(index.replace(" ", "").upper())
        if ticker is None:
            raise ProviderUnavailable(f"yfinance: no ticker configured for index {index}")
        return self._ohlcv(ticker, start, end, unadjust=False)

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        """Last price (delayed ~15 min on Yahoo); symbols without a price are omitted."""
        self._begin()
        out: dict[str, float] = {}
        for s in symbols:
            t = self._ticker_factory(to_yf_symbol(s))
            try:
                price = self._call(partial(_last_price, t), f"price {s}")
            except ProviderError as exc:
                logger.warning("yfinance price for %s skipped: %s", s, exc)
                continue
            if price is not None and not pd.isna(price):
                out[s] = float(price)
        return out

    # ───────────── corporate actions ─────────────

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self._begin()
        ticker = to_yf_symbol(symbol)
        t = self._ticker_factory(ticker)
        splits = self._call(lambda: t.splits, f"splits {ticker}")
        dividends = self._call(lambda: t.dividends, f"dividends {ticker}")
        rows: list[dict[str, Any]] = [
            _ca_row(when, CorporateActionType.SPLIT, 1.0, ratio, None, f"yfinance split x{ratio:g}")
            for when, ratio in _in_range(splits, start, end)
        ]
        per_day: dict[date, float] = {}  # one dividend row per ex-date (unique key)
        for when, amount in _in_range(dividends, start, end):
            per_day[when] = per_day.get(when, 0.0) + amount
        rows += [
            _ca_row(
                when, CorporateActionType.DIVIDEND, None, None, dps, f"yfinance dividend Rs {dps:g}"
            )
            for when, dps in per_day.items()
        ]
        df = pd.DataFrame(rows, columns=CA_COLUMNS).sort_values("ex_date", ignore_index=True)
        df.attrs["warnings"] = (
            [BONUS_AS_SPLIT] if any(r["action_type"] == "split" for r in rows) else []
        )
        return df

    # ───────────── fundamentals ─────────────

    def _statement(self, symbol: str, freq: str, table: Table) -> pd.DataFrame:
        self._begin()
        ticker = to_yf_symbol(symbol)
        t = self._ticker_factory(ticker)
        parts = [self._call(lambda: t.get_income_stmt(pretty=False, freq=freq), "income stmt")]
        if table == "fin_annual":
            parts.append(self._call(lambda: t.get_balance_sheet(pretty=False, freq=freq), "bs"))
            parts.append(self._call(lambda: t.get_cashflow(pretty=False, freq=freq), "cashflow"))
        by_period: dict[pd.Timestamp, dict[str, Any]] = {}
        for part in parts:
            if part is None or part.empty:
                continue
            for col in part.columns:
                values = {str(k): v for k, v in part[col].items()}
                by_period.setdefault(pd.Timestamp(col).normalize(), {}).update(values)

        labels = labels_for("yfinance", table)
        records = {}
        for period_end, values in by_period.items():
            rec: dict[str, Any] = {
                name: pick(values, lbls, YFINANCE_SCALE[CANONICAL_FIELDS[name].unit])
                for name, lbls in labels.items()
            }
            if all(v is None for v in rec.values()):
                continue
            if table == "fin_annual":
                rec["fiscal_year"] = fiscal_year(period_end)
            records[period_end] = rec
        df = pd.DataFrame.from_dict(records, orient="index")
        df.index = pd.DatetimeIndex(df.index, name="period_end")
        df = df.sort_index()
        df.attrs.update(
            limited_history=True,
            statement_type="consolidated",
            warnings=[
                LIMITED_ANNUAL if table == "fin_annual" else LIMITED_QUARTERLY,
                BASIS_ASSUMED,
            ],
        )
        return df

    def annual(self, symbol: str) -> pd.DataFrame:
        return self._statement(symbol, "yearly", "fin_annual")

    def quarterly(self, symbol: str) -> pd.DataFrame:
        return self._statement(symbol, "quarterly", "fin_quarterly")


def _last_price(t: TickerLike) -> Any:
    return t.fast_info["last_price"]


def _in_range(series: pd.Series, start: date, end: date) -> list[tuple[date, float]]:
    if series is None or series.empty:
        return []
    dates = _naive_dates(series.index)
    return [
        (d.date(), float(v))
        for d, v in zip(dates, series.to_numpy(), strict=True)
        if start <= d.date() <= end and v and not pd.isna(v)
    ]


def _ca_row(
    ex_date: date,
    kind: CorporateActionType,
    ratio_old: float | None,
    ratio_new: float | None,
    dps: float | None,
    description: str,
) -> dict[str, Any]:
    return {
        "ex_date": ex_date,
        "action_type": kind.value,
        "ratio_old": ratio_old,
        "ratio_new": ratio_new,
        "dividend_per_share": dps,
        "record_date": None,
        "description": description,
    }


def build_yfinance_provider(config: ProvidersConfig, limiter: Limiter | None) -> YFinanceProvider:
    return YFinanceProvider(
        config.yfinance_index_tickers,
        limiter=limiter,
        rate_limit_timeout_s=config.retry.rate_limit_timeout_s,
    )
