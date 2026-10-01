"""Buy zone (SPEC §6). Pure function.

1. Stage 4 → suppressed ("downtrend — wait for Stage 1 base").
2. Valuation range V = [Baseline, FV x (1 - MoS)]; for A / A+ grades V = [FV x (1 - MoS), FV].
3. Supports below CMP (bands clipped at CMP):
     primary   = fresh demand zones, AVWAPs, the volume-profile POC
     secondary = other technical supports (value-area low, major swing lows, older demand zones)
   Point levels become bands [level - ``buy_zone_point_band_atr`` x ATR, level].
4. Buy zone = V ∩ the *nearest* (highest) primary support.
5. If they don't intersect: V ∩ the nearest support of any kind that overlaps V.
6. Otherwise: "No technical buy zone yet".
Invalidation = the chosen support's low - ``invalidation_atr_buffer`` x ATR (SPEC: below the
demand-zone low - 0.5 ATR). Entry = min(CMP, zone high); R:R to FV and to the top band.
"""

from dataclasses import dataclass, field
from typing import Literal

from app.core.config import TechnicalConfig
from app.technical.risk import invalidation, reward_risk

Status = Literal["zone", "none", "suppressed", "unavailable"]
A_GRADES = frozenset({"A_plus", "A"})


@dataclass(frozen=True)
class Support:
    label: str  # e.g. "demand zone (base)", "AVWAP 52w low", "POC"
    low: float
    high: float
    primary: bool


@dataclass(frozen=True)
class BuyZone:
    status: Status
    low: float | None = None
    high: float | None = None
    basis: list[str] = field(default_factory=list)
    invalidation: float | None = None
    entry: float | None = None
    rr_to_fv: float | None = None
    rr_to_top: float | None = None
    valuation_range: tuple[float, float] | None = None
    reasons: list[str] = field(default_factory=list)


def point_support(
    label: str, level: float, atr: float, cfg: TechnicalConfig, *, primary: bool
) -> Support:
    return Support(label, level - cfg.buy_zone_point_band_atr * atr, level, primary)


def valuation_range(
    *, baseline: float | None, fair_value: float | None, mos: float, grade: str
) -> tuple[float, float] | None:
    if fair_value is None:
        return None
    discount_edge = fair_value * (1 - mos)
    if grade in A_GRADES:
        return discount_edge, fair_value
    if baseline is None:
        return None
    return baseline, discount_edge


def buy_zone(
    *,
    cmp: float,
    stage: int | None,
    baseline: float | None,
    fair_value: float | None,
    top_band: float | None,
    mos: float,
    grade: str,
    supports: list[Support],
    atr: float | None,
    cfg: TechnicalConfig,
) -> BuyZone:
    if stage == 4:
        return BuyZone("suppressed", reasons=["downtrend — wait for Stage 1 base"])
    v = valuation_range(baseline=baseline, fair_value=fair_value, mos=mos, grade=grade)
    if v is None:
        return BuyZone("unavailable", reasons=["valuation levels unavailable"])
    v_lo, v_hi = v
    if v_lo >= v_hi:
        return BuyZone(
            "none",
            valuation_range=v,
            reasons=[f"valuation range empty ({v_lo:,.2f} >= {v_hi:,.2f})"],
        )
    if atr is None or not atr > 0:
        return BuyZone("unavailable", valuation_range=v, reasons=["ATR unavailable"])

    below = [
        Support(s.label, s.low, min(s.high, cmp), s.primary)
        for s in supports
        if s.low < cmp and s.high >= s.low
    ]
    below.sort(key=lambda s: s.high, reverse=True)
    kind = "A-grade: discount edge to FV" if grade in A_GRADES else "baseline to discount edge"
    reasons = [f"valuation range {v_lo:,.2f}-{v_hi:,.2f} ({kind})"]

    def overlap(s: Support) -> tuple[float, float] | None:
        lo, hi = max(s.low, v_lo), min(s.high, v_hi)
        return (lo, hi) if lo <= hi else None

    chosen: tuple[Support, tuple[float, float]] | None = None
    primary = [s for s in below if s.primary]
    if primary and (ov := overlap(primary[0])):
        chosen = (primary[0], ov)
        reasons.append(f"nearest support {primary[0].label} overlaps the valuation range")
    else:
        if primary:
            reasons.append(
                f"nearest support {primary[0].label} "
                f"({primary[0].low:,.2f}-{primary[0].high:,.2f}) is outside it"
            )
        for s in below:
            if ov := overlap(s):
                chosen = (s, ov)
                reasons.append(f"using the nearest support inside it: {s.label}")
                break
    if chosen is None:
        if cmp < v_lo:
            reasons.append(
                f"price {cmp:,.2f} is already below the valuation range: no support beneath it "
                "can fall inside the range"
            )
        reasons.append("No technical buy zone yet")
        return BuyZone("none", valuation_range=v, reasons=reasons)

    support, (lo, hi) = chosen
    stop = invalidation(support.low, atr, cfg.invalidation_atr_buffer)
    entry = min(cmp, hi)
    rr_fv = reward_risk(fair_value, entry, stop)
    rr_top = reward_risk(top_band, entry, stop)
    reasons.append(
        f"invalidation {stop:,.2f} = support low {support.low:,.2f} - "
        f"{cfg.invalidation_atr_buffer:g} x ATR {atr:,.2f}"
    )
    return BuyZone("zone", lo, hi, [support.label], stop, entry, rr_fv, rr_top, v, reasons)


def distance_to_buy_zone(cmp: float, low: float | None, high: float | None) -> float | None:
    """0 inside the zone; above it, the fall needed as a fraction of CMP (> 0); below it, the
    rise back to the zone's low (< 0)."""
    if low is None or high is None or cmp <= 0:
        return None
    if cmp > high:
        return (cmp - high) / cmp
    if cmp < low:
        return (cmp - low) / cmp
    return 0.0
