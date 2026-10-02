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
from fractions import Fraction
from typing import Literal

import numpy as np
import pandas as pd

from app.core.config import AdjustmentConfig, SuspiciousMovesConfig
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
    suspicious: list["SuspiciousMove"] = field(default_factory=list)
    changed: int = 0  # bars whose stored adjustment changed (set by jobs.common.readjust)


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


def _source_masks(
    dates: pd.DatetimeIndex,
    ex: pd.Timestamp,
    step: float,
    kind: str,
    close: pd.Series | None,
    sources: pd.Series,
    config: AdjustmentConfig,
    warnings: list[str],
) -> list[np.ndarray]:
    """For one action: the bars before ``ex`` to adjust, per source. A source's own closes
    across the ex-date decide (its last bar before against the first bar on/after ex, of any
    source); inconclusive → its ``prices_already_adjusted`` flag."""
    src = pd.Series(sources.to_numpy(), index=dates).astype(object)
    out: list[np.ndarray] = []
    before = dates < ex
    for name in sorted({str(v) for v in src[before].dropna().unique()}):
        mine = (src == name).to_numpy()
        flag = config.prices_already_adjusted.get(name, "detect")
        looks = None
        if config.detect_preadjusted and close is not None:
            cl = close.copy()
            cl[(dates < ex) & ~mine] = np.nan  # before ex: this source only
            looks = _closer_to_step(_raw_move(cl, ex), step)
        if looks is None:
            adjusted = flag == "yes"
            how = f"providers.yaml says {flag}" + (" (no bars to check)" if flag != "yes" else "")
        else:
            adjusted = not looks
            how = "detected"
            if (flag == "yes" and looks) or (flag == "no" and not looks):
                warnings.append(f"{kind} on {ex.date()}: {name} prices "
                                f"{'are raw' if looks else 'are already adjusted'} at the "
                                f"ex-date although providers.yaml says {flag!r}; using what "
                                "the prices show")  # fmt: skip
        if adjusted:
            warnings.append(f"{kind} on {ex.date()}: {name} bars already adjusted ({how}); "
                            "not applied again")  # fmt: skip
            continue
        out.append(before & mine)
    unsourced = before & src.isna().to_numpy()
    if unsourced.any():
        out.append(unsourced)
    return out


