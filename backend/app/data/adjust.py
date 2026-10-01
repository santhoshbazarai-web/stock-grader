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
from datetime import date

import numpy as np
import pandas as pd

from app.core.config import AdjustmentConfig
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


def _raw_move(close: pd.Series | None, ex: pd.Timestamp) -> float | None:
    """Last raw close on/after ``ex`` over the last one before it (None without both)."""
    if close is None:
        return None
    before, after = close[close.index < ex].dropna(), close[close.index >= ex].dropna()
    if before.empty or after.empty or float(before.iloc[-1]) <= 0:
        return None
    return float(after.iloc[0]) / float(before.iloc[-1])


def _closer_to_step(move: float | None, step: float) -> bool | None:
    """Does the raw move across the ex-date look like the action (True), like no action at
    all (False: the source already adjusted), or can't it tell (None)?"""
    if move is None or move <= 0:
        return None
    lm, ls = float(np.log(move)), float(np.log(step))
    return abs(lm - ls) < abs(lm)


def adjustment_factors(
    dates: pd.DatetimeIndex,
    actions: pd.DataFrame,
    *,
    close: pd.Series | None = None,
    config: AdjustmentConfig | None = None,
) -> tuple[pd.Series, list[str], pd.Timestamp | None]:
    """Per-bar factor (NaN where unknown), warnings, and the ex-date before which bars are
    unadjustable (``None`` if all are adjustable).

    With ``config`` (and the raw ``close``), an action is applied once only:

    - the same split/bonus from two sources with ex-dates up to ``duplicate_window_days``
      apart (e.g. NSE's bonus and yfinance's "split" a day off) counts once, on the date the
      raw prices actually move;
    - with ``detect_preadjusted``, an action whose ex-date shows no matching move in the raw
      closes (the price source already adjusted its history) is not applied again.
    """
    factor = pd.Series(1.0, index=dates)
    warnings: list[str] = []
    blocked_before: pd.Timestamp | None = None
    if actions is None or actions.empty:
        return factor, warnings, None
    window = pd.Timedelta(days=config.duplicate_window_days if config else 0)

    kept: list[tuple[pd.Timestamp, float, str]] = []  # (ex-date, step, kind)
    recs = sorted(actions.to_dict("records"), key=lambda r: pd.Timestamp(r["ex_date"]))
    for rec in recs:
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
        # yfinance "split x2"), on the same ex-date or one a day or two apart: apply it once.
        dup = next((i for i, (e, st, _) in enumerate(kept)
                    if np.isclose(step, st) and abs(ex - e) <= window), None)  # fmt: skip
        if dup is not None:
            e0, st0, _ = kept[dup]
            if ex == e0:
                warnings.append(f"duplicate split/bonus on {ex.date()} counted once")
                continue
            # keep whichever ex-date the raw prices confirm
            if _closer_to_step(_raw_move(close, ex), step) and not _closer_to_step(
                _raw_move(close, e0), st0
            ):
                kept[dup] = (ex, step, kind)
                use, drop = ex, e0
            else:
                use, drop = e0, ex
            warnings.append(f"{kind} on {drop.date()} duplicates the one on {use.date()} "
                            "(sources differ on the ex-date): counted once")  # fmt: skip
            continue
        kept.append((ex, step, kind))

    for ex, step, kind in kept:
        if config is not None and config.detect_preadjusted:
            looks = _closer_to_step(_raw_move(close, ex), step)
            if looks is False:
                warnings.append(f"{kind} on {ex.date()}: the raw prices show no move at "
                                "the ex-date (already adjusted by the source); not applied "
                                "again")  # fmt: skip
                continue
        factor[dates < ex] *= step

    if blocked_before is not None:
        factor[dates < blocked_before] = np.nan
    return factor, warnings, blocked_before


