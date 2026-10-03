"""Alert evaluation (SPEC §8 alert types). Pure function; the job supplies prices and levels.

Levels come from the stock's latest report: the technical buy zone, fair value, top band and
invalidation. Each alert keeps a little state between runs (``alerts.state``) so it fires on
a *transition*, not on every run while the condition holds:

    enters_buy_zone       fires when price is inside [low, high] and was not inside at the
                          previous observation (the first observation counts: an alert created
                          while price is already in the zone fires once).
    crosses_fv / crosses_top_band / crosses_invalidation
                          fire when price is on the other side of the level than at the
                          previous observation (either direction; the message says which).
                          The first observation only records the side.

Hysteresis (``jobs.alerts.hysteresis_pct``): price within that band of a level (or of the
zone edges) keeps the previous state, so a quote wobbling on the line does not flip it.
Cooldown (``jobs.alerts.cooldown_minutes``): an alert that fired recently does not fire again;
the transition is still recorded, so it is not replayed later.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.core.config import AlertsJobConfig

AlertKind = Literal["enters_buy_zone", "crosses_fv", "crosses_top_band", "crosses_invalidation"]
Side = Literal["above", "below"]

LEVEL_NAME: dict[str, str] = {
    "crosses_fv": "fair value",
    "crosses_top_band": "top band",
    "crosses_invalidation": "invalidation",
}


@dataclass(frozen=True)
class Levels:
    buy_zone_low: float | None = None
    buy_zone_high: float | None = None
    fair_value: float | None = None
    top_band: float | None = None
    invalidation: float | None = None

    def level_for(self, kind: str) -> float | None:
        return {
            "crosses_fv": self.fair_value,
            "crosses_top_band": self.top_band,
            "crosses_invalidation": self.invalidation,
        }.get(kind)


@dataclass(frozen=True)
class Evaluation:
    fired: bool
    state: dict[str, Any]
    message: str | None = None  # set when fired
    reasons: list[str] = field(default_factory=list)


def side_of(price: float, level: float, band: float) -> Side | None:
    """``above`` / ``below`` once price clears ``level`` by ``band``; ``None`` on the line."""
    if price > level * (1 + band):
        return "above"
    if price < level * (1 - band):
        return "below"
    return None


def inside_zone(price: float, low: float, high: float, band: float) -> bool | None:
    """True inside [low, high]; False once clearly outside (beyond ``band``); else None."""
    if low <= price <= high:
        return True
    if price > high * (1 + band) or price < low * (1 - band):
        return False
    return None


def _fmt(v: float) -> str:
    return f"₹{v:,.2f}"


def evaluate(
    kind: str,
    price: float,
    levels: Levels,
    state: dict[str, Any] | None,
    cfg: AlertsJobConfig,
    *,
    now: datetime,
    last_fired_at: datetime | None,
    threshold: float | None = None,
    next_results: date | None = None,
) -> Evaluation:
    prev = dict(state or {})
    band = cfg.hysteresis_pct
    cooling = last_fired_at is not None and now - last_fired_at < timedelta(
        minutes=cfg.cooldown_minutes
    )
    base = {**prev, "last_price": price, "last_seen": now.isoformat()}

    if kind == "enters_buy_zone":
        lo, hi = levels.buy_zone_low, levels.buy_zone_high
        if lo is None or hi is None:
            return Evaluation(False, base, reasons=["no technical buy zone in the latest report"])
        now_inside = inside_zone(price, lo, hi, band)
        was_inside = prev.get("inside")
        inside = was_inside if now_inside is None else now_inside
        new_state = {**base, "inside": inside}
        entered = now_inside is True and was_inside is not True
        if not entered:
            where = "inside" if inside else "outside" if inside is False else "at the edge of"
            return Evaluation(
                False, new_state, reasons=[f"price {_fmt(price)} {where} the buy zone"]
            )
        if cooling:
            return Evaluation(
                False, new_state, reasons=["entered the buy zone (cooldown: not sent)"]
            )
        msg = f"entered the buy zone {_fmt(lo)} to {_fmt(hi)} at {_fmt(price)}"
        return Evaluation(True, new_state, msg, [msg])

    if kind in ("price_above", "price_below"):
        return _price_alert(kind, price, threshold, prev, base, cooling)
    if kind == "results_date":
        return _results_alert(threshold, next_results, now.date(), cfg, base, prev)
    if kind not in LEVEL_NAME:
        return Evaluation(False, base, reasons=[f"unknown alert type {kind!r}"])
    name = LEVEL_NAME[kind]
    level = levels.level_for(kind)
    if level is None:
        return Evaluation(False, base, reasons=[f"no {name} in the latest report"])
    now_side = side_of(price, level, band)
    was_side = prev.get("side")
    side = was_side if now_side is None else now_side
    new_state = {**base, "side": side}
    crossed = was_side is not None and now_side is not None and now_side != was_side
    if not crossed:
        if was_side is None:
            text = f"first observation: price {_fmt(price)} {side or 'at'} {name} {_fmt(level)}"
        else:
            text = f"price {_fmt(price)} still {side} {name} {_fmt(level)}"
        return Evaluation(False, new_state, reasons=[text])
    if cooling:
        return Evaluation(
            False, new_state, reasons=[f"crossed {now_side} {name} (cooldown: not sent)"]
        )
    msg = f"crossed {now_side} {name} {_fmt(level)} at {_fmt(price)}"
    return Evaluation(True, new_state, msg, [msg])


def _price_alert(
    kind: str,
    price: float,
    threshold: float | None,
    prev: dict[str, Any],
    base: dict[str, Any],
    cooling: bool,
) -> Evaluation:
    """Fires when price moves onto the alert's side of ``threshold`` (not while it stays)."""
    if threshold is None:
        return Evaluation(False, base, reasons=["no price set on this alert"])
    met = price >= threshold if kind == "price_above" else price <= threshold
    new_state = {**base, "met": met}
    word = "at or above" if kind == "price_above" else "at or below"
    if not met or prev.get("met") is True:
        return Evaluation(False, new_state, reasons=[f"price {_fmt(price)} vs {_fmt(threshold)}"])
    if cooling:
        return Evaluation(False, new_state, reasons=[f"{word} {_fmt(threshold)} (cooldown)"])
    msg = f"is {word} {_fmt(threshold)} (now {_fmt(price)})"
    return Evaluation(True, new_state, msg, [msg])


def _results_alert(
    days: float | None,
    next_results: date | None,
    today: date,
    cfg: AlertsJobConfig,
    base: dict[str, Any],
    prev: dict[str, Any],
) -> Evaluation:
    """Fires once per results date, ``days`` (default ``results_days_before``) before it."""
    if next_results is None:
        return Evaluation(False, base, reasons=["no upcoming results date known"])
    lead = int(days) if days is not None else cfg.results_days_before
    left = (next_results - today).days
    if left < 0 or left > lead:
        return Evaluation(False, base, reasons=[f"results on {next_results} ({left} days away)"])
    if prev.get("notified_for") == next_results.isoformat():
        return Evaluation(False, base, reasons=[f"already notified for {next_results}"])
    msg = f"announces results on {next_results:%d %b %Y} ({left} day(s) away)"
    return Evaluation(True, {**base, "notified_for": next_results.isoformat()}, msg, [msg])
