"""Relative strength (SPEC §6). Pure functions.

RS_t        = stock close_t / benchmark close_t            (weekly)
Mansfield_t = (RS_t / SMA(RS, rs_sma_weeks)_t - 1) x 100   (> 0: outperforming its own trend)
Percentile  = share of the universe with a lower value, ties counted half, x 100
"""

from collections.abc import Mapping

import pandas as pd

from app.technical.bars import sma


def mansfield_rs(
    stock_weekly_close: pd.Series, bench_weekly_close: pd.Series, weeks: int
) -> pd.Series:
    """Aligned on the stock's weeks; weeks without a benchmark close are dropped."""
    bench = bench_weekly_close.reindex(stock_weekly_close.index, method="ffill")
    rs = (stock_weekly_close / bench).dropna()
    return ((rs / sma(rs, weeks) - 1) * 100).dropna()


def percentile_rank(values: Mapping[str, float | None], symbol: str) -> float | None:
    """Percentile (0-100) of ``values[symbol]`` among the non-missing values."""
    own = values.get(symbol)
    others = [v for v in values.values() if v is not None and not pd.isna(v)]
    if own is None or pd.isna(own) or not others:
        return None
    below = sum(1 for v in others if v < own)
    equal = sum(1 for v in others if v == own)
    return 100.0 * (below + 0.5 * equal) / len(others)


def percentile_ranks(values: Mapping[str, float | None]) -> dict[str, float | None]:
    return {s: percentile_rank(values, s) for s in values}
