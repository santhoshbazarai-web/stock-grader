"""Split / bonus adjustment of raw daily prices (AGENTS.md rule 6). Pure functions.

A split or bonus with ratio ``ratio_old : ratio_new`` (shares before : shares after, as stored in
``corporate_actions``) multiplies the share count by ``ratio_new / ratio_old`` on its ex-date.
Every bar *before* the ex-date is scaled so the series is continuous with today's share count:

    factor(d)     = Π over actions with ex_date > d of (ratio_old / ratio_new)
    adj_price(d)  = raw_price(d) * factor(d)
    adj_volume(d) = raw_volume(d) / factor(d)

Bars on or after the latest action's ex-date have factor 1. Examples: bonus 1:1 (1 → 2 shares)
halves earlier prices; split ₹10 → ₹2 (1 → 5) divides them by 5.

Dividends are not adjusted (price-only adjustment, as used by the technical engine); rights
issues are not adjusted either (they need the rights price) and are reported as warnings.

Missing ratios are never guessed (rule 1): if a split/bonus has no usable ratio, the adjusted
columns for every bar *before* its ex-date are left NULL and the result says so. Bars after it
are still correct, because later actions do not depend on it.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.db.enums import CorporateActionType

PRICE_COLUMNS = ("open", "high", "low", "close")
ADJUSTING = frozenset({CorporateActionType.SPLIT.value, CorporateActionType.BONUS.value})


@dataclass
class AdjustmentResult:
    """``prices`` = input plus ``adj_factor, adj_open, adj_high, adj_low, adj_close,
    adj_volume``. ``complete`` is False when some bars could not be adjusted."""

    prices: pd.DataFrame
    complete: bool
    warnings: list[str] = field(default_factory=list)
    unadjustable_before: pd.Timestamp | None = None


def _usable(old: object, new: object) -> bool:
    try:
        o, n = float(old), float(new)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(o) and np.isfinite(n) and o > 0 and n > 0)


def adjustment_factors(
    dates: pd.DatetimeIndex, actions: pd.DataFrame
) -> tuple[pd.Series, list[str], pd.Timestamp | None]:
    """Per-bar factor (NaN where unknown), warnings, and the ex-date before which bars are
    unadjustable (``None`` if all are adjustable)."""
    factor = pd.Series(1.0, index=dates)
    warnings: list[str] = []
    blocked_before: pd.Timestamp | None = None
    if actions is None or actions.empty:
        return factor, warnings, None

    seen: dict[pd.Timestamp, list[float]] = {}
    for rec in actions.to_dict("records"):
        kind = str(rec.get("action_type"))
        ex = pd.Timestamp(rec["ex_date"]).normalize()
        if kind == CorporateActionType.RIGHTS.value:
            warnings.append(f"rights issue on {ex.date()} not adjusted")
            continue
        if kind not in ADJUSTING:
            continue
        old, new = rec.get("ratio_old"), rec.get("ratio_new")
        if not _usable(old, new):
            warnings.append(f"{kind} on {ex.date()} has no usable ratio; earlier bars left NULL")
            blocked_before = ex if blocked_before is None else max(blocked_before, ex)
            continue
        step = float(old) / float(new)  # type: ignore[arg-type]
        # The same event can arrive twice from different sources (NSE "bonus 1:1" and
        # yfinance "split x2" on one ex-date): apply an identical factor only once.
        if any(np.isclose(step, prior) for prior in seen.get(ex, [])):
            warnings.append(f"duplicate split/bonus on {ex.date()} counted once")
            continue
        seen.setdefault(ex, []).append(step)
        factor[dates < ex] *= step

    if blocked_before is not None:
        factor[dates < blocked_before] = np.nan
    return factor, warnings, blocked_before


def adjust_prices(raw: pd.DataFrame, actions: pd.DataFrame) -> AdjustmentResult:
    """``raw``: date-indexed ``open, high, low, close, volume`` (unadjusted).
    ``actions``: rows with ``ex_date, action_type, ratio_old, ratio_new``."""
    idx = pd.DatetimeIndex(raw.index).normalize()
    factor, warnings, blocked = adjustment_factors(idx, actions)
    out = raw.copy()
    out["adj_factor"] = factor.to_numpy()
    for col in PRICE_COLUMNS:
        out[f"adj_{col}"] = out[col] * out["adj_factor"]
    adj_volume = (out["volume"] / out["adj_factor"]).round()
    out["adj_volume"] = adj_volume.astype("Int64")
    return AdjustmentResult(out, blocked is None, warnings, blocked)


def largest_overnight_gap(close: pd.Series) -> float:
    """Largest absolute close-to-close move, as a fraction (diagnostic for missed actions)."""
    returns = close.astype(float).pct_change().abs().dropna()
    return float(returns.max()) if not returns.empty else 0.0
