"""Volume profile (SPEC §6): POC, value-area high/low over the last year of weekly bars.

Each bar's volume is spread evenly across the price bins its [low, high] range touches
(``volume_profile_bins`` equal bins from the window's lowest low to highest high; a zero-range
bar puts all its volume in one bin). POC = midpoint of the fullest bin. The value area grows from
the POC bin, each step adding whichever neighbour holds more volume (ties: the upper one), until
it holds ``value_area_pct`` of the total; VAH / VAL are its outer bin edges.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.config import TechnicalConfig


@dataclass(frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    edges: list[float] = field(default_factory=list)  # bin edges (len = bins + 1)
    volumes: list[float] = field(default_factory=list)  # volume per bin


def volume_profile(bars: pd.DataFrame, cfg: TechnicalConfig) -> VolumeProfile | None:
    win = bars.iloc[-cfg.volume_profile_lookback_weeks :]
    if win.empty or win["volume"].sum() <= 0:
        return None
    lo, hi = float(win["low"].min()), float(win["high"].max())
    if hi <= lo:
        return VolumeProfile(lo, lo, lo, [lo, hi], [float(win["volume"].sum())])
    n = cfg.volume_profile_bins
    edges = np.linspace(lo, hi, n + 1)
    vols = np.zeros(n)
    for b_lo, b_hi, v in zip(win["low"], win["high"], win["volume"], strict=True):
        first = min(int(np.searchsorted(edges, b_lo, side="right")) - 1, n - 1)
        last = min(int(np.searchsorted(edges, b_hi, side="left")) - 1, n - 1)
        last = max(last, first)
        vols[first : last + 1] += float(v) / (last - first + 1)
    poc_i = int(np.argmax(vols))
    target = cfg.value_area_pct * vols.sum()
    lo_i = hi_i = poc_i
    area = vols[poc_i]
    while area < target and (lo_i > 0 or hi_i < n - 1):
        up = vols[hi_i + 1] if hi_i < n - 1 else -1.0
        down = vols[lo_i - 1] if lo_i > 0 else -1.0
        if up >= down:
            hi_i += 1
            area += up
        else:
            lo_i -= 1
            area += down
    poc = float((edges[poc_i] + edges[poc_i + 1]) / 2)
    return VolumeProfile(
        poc,
        float(edges[hi_i + 1]),
        float(edges[lo_i]),
        [float(e) for e in edges],
        [float(v) for v in vols],
    )
