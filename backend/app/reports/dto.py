"""StockReport DTO (SPEC §8) — the one shape the API returns and ``reports.payload`` stores.

The SPEC's abridged fields come first; everything after ``thesis`` is the detail the report
page (SPEC §9) needs: pillar sub-scores, knock-outs, earned-premium conditions, DCF scenarios,
the technical summary and key fundamentals. Every derived value carries ``reasons``.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Dto(BaseModel):
    model_config = ConfigDict(frozen=True)


class Levels(_Dto):
    baseline: float | None
    fair_value: float | None
    top_band: float | None
    mos_pct: float | None = Field(description="Margin of safety (fraction) for the MoS grade")
    confidence: Literal["high", "medium", "low"] | None
    discount_edge: float | None = Field(
        None, description="FV x (1 - MoS): Discount below, Fair from here"
    )
    fair_upper: float | None = Field(
        None, description="FV x zones.fair_upper_mult: Fair up to here, Premium above"
    )


class BuyZoneDto(_Dto):
    status: Literal["zone", "none", "suppressed", "unavailable"]
    low: float | None
    high: float | None
    basis: list[str]
    reasons: list[str]


class MethodDto(_Dto):
    name: str
    value: float | None
    weight: float = Field(description="Configured weight")
    effective_weight: float = Field(description="Weight after renormalising over available")
    reasons: list[str] = []


class ReverseDcfDto(_Dto):
    implied_growth: float | None
    hist_growth: float | None
    gap: float | None
    reasons: list[str]


class DcfScenarioDto(_Dto):
    scenario: Literal["bear", "base", "bull"]
    value_per_share: float | None
    terminal_share: float | None
    reasons: list[str]


class ValuationDto(_Dto):
    sector: str = Field(description="sectors.yaml entry used")
    model: str
    methods: list[MethodDto]
    reverse_dcf: ReverseDcfDto | None
    dcf: list[DcfScenarioDto]
    wacc: float | None
    cost_of_equity: float | None
    beta: float | None
    market_cap_cr: float | None
    extra_methods: dict[str, float | None] = Field(
        default_factory=dict, description="Reported but not blended (EPV, Graham, RI)"
    )
    dcf_inputs: dict[str, float] | None = Field(
        None, description="Base DCF assumptions used (after overrides): g1, ebit_margin, ..."
    )
    reasons: list[str]


class Scores(_Dto):
    quality: float | None
    growth: float | None
    valuation: float | None
    health: float | None
    governance: float | None
    technical: float | None
    total: float | None


class SubScoreDto(_Dto):
    name: str
    value: float | str | bool | None
    score: float | None
    reason: str


class PillarDto(_Dto):
    pillar: str
    score: float | None
    weight: float
    subs: list[SubScoreDto]
    missing: list[str]
    reasons: list[str]


class ConditionDto(_Dto):
    code: str
    met: bool | None
    reason: str


class EarnedPremiumDto(_Dto):
    score: int
    max_possible: int
    conditions: list[ConditionDto]


class KnockoutsDto(_Dto):
    cap: str | None
    triggered: list[str]
    unknown: list[str]
    reasons: list[str]


class DecisionDto(_Dto):
    action: str | None
    rule: str | None
    overridden_by_stage4: bool
    checklist: list[str]
    reasons: list[str]


class TechnicalDto(_Dto):
    as_of: date
    stage: int | None
    trend: str | None
    rs_percentile: float | None
    mansfield_rs: float | None
    atr: float | None
    rsi: float | None
    from_52w_high: float | None
    delivery_ratio: float | None
    vcp: bool
    reasons: list[str]


class PeerStats(_Dto):
    """Per-stock multiples and quality used by *other* stocks' relative valuation."""

    symbol: str
    sector: str
    pe: float | None
    pb: float | None
    ev_ebitda: float | None
    roce: float | None
    roe: float | None
    eps_growth: float | None


class ShareholdingDto(_Dto):
    """The latest shareholding pattern on file, with where and when it was filed."""

    source: str | None
    period_end: date = Field(description="Quarter the pattern is for")
    filing_date: date | None = Field(description="When it was filed (point in time)")
    promoter_pct: float | None
    promoter_pledge_pct: float | None = Field(description="% of the promoter holding pledged")
    fii_pct: float | None
    dii_pct: float | None
    mf_pct: float | None
    public_pct: float | None
    promoter_change_pp: float | None = Field(description="vs the previous quarter, in pp")
    pledge_prev_pct: float | None
    quarters: int = Field(description="Patterns on file")


class AnalystConsensusDto(_Dto):
    """The vendor's analyst consensus: informational only, never scored."""

    source: str
    as_of: date = Field(description="When the vendor answer was fetched")
    recommendations: int
    mean_rating: float | None = Field(description="1 Strong Buy … 5 Strong Sell")
    ratings: dict[str, int] = Field(description="Analysts per rating")


class StockReport(_Dto):
    symbol: str
    name: str | None
    cmp: float
    as_of: date
    sources: dict[str, str | None]
    levels: Levels
    zone: str | None
    buy_zone: BuyZoneDto | None
    invalidation: float | None
    rr_to_fv: float | None
    rr_to_top: float | None
    valuation: ValuationDto
    scores: Scores
    grade: str | None = Field(description="A_plus | A | B | C | D (config keys)")
    grade_label: str | None = Field(description="Display form, e.g. A+")
    earned_premium: int | None
    action: str | None
    reasons: list[str]
    red_flags: list[str]
    data_gaps: list[str]
    thesis: str | None = None
    shareholding: ShareholdingDto | None = None
    analyst_consensus: AnalystConsensusDto | None = Field(
        default=None, description="Informational only: never part of a score, zone or action"
    )
    reconciliation_issues: list[str] = Field(
        default_factory=list,
        description="Open cross-source differences (SPEC §3.9); they lower the confidence",
    )
    # ── detail ──
    provisional_grade: str | None
    mos_grade: str | None
    pillars: list[PillarDto]
    knockouts: KnockoutsDto
    earned_premium_detail: EarnedPremiumDto
    decision: DecisionDto
    technical: TechnicalDto
    fundamentals: dict[str, float | None]
    overrides: dict[str, object]
    peer_stats: PeerStats
