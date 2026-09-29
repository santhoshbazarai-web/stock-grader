"""Technical engine: runs every SPEC §6 component on weekly bars built from adjusted daily
prices, assembles supports for the buy zone, and renders a JSON payload for chart overlays
(``/api/stocks/{symbol}/technical/debug``). Pure functions.
"""

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.config import TechnicalConfig
from app.technical.avwap import Avwap, avwaps
from app.technical.bars import atr as atr_series
from app.technical.bars import sma, to_weekly
from app.technical.buy_zone import BuyZone, Support, buy_zone, point_support
from app.technical.momentum import Momentum, momentum
from app.technical.participation import Vcp, delivery_ratio, up_down_volume_ratio, vcp
from app.technical.rs import mansfield_rs
from app.technical.stage import StageResult, weinstein_stage
from app.technical.structure import Structure, market_structure
from app.technical.volume_profile import VolumeProfile, volume_profile
from app.technical.zones import DealingRange, Fvg, Zone, dealing_range, fair_value_gaps
from app.technical.zones import supply_demand_zones as find_zones


@dataclass(frozen=True)
class ValuationLevels:
    baseline: float | None
    fair_value: float | None
    top_band: float | None
    mos: float
    grade: str


@dataclass
class TechnicalAnalysis:
    weekly: pd.DataFrame
    atr: pd.Series
    sma: pd.Series
    stage: StageResult
    structure: Structure
    major_structure: Structure
    zones: list[Zone]
    fvgs: list[Fvg]
    dealing: DealingRange | None
    avwaps: list[Avwap]
    profile: VolumeProfile | None
    rs_benchmark: pd.Series | None
    rs_sector: pd.Series | None
    momentum: Momentum
    delivery_ratio: float | None
    up_down_volume: float | None
    vcp: Vcp
    supports: list[Support]
    buy_zone: BuyZone | None
    reasons: list[str] = field(default_factory=list)

    @property
    def cmp(self) -> float:
        return float(self.weekly["close"].iloc[-1])

    @property
    def atr_now(self) -> float | None:
        v = self.atr.iloc[-1] if len(self.atr) else float("nan")
        return None if pd.isna(v) else float(v)

    @property
    def as_of(self) -> pd.Timestamp:
        return pd.Timestamp(self.weekly.index[-1])


def _supports(t: TechnicalAnalysis, cfg: TechnicalConfig) -> list[Support]:
    a = t.atr_now or 0.0
    out: list[Support] = []
    for z in t.zones:
        if z.side != "demand" or z.broken:
            continue
        label = (
            f"{'fresh ' if z.fresh else ''}demand zone ({z.source.replace('_', ' ')}, "
            f"{z.start.date()})"
        )
        out.append(Support(label, z.bottom, z.top, primary=z.fresh))
    for av in t.avwaps:
        out.append(
            point_support(f"AVWAP {av.anchor.replace('_', ' ')}", av.value, a, cfg, primary=True)
        )
    if t.profile:
        out.append(point_support("POC", t.profile.poc, a, cfg, primary=True))
        out.append(point_support("value-area low", t.profile.val, a, cfg, primary=False))
    for s in t.major_structure.lows()[-3:]:
        out.append(
            point_support(f"major swing low ({s.time.date()})", s.price, a, cfg, primary=False)
        )
    return out


