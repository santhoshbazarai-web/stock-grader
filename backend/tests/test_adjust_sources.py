"""Source-aware split/bonus adjustment and the price data-quality checks (app/data/adjust.py,
SPEC §3.2 "price adjustment").

HDFC Bank's split (ex 19 Sep 2019, 1 → 2) and 1:1 bonus (ex 27 Aug 2025) over a synthetic
random walk (seeded; not HDFC Bank's quotes). The stored history is stitched the way it can be
in practice: Fyers bars already adjusted by Fyers, then raw NSE bhavcopy bars from June 2025.
Adjusting each source on its own evidence leaves no overnight gap above 3x the ATR-based
normal move; adjusting the whole series once (the old way) halves the Fyers bars twice."""

import numpy as np
import pandas as pd
import pytest

from app.core.config import load_config
from app.data.adjust import abnormal_gaps, adjust_prices, suspicious_moves
from tests.conftest import REPO_CONFIG_DIR

ADJ = load_config(REPO_CONFIG_DIR).providers.adjustment
SUS = ADJ.suspicious
SPLIT, BONUS = pd.Timestamp("2019-09-19"), pd.Timestamp("2025-08-27")
NSE_FROM = pd.Timestamp("2025-06-02")
ACTIONS = pd.DataFrame([
    {"ex_date": SPLIT.date(), "action_type": "split", "ratio_old": 1, "ratio_new": 2},
    {"ex_date": BONUS.date(), "action_type": "bonus", "ratio_old": 1, "ratio_new": 2},
])  # fmt: skip


