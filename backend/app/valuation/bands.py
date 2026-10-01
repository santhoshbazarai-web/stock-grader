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
"""

from dataclasses import dataclass, field

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
    s = series.dropna().sort_index()
    s.index = pd.DatetimeIndex(s.index)
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
) -> Band:
    end = as_of or pd.Timestamp(price.index.max())
    start = end - pd.DateOffset(years=lookback_years)
    window = price.loc[(price.index > start) & (price.index <= end)]
    m = multiple_series(window, denominator, offset)
    n = len(m)
    if n < min_observations:
        reason = f"{name}: only {n} valid days in {lookback_years}y (need {min_observations})"
        return Band(name, lookback_years, None, None, n, reasons=[reason])
    median, sigma = float(m.median()), float(m.std(ddof=1))
    levels = {k: median + k * sigma for k in LEVELS}
    reason = f"{name} {lookback_years}y: median {median:.1f}x, sigma {sigma:.1f}x over {n} days"
    return Band(name, lookback_years, median, sigma, n, levels, [reason])


def band_prices(
    b: Band, current_denominator: float | None, current_offset: float = 0.0
) -> dict[int, float] | None:
    """Band levels as prices; ``None`` when the band or a positive denominator is missing."""
    if not b.ok or current_denominator is None or current_denominator <= 0:
        return None
    return {k: lvl * current_denominator - current_offset for k, lvl in b.levels.items()}
