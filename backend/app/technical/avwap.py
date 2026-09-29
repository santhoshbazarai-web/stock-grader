"""Anchored VWAP (SPEC §6) on weekly bars. Pure functions.

    AVWAP_t = sum_{anchor..t} typical_i x volume_i / sum_{anchor..t} volume_i,
    typical = (high + low + close) / 3

Anchors (``technical.avwap_anchors``):
- ``low_52w``: the bar with the lowest low in the last ``high_52w_weeks`` weeks
- ``last_results_date``: the first bar on/after the latest results announcement (if known)
- ``last_major_swing_low``: the latest swing low of the wider ``major_swing_fractal_n`` fractal
"""

from dataclasses import dataclass

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.structure import Swing


@dataclass(frozen=True)
class Avwap:
    anchor: str
    anchor_time: pd.Timestamp
    series: pd.Series  # from the anchor bar to the last bar
    value: float  # latest


def anchored_vwap(bars: pd.DataFrame, anchor_pos: int) -> pd.Series:
    b = bars.iloc[anchor_pos:]
    typical = (b["high"] + b["low"] + b["close"]) / 3
    vol = b["volume"].astype(float)
    return (typical * vol).cumsum() / vol.cumsum().where(vol.cumsum() > 0)


def anchors(
    bars: pd.DataFrame,
    cfg: TechnicalConfig,
    *,
    major_swings: list[Swing],
    last_results_date: pd.Timestamp | None,
) -> dict[str, int]:
    """Bar positions for each configured anchor that can be located."""
    out: dict[str, int] = {}
    n = len(bars)
    for name in cfg.avwap_anchors:
        if name == "low_52w":
            window = bars["low"].iloc[max(0, n - cfg.high_52w_weeks) :]
            out[name] = n - len(window) + int(window.to_numpy(float).argmin())
        elif name == "last_results_date" and last_results_date is not None:
            pos = int(bars.index.searchsorted(pd.Timestamp(last_results_date)))
            if pos < n:
                out[name] = pos
        elif name == "last_major_swing_low":
            lows = [s for s in major_swings if s.kind == "low"]
            if lows:
                out[name] = lows[-1].index
    return out


def avwaps(
    bars: pd.DataFrame,
    cfg: TechnicalConfig,
    *,
    major_swings: list[Swing],
    last_results_date: pd.Timestamp | None = None,
) -> list[Avwap]:
    result = []
    for name, pos in anchors(
        bars, cfg, major_swings=major_swings, last_results_date=last_results_date
    ).items():
        series = anchored_vwap(bars, pos).dropna()
        if not series.empty:
            result.append(Avwap(name, bars.index[pos], series, float(series.iloc[-1])))
    return result