def analyze(
    daily: pd.DataFrame,
    cfg: TechnicalConfig,
    *,
    benchmark_daily_close: pd.Series | None = None,
    sector_daily_close: pd.Series | None = None,
    delivery_pct: pd.Series | None = None,
    last_results_date: pd.Timestamp | None = None,
    levels: ValuationLevels | None = None,
) -> TechnicalAnalysis:
    """``daily``: split/bonus-adjusted OHLCV indexed by date."""
    weekly = to_weekly(daily)
    reasons: list[str] = []
    if len(weekly) < cfg.min_weekly_bars:
        reasons.append(
            f"only {len(weekly)} weekly bars (< {cfg.min_weekly_bars}): "
            "technical read is provisional"
        )
    atr_s = atr_series(weekly, cfg.atr_period)
    st = weinstein_stage(weekly, cfg)
    minor = market_structure(weekly, cfg.swing_fractal_n)
    major = market_structure(weekly, cfg.major_swing_fractal_n)
    zones = find_zones(weekly, atr_s, cfg)
    close = float(weekly["close"].iloc[-1]) if len(weekly) else None
    dr = dealing_range(major.swings, cfg, close)
    avs = avwaps(weekly, cfg, major_swings=major.swings, last_results_date=last_results_date)

    def rs_vs(bench: pd.Series | None) -> pd.Series | None:
        if bench is None or bench.empty:
            return None
        b = to_weekly(
            pd.DataFrame(
                {"open": bench, "high": bench, "low": bench, "close": bench, "volume": 0.0}
            )
        )["close"]
        s = mansfield_rs(weekly["close"], b, cfg.rs_sma_weeks)
        return s if not s.empty else None

    t = TechnicalAnalysis(
        weekly=weekly,
        atr=atr_s,
        sma=sma(weekly["close"], cfg.stage_sma_weeks),
        stage=st,
        structure=minor,
        major_structure=major,
        zones=zones,
        fvgs=fair_value_gaps(weekly),
        dealing=dr,
        avwaps=avs,
        profile=volume_profile(weekly, cfg),
        rs_benchmark=rs_vs(benchmark_daily_close),
        rs_sector=rs_vs(sector_daily_close),
        momentum=momentum(weekly, daily["close"], cfg),
        delivery_ratio=delivery_ratio(delivery_pct, cfg) if delivery_pct is not None else None,
        up_down_volume=up_down_volume_ratio(weekly, cfg),
        vcp=vcp(weekly, minor.swings, cfg),
        supports=[],
        buy_zone=None,
        reasons=reasons + st.reasons + minor.reasons + (dr.reasons if dr else []),
    )
    t.supports = _supports(t, cfg)
    if levels is not None and close is not None:
        t.buy_zone = buy_zone(
            cmp=close,
            stage=st.stage,
            baseline=levels.baseline,
            fair_value=levels.fair_value,
            top_band=levels.top_band,
            mos=levels.mos,
            grade=levels.grade,
            supports=t.supports,
            atr=t.atr_now,
            cfg=cfg,
        )
        t.reasons += t.buy_zone.reasons
    return t


# ───────────────────────── JSON for chart overlays ─────────────────────────


def _d(ts: object) -> str | None:
    return None if ts is None else pd.Timestamp(str(ts)).date().isoformat()


def _f(x: float | None) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), 4)


def _line(series: pd.Series | None) -> list[dict[str, Any]]:
    if series is None:
        return []
    return [{"time": _d(i), "value": _f(v)} for i, v in series.dropna().items()]


