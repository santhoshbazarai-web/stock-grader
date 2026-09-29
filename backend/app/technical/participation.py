"""Participation (SPEC §6): delivery trend, up/down-week volume, VCP. Pure functions.

Delivery ratio  mean delivery % of the last ``delivery_recent_days`` sessions / mean of the
                last ``delivery_avg_days`` sessions (> 1: more delivery-based buying lately)
Up/down volume  total volume of up weeks / total volume of down weeks over
                ``updown_volume_weeks`` (a week is up if it closes above the prior close)
VCP             pullbacks (swing high → next swing low, as a fraction of the high) inside the
                last ``vcp.lookback_weeks``; detected when the last ``min_contractions`` or
                more pullbacks each get shallower, the last is no deeper than
                ``max_final_depth`` and the close is within ``max_distance_from_pivot`` of the
                last swing high (the pivot)
"""

from dataclasses import dataclass, field
from itertools import pairwise

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.structure import Swing


@dataclass(frozen=True)
class Vcp:
    detected: bool
    depths: list[float] = field(default_factory=list)
    pivot: float | None = None
    reasons: list[str] = field(default_factory=list)


def delivery_ratio(delivery_pct: pd.Series, cfg: TechnicalConfig) -> float | None:
    d = delivery_pct.dropna().astype(float)
    if len(d) < cfg.delivery_avg_days:
        return None
    base = float(d.iloc[-cfg.delivery_avg_days :].mean())
    recent = float(d.iloc[-cfg.delivery_recent_days :].mean())
    return recent / base if base > 0 else None


def up_down_volume_ratio(weekly: pd.DataFrame, cfg: TechnicalConfig) -> float | None:
    w = weekly.iloc[-(cfg.updown_volume_weeks + 1) :]
    if len(w) < cfg.updown_volume_weeks + 1:
        return None
    change = w["close"].diff().iloc[1:]
    vol = w["volume"].iloc[1:].astype(float)
    up, down = float(vol[change > 0].sum()), float(vol[change < 0].sum())
    return up / down if down > 0 else None


def vcp(weekly: pd.DataFrame, swings: list[Swing], cfg: TechnicalConfig) -> Vcp:
    c = cfg.vcp
    start = weekly.index[max(0, len(weekly) - c.lookback_weeks)]
    recent = [s for s in swings if s.time >= start]
    depths: list[float] = []
    last_high: Swing | None = None
    for s in recent:
        if s.kind == "high":
            last_high = s
        elif last_high is not None:
            depths.append(1 - s.price / last_high.price)
            last_high = None
    highs = [s for s in recent if s.kind == "high"]
    pivot = highs[-1].price if highs else None
    if len(depths) < c.min_contractions or pivot is None:
        return Vcp(False, depths, pivot, [f"{len(depths)} pullbacks in {c.lookback_weeks}w"])
    tail = depths[-c.min_contractions :]
    contracting = all(b < a for a, b in pairwise(tail))
    close = float(weekly["close"].iloc[-1])
    near = close >= pivot * (1 - c.max_distance_from_pivot)
    tight = tail[-1] <= c.max_final_depth
    detected = contracting and near and tight
    text = " → ".join(f"{d:.0%}" for d in tail)
    reasons = [
        f"VCP {'detected' if detected else 'not detected'}: pullbacks {text}, pivot {pivot:,.2f}"
    ]
    return Vcp(detected, depths, pivot, reasons)