def true_series(seed: int = 7) -> pd.DataFrame:
    """OHLCV in today's share terms: ~1.2% daily moves, small overnight gaps."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-06-03", "2025-10-31")
    close = 1500 * np.exp(np.cumsum(rng.normal(0, 0.012, len(days))))
    prev = np.concatenate([[close[0]], close[:-1]])
    open_ = prev * np.exp(rng.normal(0, 0.003, len(days)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.008, len(days)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.008, len(days)))
    vol = rng.integers(800_000, 1_200_000, len(days)).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=days)  # fmt: skip


def as_traded(df: pd.DataFrame) -> pd.DataFrame:
    """Undo both actions: x4 before the split, x2 between split and bonus (volume inverse)."""
    out = df.copy()
    mult = pd.Series(1.0, index=df.index)
    mult[df.index < BONUS] *= 2
    mult[df.index < SPLIT] *= 2
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * mult
    out["volume"] = (out["volume"] / mult).round()
    return out


def stitched() -> tuple[pd.DataFrame, pd.Series]:
    """Fyers (already adjusted) to May 2025, raw NSE bhavcopy from June 2025."""
    true, raw = true_series(), as_traded(true_series())
    nse = true.index >= NSE_FROM
    stored = true.copy()
    stored[nse] = raw[nse]
    return stored, pd.Series(np.where(nse, "nse", "fyers"), index=true.index)


def gaps(result_prices: pd.DataFrame) -> pd.DataFrame:
    return abnormal_gaps(result_prices, SUS.atr_period, SUS.atr_multiple)


def test_true_series_is_clean() -> None:
    t = true_series().rename(columns=lambda c: f"adj_{c}")
    assert gaps(t).empty


def test_stitched_history_is_adjusted_once_per_source() -> None:
    stored, sources = stitched()
    res = adjust_prices(stored, ACTIONS, ADJ, sources=sources)
    true = true_series()
    np.testing.assert_allclose(res.prices["adj_close"].to_numpy(), true["close"].to_numpy(),
                               rtol=1e-9)  # fmt: skip
    assert gaps(res.prices).empty  # no overnight move > 3x ATR/close across both actions
    assert suspicious_moves(res.prices, ACTIONS, SUS) == []
    assert any("fyers bars already adjusted (detected)" in w and "2025-08-27" in w
               for w in res.warnings)  # fmt: skip
    assert any("split on 2019-09-19: fyers bars already adjusted" in w for w in res.warnings)


def test_the_old_whole_series_adjustment_doubles_the_fyers_bars() -> None:
    stored, _ = stitched()
    res = adjust_prices(stored, ACTIONS, ADJ)  # no sources: one decision for every bar
    bad = gaps(res.prices)
    assert pd.Timestamp("2025-06-02") in bad.index  # Fyers bars halved again: 2x jump
    [move] = [m for m in suspicious_moves(res.prices, ACTIONS, SUS)
              if m.day == NSE_FROM.date()]  # fmt: skip
    assert move.candidate == 2.0


def test_all_raw_fyers_is_adjusted_for_both_actions() -> None:
    raw = as_traded(true_series())
    sources = pd.Series("fyers", index=raw.index)
    res = adjust_prices(raw, ACTIONS, ADJ, sources=sources)
    np.testing.assert_allclose(res.prices["adj_close"].to_numpy(),
                               true_series()["close"].to_numpy(), rtol=1e-9)  # fmt: skip
    assert gaps(res.prices).empty


def test_the_detector_overrides_a_wrong_flag() -> None:
    raw = as_traded(true_series())
    sources = pd.Series("fyers", index=raw.index)
    cfg = ADJ.model_copy(update={"prices_already_adjusted": {"fyers": "yes"}})
    res = adjust_prices(raw, ACTIONS, cfg, sources=sources)
    assert gaps(res.prices).empty
    assert any("fyers prices are raw at the ex-date although providers.yaml says 'yes'" in w
               for w in res.warnings)  # fmt: skip


def test_a_missing_bonus_is_suggested_with_volume_confirmation() -> None:
    raw = as_traded(true_series())
    only_split = ACTIONS.iloc[:1]
    res = adjust_prices(raw, only_split, ADJ, sources=pd.Series("nse", index=raw.index))
    [m] = suspicious_moves(res.prices, only_split, SUS)
    assert m.day == BONUS.date() and m.kind == "missing_action"
    assert (m.ratio_old, m.ratio_new) == (1, 2) and m.volume_confirmed
    assert m.ratio == pytest.approx(0.5, rel=0.05) and m.volume_ratio == pytest.approx(2, rel=0.3)
    assert "no split/bonus on record: suggested 1:2" in m.text


def test_an_action_applied_to_adjusted_bars_is_flagged_double() -> None:
    true = true_series()
    cfg = ADJ.model_copy(update={"detect_preadjusted": False})
    res = adjust_prices(true, ACTIONS.iloc[1:], cfg)  # Fyers had adjusted; we halve again
    [m] = suspicious_moves(res.prices, ACTIONS.iloc[1:], SUS)
    assert m.kind == "double_adjusted" and m.action_ex_date == BONUS.date()
    assert m.volume_confirmed
    # the owner's fix: mark the action as already in the source's prices
    fixed = ACTIONS.iloc[1:].assign(price_adjusted_by_source=True)
    res2 = adjust_prices(true, fixed, cfg)
    assert suspicious_moves(res2.prices, fixed, SUS) == [] and gaps(res2.prices).empty


def test_an_action_not_applied_can_be_forced() -> None:
    raw = as_traded(true_series())
    cfg = ADJ.model_copy(update={"prices_already_adjusted": {"fyers": "yes"},
                                 "detect_preadjusted": False})  # fmt: skip
    src = pd.Series("fyers", index=raw.index)
    res = adjust_prices(raw, ACTIONS, cfg, sources=src)
    kinds = {m.kind for m in suspicious_moves(res.prices, ACTIONS, SUS)}
    assert kinds == {"not_applied"}
    forced = ACTIONS.assign(price_adjusted_by_source=False)
    res2 = adjust_prices(raw, forced, cfg, sources=src)
    assert gaps(res2.prices).empty and suspicious_moves(res2.prices, forced, SUS) == []
