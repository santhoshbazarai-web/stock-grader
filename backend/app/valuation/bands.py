"""Own-history valuation bands (SPEC §5.3). Pure functions.

For each trading day the multiple is

    PE        = price / TTM EPS
    EV/EBITDA = (price + net debt per share) / TTM EBITDA per share
    PB        = price / book value per share

where each per-share fundamental is a *step series* keyed by the date it became known
(announcement date) and carried forward — so the band is point-in-time. Days whose
denominator is <= 0 are excluded (SPEC: exclude periods where earnings are <= 0).

Over the lookback window the band reports the median and the sample standard deviation, with
levels at median + k x sigma for k in -2..+2. Levels convert back to prices with the *current*
denominator: price = level x denominator - offset (offset = net debt/share for EV/EBITDA).

Structural breaks (SPEC §5.3): a band can start no earlier than ``start_floor`` (the latest
merger / demerger), and ``regression_band`` fits P/B = a + b x ROE so a P/B band can be read at
the current ROE when the post-break window is too short for a plain band.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LEVELS = (-2, -1, 0, 1, 2)


@dataclass(frozen=True)
class Band:
    multiple: str
    lookback_years: int
    median: float | None
    sigma: float | None
    observations: int
    levels: dict[int, float] = field(default_factory=dict)  # k → multiple at median + k·sigma
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.median is not None


def _step(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Carry a step series (keyed by effective date) forward onto trading days."""
    s = series.dropna()
    s.index = pd.DatetimeIndex(s.index)
    s = s.sort_index(kind="stable")
    s = s[~s.index.duplicated(keep="last")]  # one value per effective date
    return s.reindex(s.index.union(index)).ffill().reindex(index)


def multiple_series(
    price: pd.Series, denominator: pd.Series, offset: pd.Series | None = None
) -> pd.Series:
    idx = pd.DatetimeIndex(price.index)
    den = _step(denominator, idx)
    num = price.astype(float)
    if offset is not None:
        num = num + _step(offset, idx)
    return (num / den.where(den > 0)).dropna()


def band(
    name: str,
    price: pd.Series,
    denominator: pd.Series,
    *,
    lookback_years: int,
    min_observations: int,
    offset: pd.Series | None = None,
    as_of: pd.Timestamp | None = None,
    start_floor: pd.Timestamp | None = None,
) -> Band:
    """``start_floor``: the window starts no earlier than this date (a structural break);
    the reason then names the window instead of the lookback."""
    end = as_of or pd.Timestamp(price.index.max())
    start = end - pd.DateOffset(years=lookback_years)
    span = f"{lookback_years}y"
    if start_floor is not None and start_floor > start:
        start = start_floor - pd.Timedelta(days=1)  # the break day itself is post-break
        span = f"{start_floor:%d %b %Y} to {end:%d %b %Y}"
    window = price.loc[(price.index > start) & (price.index <= end)]
    m = multiple_series(window, denominator, offset)
    n = len(m)
    if n < min_observations:
        reason = f"{name}: only {n} valid days in {span} (need {min_observations})"
        return Band(name, lookback_years, None, None, n, reasons=[reason])
    median, sigma = float(m.median()), float(m.std(ddof=1))
    levels = {k: median + k * sigma for k in LEVELS}
    reason = f"{name} {span}: median {median:.1f}x, sigma {sigma:.1f}x over {n} days"
    return Band(name, lookback_years, median, sigma, n, levels, [reason])


def regression_band(
    price: pd.Series,
    bvps: pd.Series,
    roe: pd.Series,
    *,
    roe_now: float | None,
    min_observations: int,
    min_distinct_roe: int,
    start: pd.Timestamp | None = None,
    as_of: pd.Timestamp | None = None,
) -> Band:
    """P/B-vs-ROE regression band: daily P/B (price / step BVPS) regressed on the step ROE known
    that day, P/B = a + b·ROE. The median is the fitted P/B at ``roe_now``; sigma is the residual
    standard deviation, so the levels are fitted ± k·sigma. ``start`` limits the fit window."""
    end = as_of or pd.Timestamp(price.index.max())
    lo = start - pd.Timedelta(days=1) if start is not None else pd.Timestamp.min
    window = price.loc[(price.index > lo) & (price.index <= end)]
    span = f"{start:%d %b %Y} to {end:%d %b %Y}" if start is not None else "full history"
    pb = multiple_series(window, bvps)
    x = _step(roe, pd.DatetimeIndex(pb.index))
    pair = pd.DataFrame({"pb": pb, "roe": x}).dropna()
    n, distinct = len(pair), int(pair["roe"].round(6).nunique())
    if n < min_observations or distinct < min_distinct_roe:
        reason = (f"P/B-vs-ROE regression ({span}): {n} days, {distinct} distinct ROE values "
                  f"(need {min_observations} and {min_distinct_roe})")  # fmt: skip
        return Band("pb_roe", 0, None, None, n, reasons=[reason])
    if roe_now is None:
        why = "P/B-vs-ROE regression: current ROE unknown"
        return Band("pb_roe", 0, None, None, n, reasons=[why])
    slope, intercept = np.polyfit(pair["roe"].to_numpy(), pair["pb"].to_numpy(), 1)
    resid = pair["pb"] - (intercept + slope * pair["roe"])
    fitted, sigma = float(intercept + slope * roe_now), float(resid.std(ddof=1))
    if fitted <= 0:
        why = f"P/B-vs-ROE regression ({span}): fitted P/B not positive"
        return Band("pb_roe", 0, None, None, n, reasons=[why])
    levels = {k: fitted + k * sigma for k in LEVELS}
    reason = (f"P/B-vs-ROE regression {span}: P/B = {intercept:.2f} + {slope:.2f} x ROE → "
              f"{fitted:.2f}x at ROE {roe_now:.1%}, sigma {sigma:.2f}x over {n} days")  # fmt: skip
    return Band("pb_roe", 0, fitted, sigma, n, levels, [reason])


def band_prices(
    b: Band, current_denominator: float | None, current_offset: float = 0.0
) -> dict[int, float] | None:
    """Band levels as prices; ``None`` when the band or a positive denominator is missing."""
    if not b.ok or current_denominator is None or current_denominator <= 0:
        return None
    return {k: lvl * current_denominator - current_offset for k, lvl in b.levels.items()}
