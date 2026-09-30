"""Blend (SPEC §5.7): fair value, baseline, top band, margin of safety, zone, confidence.
Pure functions.

    Fair value = sum(w_m x value_m) / sum(w_m) over methods with a value, weights from
                 sectors.yaml. Missing methods are dropped and the remaining weights
                 renormalised, which is always reported; if the methods that are available
                 carry less than ``blend.min_weight_coverage`` of the weight, there is no
                 fair value. NAV / SOTP sectors use their single model value.
    Baseline   = min(bear DCF, EPV, band -1 sigma price) over those available; for asset-heavy
                 sectors, then max(that, ``asset_heavy_book_multiple`` x book value).
    Top band   = max(bull DCF, band +1 sigma price), capped at the band +2 sigma price
                 (``zones.top_band_cap_sigma``).
    MoS        = ``mos_by_grade`` for the provisional grade.
    Zones      Deep Discount  CMP < Baseline
               Discount       Baseline <= CMP < FV(1 - MoS)
               Fair           FV(1 - MoS) <= CMP <= FV x fair_upper_mult
               Premium        FV x fair_upper_mult < CMP <= Top band
               Extreme        CMP > Top band
    Confidence: coefficient of variation (population std / mean) of the method values:
               > low threshold → low; > medium threshold → medium; else high. Fewer than two
               methods → low (dispersion cannot be measured).
"""

import statistics
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.config import SectorConfig, SectorModel, ValuationConfig

Grade = str  # "A_plus" | "A" | "B" | "C" | "D"


class Zone(StrEnum):
    DEEP_DISCOUNT = "deep_discount"
    DISCOUNT = "discount"
    FAIR = "fair"
    PREMIUM = "premium"
    EXTREME_PREMIUM = "extreme_premium"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class MethodLine:
    name: str
    value: float | None
    weight: float  # configured weight
    effective_weight: float  # after renormalising over available methods


@dataclass(frozen=True)
class Valuation:
    cmp: float
    fair_value: float | None
    baseline: float | None
    top_band: float | None
    mos_pct: float
    zone: Zone | None
    confidence: Confidence
    method_cv: float | None
    methods: list[MethodLine]
    reasons: list[str] = field(default_factory=list)


def fair_value(
    method_values: dict[str, float | None],
    sector: SectorConfig,
    config: ValuationConfig,
    *,
    single_model_value: float | None = None,
) -> tuple[float | None, list[MethodLine], list[str]]:
    if sector.model in (SectorModel.NAV, SectorModel.SOTP):
        v = single_model_value
        line = MethodLine(sector.model.value, v, 1.0, 1.0 if v else 0.0)
        reason = f"{sector.model.value} model" if v else f"{sector.model.value} value missing"
        return (v if v and v > 0 else None), [line], [reason]
    weights = {str(k): float(w) for k, w in (sector.weights or {}).items()}
    usable = {k: v for k, v in method_values.items() if k in weights and v is not None and v > 0}
    covered = sum(weights[k] for k in usable)
    lines = [
        MethodLine(k, method_values.get(k), w, (w / covered if k in usable and covered else 0.0))
        for k, w in weights.items()
    ]
    missing = [k for k in weights if k not in usable]
    reasons = []
    if missing:
        reasons.append(
            f"unavailable: {', '.join(missing)}; weights renormalised over the rest "
            f"({covered:.0%} of total)"
        )
    if covered < config.blend.min_weight_coverage or not usable:
        reasons.append(
            f"available methods carry {covered:.0%} of the weight "
            f"(< {config.blend.min_weight_coverage:.0%}): no fair value"
        )
        return None, lines, reasons
    fv = sum(weights[k] * v for k, v in usable.items()) / covered
    return fv, lines, reasons


def classify_zone(
    cmp: float,
    *,
    baseline: float | None,
    fair_value: float,
    mos: float,
    top_band: float | None,
    fair_upper_mult: float,
) -> Zone:
    if baseline is not None and cmp < baseline:
        return Zone.DEEP_DISCOUNT
    if cmp < fair_value * (1 - mos):
        return Zone.DISCOUNT
    if cmp <= fair_value * fair_upper_mult:
        return Zone.FAIR
    if top_band is None or cmp <= top_band:
        return Zone.PREMIUM
    return Zone.EXTREME_PREMIUM


