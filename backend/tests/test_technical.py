"""Technical engine (SPEC §6) on small hand-built weekly bars; expected values worked out in
the comments."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from app.core.config import TechnicalConfig, load_config
from app.devtools.synthetic import synthetic_daily
from app.technical.avwap import anchored_vwap, avwaps
from app.technical.bars import atr, rsi, to_weekly
from app.technical.buy_zone import Support, buy_zone, valuation_range
from app.technical.engine import ValuationLevels, analyze, debug_payload
from app.technical.momentum import momentum
from app.technical.participation import delivery_ratio, up_down_volume_ratio, vcp
from app.technical.risk import invalidation, reward_risk
from app.technical.rs import mansfield_rs, percentile_rank
from app.technical.stage import weinstein_stage
from app.technical.structure import Swing, market_structure, swing_points
from app.technical.volume_profile import volume_profile
from app.technical.zones import dealing_range, fair_value_gaps, supply_demand_zones
from tests.conftest import REPO_CONFIG_DIR

CFG: TechnicalConfig = load_config(REPO_CONFIG_DIR).technical


def cfg(**kw: object) -> TechnicalConfig:
    return CFG.model_copy(update=kw)


def weeks(n: int, start: str = "2020-01-03") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="W-FRI")


def bars(
    highs: list[float],
    lows: list[float],
    closes: list[float] | None = None,
    opens: list[float] | None = None,
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    n = len(highs)
    closes = closes or [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    opens = opens or closes
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes or [1000.0] * n,
        },
        index=weeks(n),
    )


def closes_only(values: list[float] | np.ndarray, volume: float = 1000.0) -> pd.DataFrame:
    v = np.asarray(values, float)
    return pd.DataFrame(
        {"open": v, "high": v * 1.01, "low": v * 0.99, "close": v, "volume": volume},
        index=weeks(len(v)),
    )


# ───────────────────────── bars ─────────────────────────


def test_to_weekly_labels_with_last_trading_day() -> None:
    # Week of 13 Mar 2024: Mon-Thu trade, Friday 15 Mar is a holiday → label Thu 14 Mar
    days = pd.to_datetime(
        ["2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-18", "2024-03-22"]
    )
    d = pd.DataFrame(
        {
            "open": [10, 11, 12, 13, 20, 21],
            "high": [11, 15, 13, 14, 22, 25],
            "low": [9, 10, 8, 12, 19, 20],
            "close": [11, 12, 13, 14, 21, 24],
            "volume": [1, 2, 3, 4, 5, 6],
        },
        index=days,
    )
    w = to_weekly(d)
    assert list(w.index) == [pd.Timestamp("2024-03-14"), pd.Timestamp("2024-03-22")]
    assert w.iloc[0].to_dict() == {"open": 10, "high": 15, "low": 8, "close": 14, "volume": 10}
    assert w.iloc[1].to_dict() == {"open": 20, "high": 25, "low": 19, "close": 24, "volume": 11}


def test_atr_wilder() -> None:
    # TR: bar0 = 11-9 = 2; bar1 = max(14-10, |14-10|, |10-10|) = 4;
    #     bar2 = max(13-11, |13-12|, |11-12|) = 2; bar3 = max(12-10, |12-12|, |10-12|) = 2
    # period 2: seed (2 + 4)/2 = 3; then (3 + 2)/2 = 2.5; then (2.5 + 2)/2 = 2.25
    b = bars([11, 14, 13, 12], [9, 10, 11, 10], closes=[10, 12, 12, 11])
    a = atr(b, 2)
    assert math.isnan(a.iloc[0]) and list(a.iloc[1:]) == [3.0, 2.5, 2.25]


def test_rsi_wilder() -> None:
    # closes 10, 11, 12, 11, 13 → changes +1, +1, -1, +2; period 2
    #   seed: gain (1+1)/2 = 1, loss 0 → RSI 100
    #   next: gain (1*1+0)/2 = 0.5, loss (0+1)/2 = 0.5 → RS 1 → RSI 50
    #   next: gain (0.5+2)/2 = 1.25, loss 0.25 → RS 5 → RSI 83.33
    r = rsi(pd.Series([10.0, 11, 12, 11, 13]), 2)
    assert math.isnan(r.iloc[1])
    assert list(r.iloc[2:]) == pytest.approx([100.0, 50.0, 100 - 100 / 6])


# ───────────────────────── stage ─────────────────────────


def test_stage_2_advancing_with_volume_confirmation() -> None:
    b = closes_only(100 + np.arange(60.0))
    b.iloc[-1, b.columns.get_loc("volume")] = 3000.0  # 3x the 10-week average
    s = weinstein_stage(b, CFG)
    assert s.stage == 2 and s.breakout_volume_confirmed
    assert s.sma_slope is not None and s.sma_slope > CFG.stage_flat_slope_pct


def test_stage_4_declining() -> None:
    assert weinstein_stage(closes_only(200 - np.arange(60.0)), CFG).stage == 4


def test_stage_1_basing_after_decline() -> None:
    # 45 weeks down from 200 to 112, then 35 flat weeks at 115: the 30w SMA has flattened at
    # 115 (slope 0) while 13 weeks earlier it was still falling → stage 1 (basing)
    values = np.concatenate([200 - 2 * np.arange(45.0), np.full(35, 115.0)])
    s = weinstein_stage(closes_only(values), CFG)
    assert s.stage == 1 and "basing" in s.reasons[0]


def test_stage_3_topping_after_advance() -> None:
    # 45 weeks up, then price slips below a still-rising SMA → stage 3
    values = np.concatenate([100 + 2 * np.arange(45.0), np.full(15, 170.0)])
    s = weinstein_stage(closes_only(values), CFG)
    assert s.stage == 3


def test_stage_needs_enough_history() -> None:
    s = weinstein_stage(closes_only(np.arange(1.0, 30.0)), CFG)
    assert s.stage is None and "needs 47" in s.reasons[0]


# ───────────────────────── structure ─────────────────────────
# Up-trend (fractal n = 1):
#   highs  10 12 11 14 13 16 15   → swing highs at bars 1 (12), 3 (14 HH), 5 (16 HH)
#   lows    8  9  7 10  9 12 11   → swing lows at bars 2 (7), 4 (9 HL)
#   closes  9 11 10 13.5 12 15 14 → bar 3 closes above 12 (confirmed at bar 2): bull BOS;
#                                   bar 5 closes above 14 (confirmed at bar 4): bull BOS
UP = bars([10, 12, 11, 14, 13, 16, 15], [8, 9, 7, 10, 9, 12, 11], [9, 11, 10, 13.5, 12, 15, 14])

# Down-trend then reversal:
#   highs  20 18 19 16 17 14 15 19 → swing highs 19 (bar 2), 17 LH (bar 4)
#   lows   18 15 16 13 14 11 12 13 → swing lows 15 (bar 1), 13 LL (bar 3), 11 LL (bar 5)
#   closes 19 16 18 13.5 16 11.5 14 18
#   bar 3: 13.5 < 15 → bear BOS; bar 5: 11.5 < 13 → bear BOS; bar 7: 18 > 17 → bull CHoCH
DOWN = bars(
    [20, 18, 19, 16, 17, 14, 15, 19],
    [18, 15, 16, 13, 14, 11, 12, 13],
    [19, 16, 18, 13.5, 16, 11.5, 14, 18],
)


def test_swings_and_labels() -> None:
    s = swing_points(UP, 1)
    assert [(x.index, x.kind, x.price, x.label) for x in s] == [
        (1, "high", 12, None),
        (2, "low", 7, None),
        (3, "high", 14, "HH"),
        (4, "low", 9, "HL"),
        (5, "high", 16, "HH"),
    ]


def test_no_swing_in_the_last_n_bars() -> None:
    assert all(x.index < len(UP) - 2 for x in swing_points(UP, 2))


def test_bos_in_uptrend() -> None:
    m = market_structure(UP, 1)
    assert m.trend == "up"
    assert [(e.index, e.kind, e.direction, e.price) for e in m.events] == [
        (3, "bos", "bull", 12),
        (5, "bos", "bull", 14),
    ]


def test_choch_on_reversal() -> None:
    m = market_structure(DOWN, 1)
    assert [(e.index, e.kind, e.direction, e.price) for e in m.events] == [
        (3, "bos", "bear", 15),
        (5, "bos", "bear", 13),
        (7, "choch", "bull", 17),
    ]
    assert m.trend == "down"  # last swing high LH, last swing low LL


# ───────────────────────── zones ─────────────────────────
# ATR held at 2 (so impulse > 4, base range <= 2).
#   bars 0-1: filler (range 3: too wide for a base, too narrow for an impulse);
#   bars 2-4 base (ranges 1.5, 1, 2) → demand zone [99.5, 102]
#   bar 3 is bearish (close < open) → order block [99.5, 100.5]
#   bar 5 impulse up: 101 → 110 (range 10); bars 6-7 stay above; bar 8 dips to 101.5 (retest 1);
#   bar 9 back up; bar 10 dips to 100 (retest 2 → no longer fresh)
ZH = [104, 104, 101.5, 100.5, 102, 111, 112, 113, 110, 112, 108]
ZL = [101, 101, 100, 99.5, 100, 101, 108, 109, 101.5, 107, 100]
ZO = [103, 103, 100.5, 100.4, 100.5, 101, 109, 110, 109, 108, 107]
ZC = [103, 103, 100.8, 100.0, 101.5, 110, 111, 112, 104, 111, 101]


def _zones(
    highs: list[float] = ZH, lows: list[float] = ZL, closes: list[float] = ZC, max_fresh: int = 1
):  # type: ignore[no-untyped-def]
    b = bars(highs, lows, closes, opens=ZO[: len(highs)])
    const_atr = pd.Series(2.0, index=b.index)
    return supply_demand_zones(
        b, const_atr, cfg(zone_max_retests_fresh=max_fresh, zone_lookback_weeks=500)
    )


def test_demand_zone_from_base_and_order_block() -> None:
    zones = _zones(ZH[:8], ZL[:8], ZC[:8])
    base = next(z for z in zones if z.source == "base")
    ob = next(z for z in zones if z.source == "order_block")
    assert (base.side, base.bottom, base.top) == ("demand", 99.5, 102)
    assert base.start == weeks(8)[2] and base.impulse == weeks(8)[5]
    assert (ob.bottom, ob.top) == (99.5, 100.5)
    assert base.retests == 0 and base.fresh


def test_retests_end_freshness() -> None:
    base = next(z for z in _zones() if z.source == "base")
    assert base.retests == 2 and not base.fresh and not base.broken
    assert next(z for z in _zones(max_fresh=2) if z.source == "base").fresh


def test_close_through_the_zone_breaks_it() -> None:
    closes = [*ZC[:10], 98.0]
    base = next(z for z in _zones(closes=closes) if z.source == "base")
    assert base.broken and base.broken_at == weeks(11)[10] and not base.fresh


def test_fair_value_gaps() -> None:
    # bar0 high 10, bar2 low 12 → bullish gap [10, 12] at bar 1; bar 4 trades down to 9.5 → filled
    b = bars([10, 14, 15, 14, 13], [8, 10, 12, 11, 9.5])
    [g] = [g for g in fair_value_gaps(b) if g.direction == "bull"]
    assert (g.bottom, g.top, g.time) == (10, 12, b.index[1])
    assert g.filled and g.filled_at == b.index[4]


def test_dealing_range_equilibrium_and_ote() -> None:
    # Up-leg from 100 to 200: EQ 150; OTE = 200 - 0.79 x 100 .. 200 - 0.618 x 100 = 121 .. 138.2
    t = weeks(2)
    swings = [Swing(0, t[0], 100.0, "low", None), Swing(1, t[1], 200.0, "high", None)]
    d = dealing_range(swings, CFG, close=160.0)
    assert d is not None and d.direction == "up"
    assert (d.equilibrium, d.ote_low, d.ote_high) == (150, pytest.approx(121), pytest.approx(138.2))
    assert d.premium is True
    down = dealing_range(
        [Swing(0, t[0], 200.0, "high", None), Swing(1, t[1], 100.0, "low", None)], CFG
    )
    assert down is not None and (down.ote_low, down.ote_high) == (
        pytest.approx(161.8),
        pytest.approx(179),
    )


# ───────────────────────── AVWAP & volume profile ─────────────────────────


def test_anchored_vwap() -> None:
    # typical prices (h+l+c)/3: 10, 20, 30 with volumes 100, 300, 100
    # AVWAP from bar 0: 10, (1000+6000)/400 = 17.5, (7000+3000)/500 = 20
    b = bars([11, 21, 31], [9, 19, 29], [10, 20, 30], volumes=[100, 300, 100])
    assert list(anchored_vwap(b, 0)) == pytest.approx([10.0, 17.5, 20.0])
    assert list(anchored_vwap(b, 1)) == pytest.approx([20.0, 22.5])


def test_avwap_anchors() -> None:
    b = bars([11, 9, 21, 31], [9, 5, 19, 29], [10, 7, 20, 30], volumes=[100] * 4)
    major = [Swing(1, b.index[1], 5.0, "low", None)]
    got = {
        a.anchor: a
        for a in avwaps(
            b,
            cfg(avwap_anchors=["low_52w", "last_major_swing_low", "last_results_date"]),
            major_swings=major,
            last_results_date=b.index[2] - pd.Timedelta(days=1),
        )
    }
    assert got["low_52w"].anchor_time == b.index[1]  # lowest low (5)
    assert got["last_major_swing_low"].anchor_time == b.index[1]
    assert got["last_results_date"].anchor_time == b.index[2]  # first bar on/after the date


def test_volume_profile_hand_computed() -> None:
    # 5 bins over 100-110 (edges 100, 102, 104, 106, 108, 110):
    #   bar A 100-110, vol 500 → 100 per bin; bar B 104-106, vol 400 → all in bin 2
    #   bins: 100, 100, 500, 100, 100 → POC bin 2 → 105
    #   value area 70% of 900 = 630: start 500; neighbours 100/100 tie → add upper (600);
    #   next neighbours 100 (bin 4) / 100 (bin 1) tie → upper again (700 ≥ 630)
    #   → bins 2..4 → VAL 104, VAH 110
    b = bars([110, 106], [100, 104], volumes=[500, 400])
    vp = volume_profile(b, cfg(volume_profile_bins=5))
    assert vp is not None
    assert vp.volumes == pytest.approx([100, 100, 500, 100, 100])
    assert (vp.poc, vp.val, vp.vah) == (105, 104, 110)


# ───────────────────────── RS, momentum, participation, risk ─────────────────────────


def test_mansfield_rs() -> None:
    # RS = stock/index: 1.0, 1.0, 1.2 with a 2-week SMA → last = (1.2 / 1.1 - 1) x 100 = 9.09
    idx = weeks(3)
    m = mansfield_rs(
        pd.Series([10.0, 10.0, 12.0], index=idx), pd.Series([10.0, 10.0, 10.0], index=idx), 2
    )
    assert m.iloc[-1] == pytest.approx((1.2 / 1.1 - 1) * 100)


def test_percentile_rank() -> None:
    values = {"A": 1.0, "B": 2.0, "C": 2.0, "D": 4.0, "E": None}
    assert percentile_rank(values, "D") == 100 * (3 + 0.5) / 4
    assert percentile_rank(values, "B") == 100 * (1 + 0.5 * 2) / 4
    assert percentile_rank(values, "E") is None


def test_momentum() -> None:
    w = closes_only(100 + np.arange(60.0))
    daily = pd.Series(np.arange(1.0, 201.0))
    m = momentum(w, daily, CFG)
    # 200-day z: last 200 values 1..200 → (200 - 100.5) / sd(1..200) = 99.5 / 57.88 = 1.719
    assert m.dma_z == pytest.approx(99.5 / np.std(np.arange(1, 201), ddof=1))
    # 52w high = 159 x 1.01; close 159 → 159 / 160.59 - 1
    assert m.from_52w_high == pytest.approx(1 / 1.01 - 1)
    assert m.rsi == pytest.approx(100.0)  # only gains


def test_delivery_and_up_down_volume() -> None:
    d = pd.Series([40.0] * 45 + [60.0] * 5)  # 50-day avg 42, last 5 avg 60 → 1.4286
    assert delivery_ratio(d, CFG) == pytest.approx(60 / 42)
    assert delivery_ratio(d.iloc[:10], CFG) is None
    # closes up, up, down (vols 300, 200, 100) → 500 / 100 = 5
    b = closes_only([10.0, 11, 12, 11], volume=0)
    b["volume"] = [0, 300, 200, 100]
    assert up_down_volume_ratio(b, cfg(updown_volume_weeks=3)) == pytest.approx(5.0)


def test_vcp_detected_and_rejected() -> None:
    t = weeks(10)
    # pullbacks 100→80 (20%), 98→88 (10.2%), 97→92 (5.2%); close 96 within 5% of pivot 97
    swings = [
        Swing(1, t[1], 100, "high", None),
        Swing(2, t[2], 80, "low", None),
        Swing(4, t[4], 98, "high", "LH"),
        Swing(5, t[5], 88, "low", "HL"),
        Swing(7, t[7], 97, "high", "LH"),
        Swing(8, t[8], 92, "low", "HL"),
    ]
    w = closes_only([90.0] * 9 + [96.0])
    v = vcp(w, swings, cfg(vcp=CFG.vcp.model_copy(update={"min_contractions": 3})))
    assert v.detected and v.pivot == 97
    assert v.depths == pytest.approx([0.20, 1 - 88 / 98, 1 - 92 / 97])
    far = closes_only([90.0] * 10)  # 90 is > 5% below the pivot
    assert not vcp(far, swings, CFG).detected


def test_risk() -> None:
    assert invalidation(410, 20, 0.5) == 400
    assert reward_risk(600, 435, 400) == pytest.approx(165 / 35)
    assert reward_risk(600, 400, 400) is None


# ───────────────────────── buy zone ─────────────────────────
# CMP 500, FV 600, MoS 27.5% → discount edge 435; baseline 380 → V = [380, 435]; ATR 20.

KW = dict(
    cmp=500.0,
    stage=2,
    baseline=380.0,
    fair_value=600.0,
    top_band=750.0,
    mos=0.275,
    grade="B",
    atr=20.0,
    cfg=CFG,
)


def test_valuation_range() -> None:
    assert valuation_range(baseline=380, fair_value=600, mos=0.275, grade="B") == (380, 435)
    assert valuation_range(baseline=380, fair_value=600, mos=0.15, grade="A") == (510, 600)
    assert valuation_range(baseline=None, fair_value=600, mos=0.275, grade="B") is None


def test_buy_zone_nearest_fresh_demand_zone() -> None:
    # fresh demand zone 410-440 overlaps V → buy zone 410-435
    # invalidation 410 - 0.5 x 20 = 400; entry min(500, 435) = 435
    # R:R to FV (600 - 435) / 35 = 4.714; to top (750 - 435) / 35 = 9.0
    z = buy_zone(supports=[Support("fresh demand zone", 410, 440, True)], **KW)  # type: ignore[arg-type]
    assert (z.status, z.low, z.high, z.invalidation, z.entry) == ("zone", 410, 435, 400, 435)
    assert z.rr_to_fv == pytest.approx(165 / 35) and z.rr_to_top == pytest.approx(9.0)


def test_buy_zone_falls_back_to_support_inside_range() -> None:
    # nearest primary: AVWAP band 460-470 (above V) → no overlap; next support overlapping V is
    # an older demand zone 400-420 → buy zone 400-420
    supports = [Support("AVWAP 52w low", 460, 470, True), Support("demand zone", 400, 420, False)]
    z = buy_zone(supports=supports, **KW)  # type: ignore[arg-type]
    assert (z.status, z.low, z.high, z.basis) == ("zone", 400, 420, ["demand zone"])
    assert any("outside it" in r for r in z.reasons)


def test_no_technical_buy_zone_yet() -> None:
    z = buy_zone(supports=[Support("POC", 340, 350, True)], **KW)  # type: ignore[arg-type]
    assert z.status == "none" and "No technical buy zone yet" in z.reasons


def test_stage_4_suppresses_buy_zone() -> None:
    kw = {**KW, "stage": 4}
    z = buy_zone(supports=[Support("fresh demand zone", 410, 440, True)], **kw)  # type: ignore[arg-type]
    assert z.status == "suppressed" and "Stage 1" in z.reasons[0]


def test_a_grade_uses_discount_edge_to_fair_value() -> None:
    # A grade, MoS 15%, CMP 560: V = [510, 600]; nearest primary support below CMP is the demand
    # zone 505-520 (the AVWAP band 460-470 is lower) → 505-520 ∩ V = 510-520
    kw = {**KW, "grade": "A", "mos": 0.15, "cmp": 560.0}
    supports = [Support("fresh demand zone", 505, 520, True), Support("AVWAP", 460, 470, True)]
    z = buy_zone(supports=supports, **kw)  # type: ignore[arg-type]
    assert (z.low, z.high) == (510, 520)


def test_price_already_below_the_valuation_range() -> None:
    # A grade with CMP 500 < V low 510: no support below CMP can overlap V; say why
    kw = {**KW, "grade": "A", "mos": 0.15}
    z = buy_zone(supports=[Support("fresh demand zone", 480, 495, True)], **kw)  # type: ignore[arg-type]
    assert z.status == "none"
    assert any("already below the valuation range" in r for r in z.reasons)


def test_zone_is_clipped_at_cmp() -> None:
    # CMP 425 sits inside the demand zone 410-440 → zone 410-425 (never above the price)
    kw = {**KW, "cmp": 425.0}
    z = buy_zone(supports=[Support("fresh demand zone", 410, 440, True)], **kw)  # type: ignore[arg-type]
    assert (z.low, z.high, z.entry) == (410, 425, 425)


def test_buy_zone_without_valuation_levels() -> None:
    kw = {**KW, "fair_value": None}
    assert buy_zone(supports=[], **kw).status == "unavailable"  # type: ignore[arg-type]


# ───────────────────────── engine & payload ─────────────────────────


def test_engine_end_to_end_payload_is_chart_ready() -> None:
    daily = synthetic_daily()
    bench = synthetic_daily(seed=5)["close"]
    delivery = pd.Series(np.linspace(40, 60, 60), index=daily.index[-60:])
    levels = ValuationLevels(baseline=80.0, fair_value=150.0, top_band=200.0, mos=0.275, grade="B")
    t = analyze(daily, CFG, benchmark_daily_close=bench, delivery_pct=delivery, levels=levels)
    p = debug_payload(t, "SYNTH")
    json.dumps(p)  # JSON-serialisable (no NaN/inf, no Timestamps)
    assert p["timeframe"] == "weekly" and len(p["bars"]) == len(t.weekly)
    assert p["bars"][0]["time"] == t.weekly.index[0].date().isoformat()
    assert p["stage"]["stage"] in (1, 2, 3, 4)
    assert p["swings"] and p["zones"] and p["avwaps"] and p["volume_profile"]
    assert p["rs"]["mansfield_benchmark"] is not None
    assert p["participation"]["delivery_ratio"] is not None
    assert p["buy_zone"]["status"] in ("zone", "none", "suppressed")
    assert all(isinstance(s["time"], str) for s in p["swings"])
    assert p["sma_30w"][0]["time"] == t.weekly.index[CFG.stage_sma_weeks - 1].date().isoformat()


def test_engine_short_history_is_flagged() -> None:
    t = analyze(synthetic_daily(n_days=120), CFG)
    assert t.stage.stage is None
    assert any("provisional" in r for r in t.reasons)
    json.dumps(debug_payload(t, "SHORT"))