def debug_payload(t: TechnicalAnalysis, symbol: str) -> dict[str, Any]:
    """Everything the engine found, in lightweight-charts friendly shapes (time = YYYY-MM-DD)."""
    w = t.weekly
    bz = t.buy_zone
    return {
        "symbol": symbol,
        "timeframe": "weekly",
        "as_of": _d(t.as_of) if len(w) else None,
        "cmp": _f(t.cmp) if len(w) else None,
        "bars": [
            {
                "time": _d(i),
                "open": _f(r.open),
                "high": _f(r.high),
                "low": _f(r.low),
                "close": _f(r.close),
                "volume": _f(r.volume),
            }
            for i, r in w.iterrows()
        ],
        "sma_30w": _line(t.sma),
        "atr": _f(t.atr_now),
        "stage": {
            "stage": t.stage.stage,
            "sma_slope": _f(t.stage.sma_slope),
            "price_vs_sma": _f(t.stage.price_vs_sma),
            "breakout_volume_confirmed": t.stage.breakout_volume_confirmed,
            "reasons": t.stage.reasons,
        },
        "trend": t.structure.trend,
        "swings": [
            {"time": _d(s.time), "price": _f(s.price), "kind": s.kind, "label": s.label}
            for s in t.structure.swings
        ],
        "major_swings": [
            {"time": _d(s.time), "price": _f(s.price), "kind": s.kind, "label": s.label}
            for s in t.major_structure.swings
        ],
        "structure_events": [
            {
                "time": _d(e.time),
                "price": _f(e.price),
                "kind": e.kind,
                "direction": e.direction,
                "swing_time": _d(e.swing_time),
            }
            for e in t.structure.events
        ],
        "zones": [
            {
                "side": z.side,
                "source": z.source,
                "bottom": _f(z.bottom),
                "top": _f(z.top),
                "start": _d(z.start),
                "impulse": _d(z.impulse),
                "retests": z.retests,
                "fresh": z.fresh,
                "broken": z.broken,
                "broken_at": _d(z.broken_at),
            }
            for z in t.zones
        ],
        "fvgs": [
            {
                "direction": g.direction,
                "bottom": _f(g.bottom),
                "top": _f(g.top),
                "time": _d(g.time),
                "filled": g.filled,
                "filled_at": _d(g.filled_at),
            }
            for g in t.fvgs
        ],
        "dealing_range": None
        if t.dealing is None
        else {
            "low": _f(t.dealing.low),
            "high": _f(t.dealing.high),
            "low_time": _d(t.dealing.low_time),
            "high_time": _d(t.dealing.high_time),
            "direction": t.dealing.direction,
            "equilibrium": _f(t.dealing.equilibrium),
            "ote_low": _f(t.dealing.ote_low),
            "ote_high": _f(t.dealing.ote_high),
            "in_premium": t.dealing.premium,
        },
        "avwaps": [
            {
                "anchor": a.anchor,
                "anchor_time": _d(a.anchor_time),
                "value": _f(a.value),
                "series": _line(a.series),
            }
            for a in t.avwaps
        ],
        "volume_profile": None
        if t.profile is None
        else {
            "poc": _f(t.profile.poc),
            "vah": _f(t.profile.vah),
            "val": _f(t.profile.val),
            "bins": [
                {"low": _f(lo), "high": _f(hi), "volume": _f(v)}
                for lo, hi, v in zip(
                    t.profile.edges[:-1], t.profile.edges[1:], t.profile.volumes, strict=True
                )
            ],
        },
        "rs": {
            "mansfield_benchmark": _f(t.rs_benchmark.iloc[-1])
            if t.rs_benchmark is not None
            else None,
            "mansfield_sector": _f(t.rs_sector.iloc[-1]) if t.rs_sector is not None else None,
            "series_benchmark": _line(t.rs_benchmark),
        },
        "momentum": {
            "rsi": _f(t.momentum.rsi),
            "dma_z": _f(t.momentum.dma_z),
            "high_52w": _f(t.momentum.high_52w),
            "from_52w_high": _f(t.momentum.from_52w_high),
        },
        "participation": {
            "delivery_ratio": _f(t.delivery_ratio),
            "up_down_volume": _f(t.up_down_volume),
            "vcp": {
                "detected": t.vcp.detected,
                "depths": [_f(d) for d in t.vcp.depths],
                "pivot": _f(t.vcp.pivot),
                "reasons": t.vcp.reasons,
            },
        },
        "supports": [
            {"label": s.label, "low": _f(s.low), "high": _f(s.high), "primary": s.primary}
            for s in t.supports
        ],
        "buy_zone": None
        if bz is None
        else {
            "status": bz.status,
            "low": _f(bz.low),
            "high": _f(bz.high),
            "basis": bz.basis,
            "invalidation": _f(bz.invalidation),
            "entry": _f(bz.entry),
            "rr_to_fv": _f(bz.rr_to_fv),
            "rr_to_top": _f(bz.rr_to_top),
            "valuation_range": None
            if bz.valuation_range is None
            else [_f(bz.valuation_range[0]), _f(bz.valuation_range[1])],
            "reasons": bz.reasons,
        },
        "reasons": t.reasons,
    }
