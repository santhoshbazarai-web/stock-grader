"""Six-pillar scores (SPEC §7.2). Pure functions.

Every sub-metric is scored 0-100 by its piecewise-linear map in ``scoring.yaml`` (``maps``;
``bank_maps`` for banks). A pillar is the equal-weighted mean of its *available* sub-scores and
needs at least ``pillar_min_coverage`` of them; otherwise it is ``None``. Missing sub-metrics are
listed in ``PillarScore.missing`` (data gaps), never scored as 0 or as an average.

    Quality     ROCE 5y avg, ROCE trend (now - trend_years ago), CFO/EBITDA 5y,
                FCF conversion 5y, Piotroski F
                banks: ROA, NIM
    Growth      sales CAGR 5y, EPS CAGR 5y, TTM YoY EPS growth, EPS acceleration
                (latest quarter's YoY growth - the previous quarter's)
    Valuation   (FV - CMP) / FV, reverse-DCF gap (implied - historical growth)
    Health      D/E, interest coverage, net debt/EBITDA, CCC trend (days now - trend_years
                ago), Altman Z''
                banks: GNPA, CAR
    Governance  pledge %, promoter holding change QoQ (pp), MF+FII+DII change QoQ (pp),
                other-income share of PBT, related-party-transaction flag
    Technical   Weinstein stage, RS percentile, structure trend, delivery ratio

Valuation is scored separately (:func:`valuation_pillar`) because it depends on the fair value,
which is computed after the provisional grade (see ``grade.py``).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.config import PiecewiseLinearMap, ScoringConfig
from app.scoring.common import Pillar, map_score


@dataclass(frozen=True)
class SubScore:
    name: str
    value: float | str | bool | None  # the raw metric
    score: float | None
    reason: str


@dataclass(frozen=True)
class PillarScore:
    pillar: Pillar
    score: float | None
    subs: list[SubScore]
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # "reduced": scored partly from proxies standing in for reported metrics (banks: GNPA,
    # CAR not reported) — shown with the pillar, never n/a while a proxy exists
    confidence: Literal["full", "reduced"] = "full"


@dataclass(frozen=True)
class PillarInputs:
    """Raw metrics (fractions unless named ``*_pct`` / ``*_pp`` / ``*_days``). ``None`` =
    unavailable. Trend "prior" values are from ``scoring.trend_years`` years earlier."""

    # quality
    roce_5y_avg: float | None = None
    roce_latest: float | None = None
    roce_prior: float | None = None
    cfo_to_ebitda_5y: float | None = None
    fcf_conversion_5y: float | None = None
    piotroski: float | None = None
    # growth
    sales_cagr_5y: float | None = None
    eps_cagr_5y: float | None = None
    eps_yoy_ttm: float | None = None
    eps_yoy_q_latest: float | None = None
    eps_yoy_q_prev: float | None = None
    # health
    debt_to_equity: float | None = None
    interest_coverage: float | None = None
    net_debt_to_ebitda: float | None = None
    ccc_days: float | None = None
    ccc_days_prior: float | None = None
    altman_z2: float | None = None
    # governance
    pledge_pct: float | None = None
    promoter_change_qoq_pp: float | None = None
    institutional_change_qoq_pp: float | None = None
    other_income_share: float | None = None
    rpt_flagged: bool | None = None
    # technical
    stage: int | None = None
    rs_percentile: float | None = None
    trend: Literal["up", "down", "range"] | None = None
    delivery_ratio: float | None = None
    # banks (percent)
    is_bank: bool = False
    roa_pct: float | None = None
    nim_pct: float | None = None
    gnpa_pct: float | None = None
    car_pct: float | None = None
    # proxies used only in place of a missing reported metric (fundamentals/banking.PROXIES)
    credit_cost_pct: float | None = None  # for GNPA
    equity_to_assets_pct: float | None = None  # for CAR


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _fmt(v: float | str | bool | None) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _mapped(name: str, value: float | None, points: PiecewiseLinearMap) -> SubScore:
    s = map_score(points, value)
    if s is None:
        return SubScore(name, value, None, f"{name} unavailable")
    return SubScore(name, value, s, f"{name} {_fmt(value)} → {s:.0f}")


def _lookup(name: str, value: object, table: Mapping[Any, float], shown: str) -> SubScore:
    if value is None or value not in table:
        return SubScore(name, None, None, f"{name} unavailable")
    s = float(table[value])
    return SubScore(name, shown, s, f"{name} {shown} → {s:.0f}")


def combine(pillar: Pillar, subs: list[SubScore], cfg: ScoringConfig) -> PillarScore:
    have = [s for s in subs if s.score is not None]
    missing = [s.name for s in subs if s.score is None]
    reasons = [s.reason for s in subs]
    coverage = len(have) / len(subs) if subs else 0.0
    if not have or coverage < cfg.pillar_min_coverage:
        reasons.append(
            f"{pillar.value}: only {len(have)} of {len(subs)} sub-metrics available "
            f"(< {cfg.pillar_min_coverage:.0%}): no score"
        )
        return PillarScore(pillar, None, subs, missing, reasons)
    score = sum(s.score for s in have if s.score is not None) / len(have)
    summary = f"{pillar.value} {score:.0f}/100 from {len(have)} of {len(subs)} sub-metrics"
    if missing:
        summary += f" (missing: {', '.join(missing)})"
    reasons.append(summary)
    return PillarScore(pillar, score, subs, missing, reasons)


def quality(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    m, b = cfg.maps, cfg.bank_maps
    if x.is_bank:
        subs = [_mapped("roa_pct", x.roa_pct, b.roa_pct), _mapped("nim_pct", x.nim_pct, b.nim_pct)]
    else:
        subs = [
            _mapped("roce_5y_avg", x.roce_5y_avg, m.roce_5y_avg),
            _mapped("roce_trend", _diff(x.roce_latest, x.roce_prior), m.roce_trend),
            _mapped("cfo_to_ebitda_5y", x.cfo_to_ebitda_5y, m.cfo_to_ebitda_5y),
            _mapped("fcf_conversion_5y", x.fcf_conversion_5y, m.fcf_conversion_5y),
            _mapped("piotroski", x.piotroski, m.piotroski),
        ]
    return combine(Pillar.QUALITY, subs, cfg)


def growth(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    m = cfg.maps
    subs = [
        _mapped("sales_cagr_5y", x.sales_cagr_5y, m.sales_cagr_5y),
        _mapped("eps_cagr_5y", x.eps_cagr_5y, m.eps_cagr_5y),
        _mapped("eps_yoy_last4q", x.eps_yoy_ttm, m.eps_yoy_last4q),
        _mapped(
            "eps_acceleration", _diff(x.eps_yoy_q_latest, x.eps_yoy_q_prev), m.eps_acceleration
        ),
    ]
    return combine(Pillar.GROWTH, subs, cfg)


def health(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    m = cfg.maps
    if x.is_bank:
        return _bank_health(x, cfg)
    else:
        subs = [
            _mapped("debt_to_equity", x.debt_to_equity, m.debt_to_equity),
            _mapped("interest_coverage", x.interest_coverage, m.interest_coverage),
            _mapped("net_debt_ebitda", x.net_debt_to_ebitda, m.net_debt_ebitda),
            _mapped("ccc_trend_days", _diff(x.ccc_days, x.ccc_days_prior), m.ccc_trend_days),
            _mapped("altman_z2", x.altman_z2, m.altman_z2),
        ]
    return combine(Pillar.HEALTH, subs, cfg)


def _bank_health(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    """GNPA and CAR as reported; a missing one is replaced by its proxy (credit cost for GNPA,
    equity / assets for CAR). Then the pillar scores what exists, lists the missing reported
    metrics, and its confidence is reduced instead of the pillar being n/a."""
    b = cfg.bank_maps
    pairs = (
        ("gnpa_pct", x.gnpa_pct, b.gnpa_pct, "credit_cost_pct", x.credit_cost_pct,
         b.credit_cost_pct),
        ("car_pct", x.car_pct, b.car_pct, "equity_to_assets_pct", x.equity_to_assets_pct,
         b.equity_to_assets_pct),
    )  # fmt: skip
    subs: list[SubScore] = []
    replaced: list[str] = []
    for name, value, points, proxy, pvalue, ppoints in pairs:
        sub = _mapped(name, value, points)
        if sub.score is None:
            alt = _mapped(proxy, pvalue, ppoints)
            if alt.score is not None:
                sub = SubScore(alt.name, alt.value, alt.score,
                               f"{alt.reason} (proxy: {name} not reported)")  # fmt: skip
                replaced.append(name)
        subs.append(sub)
    out = combine(Pillar.HEALTH, subs, cfg)
    if not replaced:
        return out
    reasons = [*out.reasons, f"health: {', '.join(replaced)} not reported; scored from proxies "
               "(reduced confidence)"]  # fmt: skip
    return PillarScore(out.pillar, out.score, out.subs, [*replaced, *out.missing], reasons,
                       confidence="reduced")  # fmt: skip


def governance(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    m = cfg.maps
    rpt = None if x.rpt_flagged is None else ("flagged" if x.rpt_flagged else "clean")
    subs = [
        _mapped("pledge_pct", x.pledge_pct, m.pledge_pct),
        _mapped("promoter_change_qoq_pp", x.promoter_change_qoq_pp, m.promoter_change_qoq_pp),
        _mapped(
            "institutional_change_qoq_pp",
            x.institutional_change_qoq_pp,
            m.institutional_change_qoq_pp,
        ),
        _mapped("other_income_share", x.other_income_share, m.other_income_share),
        _lookup("rpt_flag", rpt, m.rpt_score, rpt or ""),
    ]
    return combine(Pillar.GOVERNANCE, subs, cfg)


def technical(x: PillarInputs, cfg: ScoringConfig) -> PillarScore:
    m = cfg.maps
    subs = [
        _lookup("stage", x.stage, m.stage_score, f"Stage {x.stage}"),
        _mapped("rs_percentile", x.rs_percentile, m.rs_percentile),
        _lookup("trend", x.trend, m.trend_score, x.trend or ""),
        _mapped("delivery_ratio", x.delivery_ratio, m.delivery_ratio),
    ]
    return combine(Pillar.TECHNICAL, subs, cfg)


def valuation_pillar(
    *,
    cmp: float,
    fair_value: float | None,
    implied_growth: float | None,
    hist_growth: float | None,
    cfg: ScoringConfig,
    reverse_dcf_applies: bool = True,
) -> PillarScore:
    """Zone position (FV - CMP) / FV and the reverse-DCF gap (implied - historical growth).
    Models with no FCFF DCF (banks, insurers, NAV, SOTP; rule 10) have no reverse DCF: the
    pillar is the zone position alone, and the gap is not a missing input."""
    discount = (fair_value - cmp) / fair_value if fair_value and fair_value > 0 else None
    subs = [_mapped("discount_to_fv", discount, cfg.maps.discount_to_fv)]
    if reverse_dcf_applies:
        subs.append(_mapped("reverse_dcf_gap", _diff(implied_growth, hist_growth),
                            cfg.maps.reverse_dcf_gap))  # fmt: skip
    return combine(Pillar.VALUATION, subs, cfg)


NON_VALUATION: dict[Pillar, Callable[[PillarInputs, ScoringConfig], PillarScore]] = {
    Pillar.QUALITY: quality,
    Pillar.GROWTH: growth,
    Pillar.HEALTH: health,
    Pillar.GOVERNANCE: governance,
    Pillar.TECHNICAL: technical,
}


def non_valuation_pillars(x: PillarInputs, cfg: ScoringConfig) -> dict[Pillar, PillarScore]:
    """The five pillars that do not depend on the fair value."""
    return {p: fn(x, cfg) for p, fn in NON_VALUATION.items()}
