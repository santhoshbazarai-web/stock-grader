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
    fair_value_low: float | None = Field(
        None, description="Methods disagree: the lowest used method value (else null)"
    )
    fair_value_high: float | None = Field(
        None, description="Methods disagree: the highest used method value (else null)"
    )
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
    justified_pb_inputs: dict[str, float | None] | None = Field(
        None, description="Banks: ROE, normalised ROE, Ke, terminal g, retention, stage-1 years"
    )
    justified_pb_grid: list[dict[str, float | None]] = Field(
        default_factory=list, description="Banks: value for normalised ROE x g x Ke"
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
    confidence: Literal["full", "reduced"] = Field(
        default="full", description="reduced: scored partly from proxies for missing metrics"
    )


class DurabilityTestDto(_Dto):
    key: str
    label: str
    passed: bool | None = Field(description="None: not enough history (lowers confidence)")
    detail: str


class DurabilityDto(_Dto):
    rating: Literal["strong", "moderate", "weak"] | None = Field(
        description="None when too few tests have data"
    )
    score: float | None = Field(description="Share of tests with data that pass")
    confidence: Literal["high", "medium", "low"] | None
    label: str = Field(description="Always shown with the rating: a proxy, not a moat rating")
    tests: list[DurabilityTestDto]
    reasons: list[str]


class BankMetricDto(_Dto):
    """A bank metric for the latest fiscal year. ``proxy``: derived from the statements, not
    the figure the bank reports (``definition`` says how)."""

    name: str
    value: float | None
    unit: Literal["pct", "inr", "x"]
    proxy: bool
    definition: str | None
    reason: str | None = Field(description="Why the value is missing, or a caveat")


class ConditionDto(_Dto):
    code: str
    met: bool | None
    reason: str


class EarnedPremiumDto(_Dto):
    score: int
    max_possible: int
    out_of: int = Field(8, description="Conditions scored: 8, or 10 for banks / insurers")
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
    name: str | None = None
    source: Literal["report", "vendor"] = Field(
        "report", description="report: our own stored report; vendor: Indian API peerCompanyList"
    )


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
    pledge_source: str | None = Field(
        None, description="Where the pledge % came from (pattern, NSE pledge disclosure, ...)"
    )
    note: str | None = Field(None, description="e.g. no identified promoter")


class AnalystConsensusDto(_Dto):
    """The vendor's analyst consensus: informational only, never scored."""

    source: str
    as_of: date = Field(description="When the vendor answer was fetched")
    recommendations: int
    mean_rating: float | None = Field(description="1 Strong Buy … 5 Strong Sell")
    ratings: dict[str, int] = Field(description="Analysts per rating")


class DataDepthDto(_Dto):
    """How many fiscal years of P&L the report stands on (SPEC §7.3): the header badge."""

    level: Literal["technical_only", "provisional", "full"]
    pl_years: int
    reason: str


class StockReport(_Dto):
    symbol: str
    name: str | None
    cmp: float
    prev_close: float | None = Field(None, description="Previous session's close")
    day_change_pct: float | None = Field(None, description="CMP vs previous close (fraction)")
    sector: str | None = Field(None, description="Sector key / label for the breadcrumb")
    industry: str | None = Field(None, description="NSE basic industry")
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
    grade_confidence: Literal["full", "reduced"] = Field(
        default="full", description="reduced below full data depth: the grade is provisional"
    )
    data_depth: DataDepthDto | None = None
    earned_premium: int | None
    action: str | None
    reasons: list[str]
    red_flags: list[str]
    data_gaps: list[str]
    thesis: str | None = None
    shareholding: ShareholdingDto | None = None
    bank_metrics: list[BankMetricDto] = Field(
        default_factory=list, description="Banks only: reported metrics and labelled proxies"
    )
    durability: DurabilityDto | None = Field(
        default=None,
        description="Proxy from reported data only; not an analyst moat rating",
    )
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
    fundamentals_notes: dict[str, str] = Field(
        default_factory=dict,
        description="Why a metric is null (or how it was computed, e.g. per share across a "
        "structural break)",
    )
    overrides: dict[str, object]
    peer_stats: PeerStats
