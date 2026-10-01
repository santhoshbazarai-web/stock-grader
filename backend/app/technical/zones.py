"""Supply/demand zones, order blocks, fair-value gaps and the dealing range (SPEC §6).
Pure functions over weekly bars with an ATR series.

Zones
    Impulse: a bar whose range (high - low) exceeds ``zone_impulse_atr_mult`` x ATR (ATR of the
    bar before it, so the impulse does not inflate its own yardstick). Bullish if it closes
    above its open → demand below it; bearish → supply above it.
    Base: the consecutive bars right before the impulse whose range is at most
    ``zone_base_max_atr_mult`` x ATR, up to ``zone_max_base_candles``. Zone = [min low, max high]
    of the base. No base bars → no base zone for that impulse.
    Order block: the last opposite-colour bar before the impulse (bearish bar before a bullish
    impulse) → zone = its [low, high].
    Retests: after the impulse, each *entry* into the zone (a bar reaching it after a bar that did
    not) counts once. A close through the far side (below a demand zone's low / above a supply
    zone's high) breaks the zone. Fresh = not broken and retests <= ``zone_max_retests_fresh``.

Fair-value gap (three bars i-1, i, i+1): bullish if low_{i+1} > high_{i-1} → gap
[high_{i-1}, low_{i+1}]; bearish if high_{i+1} < low_{i-1} → gap [high_{i+1}, low_{i-1}].
Filled once later price trades through the whole gap.

Dealing range: last major swing low and high. If the low came first (up-leg), equilibrium is the
midpoint and the OTE (optimal trade entry) is the ``ote_retracement`` retracement of the leg down
from the high: [high - 0.79 x range, high - 0.618 x range]. A down-leg mirrors this upwards.
"""

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.structure import Swing

Side = Literal["demand", "supply"]


@dataclass
class Zone:
    side: Side
    source: Literal["base", "order_block"]
    bottom: float
    top: float
    start: pd.Timestamp  # first base bar (or the order-block bar)
    impulse: pd.Timestamp  # the impulse bar that created the zone
    retests: int = 0
    broken: bool = False
    broken_at: pd.Timestamp | None = None
    fresh: bool = True

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class Fvg:
    direction: Literal["bull", "bear"]
    bottom: float
    top: float
    time: pd.Timestamp  # the middle bar
    filled: bool
    filled_at: pd.Timestamp | None


@dataclass(frozen=True)
class DealingRange:
    low: float
    high: float
    low_time: pd.Timestamp
    high_time: pd.Timestamp
    direction: Literal["up", "down"]  # up-leg: low before high
    equilibrium: float
    ote_low: float
    ote_high: float
    premium: bool | None  # close above equilibrium (None if no close given)
    reasons: list[str] = field(default_factory=list)


def _track(zone: Zone, bars: pd.DataFrame, after: int, max_fresh: int) -> Zone:
    inside_prev = False
    highs = bars["high"].to_numpy(float)
    lows = bars["low"].to_numpy(float)
    closes = bars["close"].to_numpy(float)
    for j in range(after + 1, len(bars)):
        hi, lo, cl = highs[j], lows[j], closes[j]
        if zone.side == "demand":
            if cl < zone.bottom:
                zone.broken, zone.broken_at = True, bars.index[j]
                break
            touching = lo <= zone.top
        else:
            if cl > zone.top:
                zone.broken, zone.broken_at = True, bars.index[j]
                break
            touching = hi >= zone.bottom
        if touching and not inside_prev:
            zone.retests += 1
        inside_prev = bool(touching)
    zone.fresh = not zone.broken and zone.retests <= max_fresh
    return zone


def supply_demand_zones(
    bars: pd.DataFrame, atr_series: pd.Series, cfg: TechnicalConfig
) -> list[Zone]:
    highs, lows = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    rng = highs - lows
    opens, closes = bars["open"].to_numpy(float), bars["close"].to_numpy(float)
    atr_prev = atr_series.shift(1).to_numpy(float)
    start = max(0, len(bars) - cfg.zone_lookback_weeks)
    zones: list[Zone] = []
    for i in range(start + 1, len(bars)):
        a = atr_prev[i]
        if not a > 0 or rng[i] <= cfg.zone_impulse_atr_mult * a:
            continue
        bullish = closes[i] > opens[i]
        side: Side = "demand" if bullish else "supply"
        # base: small-range bars directly before the impulse
        base: list[int] = []
        j = i - 1
        while j >= start and len(base) < cfg.zone_max_base_candles:
            if rng[j] <= cfg.zone_base_max_atr_mult * a:
                base.append(j)
                j -= 1
            else:
                break
        if base:
            lo = float(bars["low"].iloc[min(base) : i].min())
            hi = float(bars["high"].iloc[min(base) : i].max())
            zones.append(
                _track(
                    Zone(side, "base", lo, hi, bars.index[min(base)], bars.index[i]),
                    bars,
                    i,
                    cfg.zone_max_retests_fresh,
                )
            )
        # order block: last opposite-colour bar before the impulse
        for k in range(i - 1, max(start, i - 1 - cfg.zone_max_base_candles) - 1, -1):
            opposite = closes[k] < opens[k] if bullish else closes[k] > opens[k]
            if opposite:
                ob = Zone(
                    side,
                    "order_block",
                    float(lows[k]),
                    float(highs[k]),
                    bars.index[k],
                    bars.index[i],
                )
                zones.append(_track(ob, bars, i, cfg.zone_max_retests_fresh))
                break
    return zones


def fair_value_gaps(bars: pd.DataFrame) -> list[Fvg]:
    hi, lo = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    out = []
    for i in range(1, len(bars) - 1):
        if lo[i + 1] > hi[i - 1]:
            bottom, top, d = hi[i - 1], lo[i + 1], "bull"
            later = [j for j in range(i + 2, len(bars)) if lo[j] <= bottom]
        elif hi[i + 1] < lo[i - 1]:
            bottom, top, d = hi[i + 1], lo[i - 1], "bear"
            later = [j for j in range(i + 2, len(bars)) if hi[j] >= top]
        else:
            continue
        filled_at = bars.index[later[0]] if later else None
        out.append(Fvg(d, float(bottom), float(top), bars.index[i], bool(later), filled_at))  # type: ignore[arg-type]
    return out


def dealing_range(
    major_swings: list[Swing], cfg: TechnicalConfig, close: float | None = None
) -> DealingRange | None:
    highs = [s for s in major_swings if s.kind == "high"]
    lows = [s for s in major_swings if s.kind == "low"]
    if not highs or not lows:
        return None
    h, lo = highs[-1], lows[-1]
    size = h.price - lo.price
    if size <= 0:
        return None
    r1, r2 = cfg.ote_retracement
    eq = lo.price + size / 2
    if lo.index < h.index:  # up-leg: look for longs on a retracement down
        direction: Literal["up", "down"] = "up"
        ote_low, ote_high = h.price - r2 * size, h.price - r1 * size
    else:
        direction = "down"
        ote_low, ote_high = lo.price + r1 * size, lo.price + r2 * size
    premium = None if close is None else close > eq
    reasons = [
        f"dealing range {lo.price:,.2f}-{h.price:,.2f} ({direction}-leg), "
        f"equilibrium {eq:,.2f}, OTE {ote_low:,.2f}-{ote_high:,.2f}"
    ]
    return DealingRange(
        lo.price, h.price, lo.time, h.time, direction, eq, ote_low, ote_high, premium, reasons
    )
