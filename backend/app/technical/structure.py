"""Market structure (SPEC §6): fractal swing points, HH/HL/LH/LL labels, break of structure
(BOS), change of character (CHoCH) and trend state. Pure functions.

Swing high at bar i: high_i is strictly above the N highs before it and at least as high as the
N highs after it (ties resolve to the earliest bar); swing lows mirror this. A swing is only
*confirmed* N bars later, so the last N bars never carry a swing — no look-ahead.

Labels compare each swing with the previous swing of the same kind (HH / LH for highs,
HL / LL for lows). Trend: last high HH and last low HL → up; LH and LL → down; else range.

BOS / CHoCH: walking forward, once a swing is confirmed its level becomes the active
resistance (high) or support (low). The first close beyond it is a break: in the direction of
the prevailing break sequence it is a BOS, against it a CHoCH. Each level breaks once.
"""

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

Kind = Literal["high", "low"]


@dataclass(frozen=True)
class Swing:
    index: int
    time: pd.Timestamp
    price: float
    kind: Kind
    label: str | None  # HH / LH / HL / LL (None for the first of each kind)


@dataclass(frozen=True)
class StructureEvent:
    index: int
    time: pd.Timestamp
    price: float  # the broken swing level
    kind: Literal["bos", "choch"]
    direction: Literal["bull", "bear"]
    swing_time: pd.Timestamp


@dataclass(frozen=True)
class Structure:
    swings: list[Swing]
    events: list[StructureEvent]
    trend: Literal["up", "down", "range"]
    reasons: list[str] = field(default_factory=list)

    def highs(self) -> list[Swing]:
        return [s for s in self.swings if s.kind == "high"]

    def lows(self) -> list[Swing]:
        return [s for s in self.swings if s.kind == "low"]


def swing_points(bars: pd.DataFrame, n: int) -> list[Swing]:
    highs, lows = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    idx = pd.DatetimeIndex(bars.index)
    raw: list[tuple[int, Kind, float]] = []
    for i in range(n, len(bars) - n):
        left_h, right_h = highs[i - n : i], highs[i + 1 : i + n + 1]
        if highs[i] > left_h.max() and highs[i] >= right_h.max():
            raw.append((i, "high", float(highs[i])))
        left_l, right_l = lows[i - n : i], lows[i + 1 : i + n + 1]
        if lows[i] < left_l.min() and lows[i] <= right_l.min():
            raw.append((i, "low", float(lows[i])))
    out: list[Swing] = []
    last: dict[str, float] = {}
    for i, kind, price in raw:
        prev = last.get(kind)
        if prev is None:
            label = None
        elif kind == "high":
            label = "HH" if price > prev else "LH"
        else:
            label = "HL" if price > prev else "LL"
        last[kind] = price
        out.append(Swing(i, idx[i], price, kind, label))
    return out


def trend_state(swings: list[Swing]) -> Literal["up", "down", "range"]:
    highs = [s for s in swings if s.kind == "high" and s.label]
    lows = [s for s in swings if s.kind == "low" and s.label]
    if not highs or not lows:
        return "range"
    h, lo = highs[-1].label, lows[-1].label
    if h == "HH" and lo == "HL":
        return "up"
    if h == "LH" and lo == "LL":
        return "down"
    return "range"


def structure_events(bars: pd.DataFrame, swings: list[Swing], n: int) -> list[StructureEvent]:
    closes = bars["close"].to_numpy(float)
    idx = pd.DatetimeIndex(bars.index)
    confirm_at: dict[int, list[Swing]] = {}
    for s in swings:
        confirm_at.setdefault(s.index + n, []).append(s)
    active_high: Swing | None = None
    active_low: Swing | None = None
    bias: Literal["bull", "bear"] | None = None
    events: list[StructureEvent] = []
    for i in range(len(bars)):
        for s in confirm_at.get(i, []):
            if s.kind == "high":
                active_high = s
            else:
                active_low = s
        if active_high is not None and closes[i] > active_high.price:
            kind: Literal["bos", "choch"] = "choch" if bias == "bear" else "bos"
            events.append(
                StructureEvent(i, idx[i], active_high.price, kind, "bull", active_high.time)
            )
            bias, active_high = "bull", None
        elif active_low is not None and closes[i] < active_low.price:
            kind = "choch" if bias == "bull" else "bos"
            events.append(
                StructureEvent(i, idx[i], active_low.price, kind, "bear", active_low.time)
            )
            bias, active_low = "bear", None
    return events


def market_structure(bars: pd.DataFrame, n: int) -> Structure:
    swings = swing_points(bars, n)
    events = structure_events(bars, swings, n)
    trend = trend_state(swings)
    reasons = [f"trend {trend} from {len(swings)} swings (fractal {n})"]
    if events:
        e = events[-1]
        reasons.append(
            f"last event: {e.direction} {e.kind.upper()} at {e.price:,.2f} ({e.time.date()})"
        )
    return Structure(swings, events, trend, reasons)