def adjustment_factors(
    dates: pd.DatetimeIndex,
    actions: pd.DataFrame,
    *,
    close: pd.Series | None = None,
    config: AdjustmentConfig | None = None,
    sources: pd.Series | None = None,
) -> tuple[pd.Series, list[str], pd.Timestamp | None]:
    """Per-bar factor (NaN where unknown), warnings, and the ex-date before which bars are
    unadjustable (``None`` if all are adjustable).

    With ``config`` (and the raw ``close``), an action is applied once only:

    - the same split/bonus from two sources with ex-dates up to ``duplicate_window_days``
      apart (e.g. NSE's bonus and yfinance's "split" a day off) counts once, on the date the
      raw prices actually move;
    - with ``detect_preadjusted``, an action whose ex-date shows no matching move in the raw
      closes (the price source already adjusted its history) is not applied again;
    - with ``sources`` (the provider of each bar), that decision is made per source: the bars
      of a source that already adjusted for the action are left alone, the others are
      adjusted, so a series stitched from an adjusted and a raw source is adjusted exactly
      once. ``prices_already_adjusted`` gives each source's expected state; the detector
      checks it and wins when conclusive;
    - an action marked ``price_adjusted_by_source`` (the owner applied a suggested fix) is
      never applied.
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
        owner = rec.get("price_adjusted_by_source")
        if owner is True:
            warnings.append(f"{kind} on {ex.date()}: marked as already in the source's prices "
                            "(owner's fix); not applied")  # fmt: skip
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
        kept.append((ex, step, kind if owner is not False else f"{kind}!"))

    for ex, step, kind in kept:
        if kind.endswith("!"):  # the owner confirmed the prices are raw: always applied
            factor[dates < ex] *= step
            continue
        if sources is not None and config is not None:
            for mask in _source_masks(dates, ex, step, kind, close, sources, config, warnings):
                factor[mask] *= step
            continue
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
    raw: pd.DataFrame,
    actions: pd.DataFrame,
    config: AdjustmentConfig | None = None,
    sources: pd.Series | None = None,
) -> AdjustmentResult:
    """``raw``: date-indexed ``open, high, low, close, volume`` (unadjusted).
    ``actions``: rows with ``ex_date, action_type, ratio_old, ratio_new``. ``config``: the
    providers.yaml ``adjustment`` block (duplicate window, pre-adjusted detection, the gap
    that is reported as abnormal after adjustment). ``sources``: each bar's provider
    (source-aware adjustment, see :func:`adjustment_factors`)."""
    idx = pd.DatetimeIndex(raw.index).normalize()
    close = pd.Series(raw["close"].astype(float).to_numpy(), index=idx)
    factor, warnings, blocked = adjustment_factors(idx, actions, close=close, config=config,
                                                   sources=sources)  # fmt: skip
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


# ───────────────────────── data-quality checks ─────────────────────────


@dataclass(frozen=True)
class SuspiciousMove:
    """An adjusted close-to-close move that looks like a split / bonus (SPEC §3.2 "price
    adjustment"): a missing action, one applied to bars that already had it, or one on record
    that was not applied. The suggested fix is applied only by the owner (one click)."""

    day: date
    ratio: float  # adjusted close / previous adjusted close
    candidate: float  # the action-like ratio it is near
    volume_ratio: float | None  # median adjusted volume after / before
    volume_confirmed: bool
    kind: Literal["missing_action", "double_adjusted", "not_applied"]
    action_ex_date: date | None  # the action on record near it
    ratio_old: int | None  # missing_action: the suggested action (ratio_new for ratio_old)
    ratio_new: int | None
    text: str


def suspicious_moves(adjusted: pd.DataFrame, actions: pd.DataFrame | None,
                     cfg: SuspiciousMovesConfig) -> list[SuspiciousMove]:  # fmt: skip
    """``adjusted``: date-indexed ``adj_close`` and ``adj_volume``. A move within
    ``ratio_tolerance`` of one of ``ratios`` is listed; it is confirmed when the volume moves
    the other way by ``volume_confirmation`` (a real share-count change does both)."""
    if adjusted.empty or "adj_close" not in adjusted.columns:
        return []
    idx = pd.DatetimeIndex(adjusted.index).normalize()
    close = pd.Series(adjusted["adj_close"].astype(float).to_numpy(), index=idx)
    vol = None
    if "adj_volume" in adjusted.columns:
        v = pd.to_numeric(adjusted["adj_volume"], errors="coerce").astype(float)
        vol = pd.Series(v.to_numpy(), index=idx)
    values = close.to_numpy()
    vols = vol.to_numpy() if vol is not None else None
    ex_dates = []
    if actions is not None and not actions.empty:
        ex_dates = [pd.Timestamp(r["ex_date"]).normalize() for r in actions.to_dict("records")
                    if str(r.get("action_type")) in ADJUSTING]  # fmt: skip
    window = pd.Timedelta(days=cfg.action_window_days)
    out: list[SuspiciousMove] = []
    for pos in range(1, len(values)):
        prev_close, now = values[pos - 1], values[pos]
        if not (np.isfinite(prev_close) and np.isfinite(now)) or prev_close <= 0:
            continue
        r = float(now / prev_close)
        c = next((c for c in cfg.ratios if abs(r / c - 1) <= cfg.ratio_tolerance), None)
        if c is None:
            continue
        d = pd.Timestamp(idx[pos])
        vr = None
        if vols is not None:
            before = vols[max(0, pos - cfg.volume_days) : pos]
            after = vols[pos : pos + cfg.volume_days]
            before, after = before[np.isfinite(before)], after[np.isfinite(after)]
            if len(before) and len(after) and float(np.median(before)) > 0:
                vr = float(np.median(after)) / float(np.median(before))
        confirmed = vr is not None and (vr >= cfg.volume_confirmation if c < 1
                                        else vr <= 1 / cfg.volume_confirmation)  # fmt: skip
        near = next((e for e in ex_dates if abs(e - d) <= window), None)
        frac = Fraction(c).limit_denominator(4)
        if near is None:
            kind: Literal["missing_action", "double_adjusted", "not_applied"] = "missing_action"
            old, new = frac.numerator, frac.denominator
            what = (f"no split/bonus on record: suggested {old}:{new} (ratio_new {new} shares "
                    f"for {old}) ex {d.date()}")  # fmt: skip
        elif c > 1:
            kind, old, new = "double_adjusted", None, None
            what = (f"the split/bonus ex {near.date()} looks applied to bars that already had "
                    "it: suggested fix: mark it as already in the source's prices")  # fmt: skip
        else:
            kind, old, new = "not_applied", None, None
            what = (f"the split/bonus ex {near.date()} looks not applied: suggested fix: mark "
                    "the prices as raw so it is applied")  # fmt: skip
        vol_txt = (f"volume x{vr:.2f}" + ("" if confirmed else " (does not confirm)")
                   if vr is not None else "no volume to confirm")  # fmt: skip
        out.append(SuspiciousMove(
            d.date(), float(r), float(c), vr, confirmed, kind,
            near.date() if near is not None else None, old, new,
            f"adjusted close x{r:.3f} on {d.date()} (near {frac}), {vol_txt}: {what}",
        ))  # fmt: skip
    return out


def abnormal_gaps(adjusted: pd.DataFrame, period: int, multiple: float) -> pd.DataFrame:
    """Overnight gaps |open / previous close - 1| above ``multiple`` x the previous day's
    ATR(``period``) / close: the "normal move". Columns gap, normal; empty when clean."""
    o, h, low, c = (adjusted[f"adj_{k}"].astype(float) for k in ("open", "high", "low", "close"))
    prev = c.shift(1)
    tr = pd.concat([h - low, (h - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    normal = (tr.rolling(period).mean() / c).shift(1)
    gap = (o / prev - 1).abs()
    bad = gap > multiple * normal
    return pd.DataFrame({"gap": gap[bad], "normal": normal[bad]})


def largest_overnight_gap(close: pd.Series) -> float:
    """Largest absolute close-to-close move, as a fraction (diagnostic for missed actions)."""
    returns = close.astype(float).pct_change().abs().dropna()
    return float(returns.max()) if not returns.empty else 0.0
