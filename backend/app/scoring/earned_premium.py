"""Earned-premium score (SPEC §7.4), 0-8: one point per condition met. Pure function.

    1 implied_growth     reverse-DCF implied growth <= 5-year historical growth
    2 eps_acceleration   YoY EPS growth of the latest quarter > the previous quarter's
    3 roce_up            ROCE now > ROCE ``trend_years`` ago
    4 operating_leverage OPM up YoY and sales growth YoY > operating_leverage_min_sales_growth
    5 institutions_up    MF + FII + DII holding up QoQ
    6 promoter_steady    promoter holding flat or up QoQ, and pledge % not up (no new pledge)
    7 rs_leader          RS percentile >= rs_percentile_min
    8 stage2_near_high   Stage 2 and within near_52w_high_pct of the 52-week high

A condition without data is *unknown*: it earns no point and is listed, so the score is a
lower bound (``max_possible`` = score + unknown).
"""

from dataclasses import dataclass, field

from app.core.config import ScoringConfig


@dataclass(frozen=True)
class EarnedPremiumInputs:
    implied_growth: float | None = None
    hist_growth_5y: float | None = None
    eps_yoy_q_latest: float | None = None
    eps_yoy_q_prev: float | None = None
    roce_latest: float | None = None
    roce_prior: float | None = None  # trend_years ago
    opm_latest: float | None = None
    opm_prev_year: float | None = None
    sales_growth_yoy: float | None = None
    institutional_change_qoq_pp: float | None = None
    promoter_change_qoq_pp: float | None = None
    pledge_pct: float | None = None
    pledge_pct_prev_quarter: float | None = None
    rs_percentile: float | None = None
    stage: int | None = None
    from_52w_high: float | None = None  # close / 52w high - 1 (<= 0)


@dataclass(frozen=True)
class Condition:
    code: str
    met: bool | None
    reason: str


@dataclass(frozen=True)
class EarnedPremium:
    score: int
    conditions: list[Condition]
    unknown: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def max_possible(self) -> int:
        return self.score + len(self.unknown)


def _implied_growth(x: EarnedPremiumInputs) -> Condition:
    code, imp, hist = "implied_growth", x.implied_growth, x.hist_growth_5y
    if imp is None or hist is None:
        return Condition(code, None, "implied or historical growth unavailable")
    ok = imp <= hist
    return Condition(
        code, ok, f"implied growth {imp:.1%} {'<=' if ok else '>'} 5y historical {hist:.1%}"
    )


def _eps_acceleration(x: EarnedPremiumInputs) -> Condition:
    code, now, prev = "eps_acceleration", x.eps_yoy_q_latest, x.eps_yoy_q_prev
    if now is None or prev is None:
        return Condition(code, None, "last two quarters' YoY EPS growth unavailable")
    ok = now > prev
    state = "accelerating" if ok else "not accelerating"
    return Condition(code, ok, f"YoY EPS growth {prev:.1%} → {now:.1%} ({state})")


def _roce_up(x: EarnedPremiumInputs, years: int) -> Condition:
    code, now, prior = "roce_up", x.roce_latest, x.roce_prior
    if now is None or prior is None:
        return Condition(code, None, f"ROCE now or {years}y ago unavailable")
    ok = now > prior
    return Condition(
        code, ok, f"ROCE {now:.1%} vs {prior:.1%} {years}y ago ({'up' if ok else 'not up'})"
    )


def _operating_leverage(x: EarnedPremiumInputs, min_growth: float) -> Condition:
    code, opm, opm_prev, sales = (
        "operating_leverage",
        x.opm_latest,
        x.opm_prev_year,
        x.sales_growth_yoy,
    )
    if opm is None or opm_prev is None or sales is None:
        return Condition(code, None, "OPM or sales growth unavailable")
    ok = opm > opm_prev and sales > min_growth
    return Condition(
        code,
        ok,
        f"OPM {opm_prev:.1%} → {opm:.1%}, sales growth {sales:.1%} "
        f"(needs OPM up and sales growth > {min_growth:.0%})",
    )


def _institutions_up(x: EarnedPremiumInputs) -> Condition:
    code, change = "institutions_up", x.institutional_change_qoq_pp
    if change is None:
        return Condition(code, None, "institutional holding change unavailable")
    return Condition(code, change > 0, f"MF+FII+DII holding {change:+.2f} pp QoQ")


def _promoter_steady(x: EarnedPremiumInputs) -> Condition:
    code, change, pledge, prev = (
        "promoter_steady",
        x.promoter_change_qoq_pp,
        x.pledge_pct,
        x.pledge_pct_prev_quarter,
    )
    if change is None or pledge is None or prev is None:
        return Condition(code, None, "promoter holding or pledge unavailable")
    ok = change >= 0 and pledge <= prev
    return Condition(
        code, ok, f"promoter holding {change:+.2f} pp QoQ, pledge {prev:g}% → {pledge:g}%"
    )


def _rs_leader(x: EarnedPremiumInputs, minimum: float) -> Condition:
    code, rs = "rs_leader", x.rs_percentile
    if rs is None:
        return Condition(code, None, "RS percentile unavailable")
    ok = rs >= minimum
    return Condition(code, ok, f"RS percentile {rs:.0f} ({'>=' if ok else '<'} {minimum:g})")


def _stage2_near_high(x: EarnedPremiumInputs, near: float) -> Condition:
    code, stage, dist = "stage2_near_high", x.stage, x.from_52w_high
    if stage is None or (stage == 2 and dist is None):
        return Condition(code, None, "stage or 52-week high unavailable")
    ok = stage == 2 and dist is not None and dist >= -near
    where = f", {dist:.1%} from the 52w high" if dist is not None else ""
    return Condition(
        code, ok, f"Stage {stage}{where} (needs Stage 2 within {near:.0%} of the high)"
    )


def earned_premium(x: EarnedPremiumInputs, cfg: ScoringConfig) -> EarnedPremium:
    ep = cfg.earned_premium
    conditions = [
        _implied_growth(x),
        _eps_acceleration(x),
        _roce_up(x, cfg.trend_years),
        _operating_leverage(x, ep.operating_leverage_min_sales_growth),
        _institutions_up(x),
        _promoter_steady(x),
        _rs_leader(x, ep.rs_percentile_min),
        _stage2_near_high(x, ep.near_52w_high_pct),
    ]
    score = sum(1 for c in conditions if c.met)
    unknown = [c.code for c in conditions if c.met is None]
    marks = {True: "✓", False: "✗", None: "?"}
    reasons = [f"{marks[c.met]} {c.reason}" for c in conditions]
    summary = f"earned premium {score}/{len(conditions)}"
    if unknown:
        summary += f" ({len(unknown)} unknown: up to {score + len(unknown)})"
    reasons.append(summary)
    return EarnedPremium(score, conditions, unknown, reasons)