def adjust_prices(
    raw: pd.DataFrame, actions: pd.DataFrame, config: AdjustmentConfig | None = None
) -> AdjustmentResult:
    """``raw``: date-indexed ``open, high, low, close, volume`` (unadjusted).
    ``actions``: rows with ``ex_date, action_type, ratio_old, ratio_new``. ``config``: the
    providers.yaml ``adjustment`` block (duplicate window, pre-adjusted detection, the gap
    that is reported as abnormal after adjustment)."""
    idx = pd.DatetimeIndex(raw.index).normalize()
    close = pd.Series(raw["close"].astype(float).to_numpy(), index=idx)
    factor, warnings, blocked = adjustment_factors(idx, actions, close=close, config=config)
    out = raw.copy()
    out["adj_factor"] = factor.to_numpy()
    for col in PRICE_COLUMNS:
        out[f"adj_{col}"] = out[col] * out["adj_factor"]
    adj_volume = (out["volume"] / out["adj_factor"]).round()
    out["adj_volume"] = adj_volume.astype("Int64")
    if config is not None:
        adj_close = pd.Series(out["adj_close"].astype(float).to_numpy(), index=idx).dropna()
        moves = adj_close.pct_change().abs().dropna()
        big = moves[moves > config.abnormal_gap]
        for day, move in zip(pd.DatetimeIndex(big.index), big.to_numpy(), strict=True):
            warnings.append(f"adjusted close moves {move:.0%} on {day.date()}: "
                            "a split/bonus may be missing or doubled")  # fmt: skip
    return AdjustmentResult(out, blocked is None, warnings, blocked)


PER_SHARE_DIVIDE = ("eps_diluted", "book_value_per_share")  # ₹ per share: divided by the multiplier
PER_SHARE_MULTIPLY = ("shares_diluted_cr",)  # share counts: x multiplier


def share_multipliers(
    actions: pd.DataFrame, config: AdjustmentConfig | None = None
) -> list[tuple[pd.Timestamp, float, str]]:
    """Splits / bonuses with a usable ratio as (ex-date, shares-after / shares-before, text),
    the same event from two sources (ex-dates within ``duplicate_window_days``) once."""
    window = pd.Timedelta(days=config.duplicate_window_days if config else 0)
    out: list[tuple[pd.Timestamp, float, str]] = []
    if actions is None or actions.empty:
        return out
    for rec in sorted(actions.to_dict("records"), key=lambda r: pd.Timestamp(r["ex_date"])):
        kind = str(rec.get("action_type"))
        old, new = rec.get("ratio_old"), rec.get("ratio_new")
        if kind not in ADJUSTING or not _usable(old, new):
            continue
        ex = pd.Timestamp(rec["ex_date"]).normalize()
        mult = float(new) / float(old)  # type: ignore[arg-type]
        if any(np.isclose(mult, m) and abs(ex - e) <= window for e, m, _ in out):
            continue
        out.append((ex, mult, f"{kind} x{mult:g} on {ex.date()}"))
    return out


def restate_per_share(
    fin: pd.DataFrame,
    actions: pd.DataFrame,
    config: AdjustmentConfig | None = None,
    *,
    current: set[date] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Per-share figures and share counts on today's share basis, like adjusted prices.

    ``fin`` is indexed by period end (fin_annual / fin_quarterly). A row is restated for every
    split / bonus that went ex after it became known (its ``announcement_date``, else its
    period end): the filing reported the old share basis. One announced after the ex-date was
    already restated by the company (Ind AS 33) and is left alone. EPS and book value per
    share are divided by the multiplier, diluted shares multiplied."""
    mults = share_multipliers(actions, config)
    if fin.empty or not mults:
        return fin, []
    out = fin.copy()
    known = pd.to_datetime(pd.Series(out.index, index=out.index), errors="coerce")
    if "announcement_date" in out.columns:
        ann = pd.to_datetime(out["announcement_date"], errors="coerce")
        known = ann.where(ann.notna(), known)
    factor = pd.Series(1.0, index=out.index)
    for ex, mult, _ in mults:
        factor[(known < ex).to_numpy()] *= mult
    if current:  # rows whose source already reports today's share basis
        keep = [pd.Timestamp(i).date() in current for i in out.index]
        factor[keep] = 1.0
    changed = factor != 1.0
    if not changed.any():
        return out, []
    for col in PER_SHARE_DIVIDE:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") / factor
    for col in PER_SHARE_MULTIPLY:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") * factor
    notes = [f"per-share figures known before {ex.date()} restated for the {text}"
             for ex, _, text in mults if bool((known < ex).any())]  # fmt: skip
    return out, notes


def largest_overnight_gap(close: pd.Series) -> float:
    """Largest absolute close-to-close move, as a fraction (diagnostic for missed actions)."""
    returns = close.astype(float).pct_change().abs().dropna()
    return float(returns.max()) if not returns.empty else 0.0