def confidence(values: list[float], config: ValuationConfig) -> tuple[Confidence, float | None]:
    if len(values) < 2:
        return Confidence.LOW, None
    mean = statistics.fmean(values)
    cv = statistics.pstdev(values) / mean if mean > 0 else float("inf")
    if cv > config.confidence.low_if_method_cv_above:
        return Confidence.LOW, cv
    if cv > config.confidence.medium_if_method_cv_above:
        return Confidence.MEDIUM, cv
    return Confidence.HIGH, cv


_LEVELS = (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW)


def lower_confidence(conf: Confidence, steps: int) -> Confidence:
    """``steps`` levels lower, never below low (reconciliation issues, SPEC v0.2 §3.9)."""
    return _LEVELS[min(len(_LEVELS) - 1, _LEVELS.index(conf) + max(0, steps))]


def blend(
    *,
    cmp: float,
    sector: SectorConfig,
    method_values: dict[str, float | None],
    provisional_grade: Grade,
    config: ValuationConfig,
    bear_dcf: float | None = None,
    bull_dcf: float | None = None,
    epv: float | None = None,
    band_prices: dict[int, float] | None = None,
    book_value_ps: float | None = None,
    single_model_value: float | None = None,
) -> Valuation:
    """``band_prices``: the sector's primary band (highest-weighted band method) as prices,
    keyed by sigma level (-2..+2)."""
    reasons: list[str] = []
    fv, lines, fv_reasons = fair_value(
        method_values, sector, config, single_model_value=single_model_value
    )
    reasons += fv_reasons
    mos = float(getattr(config.mos_by_grade, provisional_grade))
    band = band_prices or {}

    floors = {"bear DCF": bear_dcf, "EPV": epv, "band -1 sigma": band.get(-1)}
    avail = {k: v for k, v in floors.items() if v is not None and v > 0}
    baseline = min(avail.values()) if avail else None
    if baseline is not None:
        source = min(avail, key=lambda k: avail[k])
        reasons.append(f"baseline {baseline:,.1f} from {source}")
    else:
        reasons.append("no baseline: bear DCF, EPV and band -1 sigma all unavailable")
    if sector.asset_heavy and book_value_ps and book_value_ps > 0:
        floor = config.blend.asset_heavy_book_multiple * book_value_ps
        if baseline is None or floor > baseline:
            baseline = floor
            reasons.append(f"asset-heavy: baseline raised to {floor:,.1f} (book value floor)")

    tops = {"bull DCF": bull_dcf, "band +1 sigma": band.get(1)}
    top_avail = {k: v for k, v in tops.items() if v is not None and v > 0}
    top = max(top_avail.values()) if top_avail else None
    cap = band.get(int(config.zones.top_band_cap_sigma))
    if top is not None and cap is not None and top > cap:
        reasons.append(f"top band capped at band +{config.zones.top_band_cap_sigma:g} sigma")
        top = cap

    zone = None
    if fv is not None:
        zone = classify_zone(
            cmp,
            baseline=baseline,
            fair_value=fv,
            mos=mos,
            top_band=top,
            fair_upper_mult=config.zones.fair_upper_mult,
        )
        reasons.append(
            f"CMP {cmp:,.1f} vs fair value {fv:,.1f} (MoS {mos:.1%} for grade "
            f"{provisional_grade}): {zone.value.replace('_', ' ')}"
        )
        if baseline is not None and baseline > fv * (1 - mos):
            reasons.append("baseline above the discount threshold: discount zone is empty")
    used = [ln.value for ln in lines if ln.effective_weight > 0 and ln.value is not None]
    conf, cv = confidence(used, config)
    if cv is None:
        reasons.append("confidence low: fewer than two valuation methods")
    else:
        reasons.append(f"method dispersion CV {cv:.0%}: {conf.value} confidence")
    return Valuation(cmp, fv, baseline, top, mos, zone, conf, cv, lines, reasons)
