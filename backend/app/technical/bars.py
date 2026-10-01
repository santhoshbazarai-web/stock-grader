"""Bar utilities shared by the technical engine. Pure functions.

All technical calculations run on split/bonus-adjusted prices (AGENTS.md rule 6); callers pass
``adj_*`` columns renamed to ``open, high, low, close, volume``.
"""

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


def to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLCV → weekly (Mon-Fri) bars labelled with the week's *last trading day*, so the
    label is always a real session date (what the chart plots)."""
    if daily.empty:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], name="date"))
    df = daily[OHLCV].sort_index()
    df.index = pd.DatetimeIndex(df.index)
    week = df.index.to_period("W-FRI")
    grouped = df.groupby(week)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    out.index = pd.DatetimeIndex(grouped.apply(lambda g: g.index.max()), name="date")
    return out


def true_range(bars: pd.DataFrame) -> pd.Series:
    prev_close = bars["close"].shift(1)
    ranges = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing: seeded with the simple mean of the first ``period`` values."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < period:
        return pd.Series(out, index=series.index)
    start = valid[0]
    seed = start + period - 1
    out[seed] = np.nanmean(values[start : seed + 1])
    for i in range(seed + 1, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(out, index=series.index)


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    """Average true range (Wilder). The first bar's true range is its high - low."""
    return wilder(true_range(bars), period)


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    gain = wilder(delta.clip(lower=0).iloc[1:], period).reindex(close.index)
    loss = wilder((-delta.clip(upper=0)).iloc[1:], period).reindex(close.index)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()
