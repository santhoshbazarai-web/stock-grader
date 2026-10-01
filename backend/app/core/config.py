"""Typed, validated view of ``config/*.yaml``.

Every tunable threshold, weight and formula parameter lives in YAML. These models only
describe the *shape* and *internal consistency* of that YAML (types, ranges, ordering,
weights summing correctly); they carry no defaults for model parameters so a missing key
fails at startup instead of silently falling back.

Load with :func:`load_config` (explicit directory) or :func:`get_config` (cached, uses
``Settings.config_dir``). Any problem raises :class:`ConfigError` listing every issue.
"""

import math
import re
from datetime import time
from enum import StrEnum
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    RootModel,
    ValidationError,
    model_validator,
)

from app.core.settings import get_settings
from app.db.enums import Broker, EventKind

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Score = Annotated[float, Field(ge=0.0, le=100.0)]
GradeKey = Literal["A_plus", "A", "B", "C", "D"]
ZoneKey = Literal["deep_discount", "discount", "fair", "premium", "extreme_premium"]
_SUM_TOLERANCE = 1e-6


class ConfigError(RuntimeError):
    """Raised when a config file is missing, unparsable or fails validation."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ───────────────────────── shared ─────────────────────────


class PiecewiseLinearMap(RootModel[list[tuple[float, float]]]):
    """``[[metric_value, score], ...]``; linear between points, clamped at the ends.

    Metric values must be strictly monotonic (increasing *or* decreasing, e.g. lower
    debt/equity scores higher) and scores must lie in 0..100.
    """

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _check(self) -> Self:
        points = self.root
        if len(points) < 2:
            raise ValueError("a piecewise-linear map needs at least 2 points")
        xs = [x for x, _ in points]
        diffs = [b - a for a, b in pairwise(xs)]
        if not (all(d > 0 for d in diffs) or all(d < 0 for d in diffs)):
            raise ValueError(f"metric values must be strictly monotonic, got {xs}")
        for _, score in points:
            if not 0.0 <= score <= 100.0:
                raise ValueError(f"scores must be within 0..100, got {score}")
        return self


# ───────────────────────── providers.yaml ─────────────────────────


class Provider(StrEnum):
    FYERS = "fyers"
    KITE = "kite"
    YFINANCE = "yfinance"
    NSE = "nse"
    SCREENER = "screener"
    BSE = "bse"
    MARKET_LENS = "market_lens"  # NSE Market Lens (beta): reconciliation only, off by default
    OFFLINE = "offline"  # development only: the synthetic offline exchange (app.devtools)


class Dataset(StrEnum):
    DAILY_OHLCV = "daily_ohlcv"
    INDEX_OHLCV = "index_ohlcv"
    LTP = "ltp"
    DELIVERY = "delivery"
    CORPORATE_ACTIONS = "corporate_actions"
    SHAREHOLDING = "shareholding"
    FIN_ANNUAL = "fin_annual"
    FIN_QUARTERLY = "fin_quarterly"
    INDEX_CONSTITUENTS = "index_constituents"
    SURVEILLANCE = "surveillance"
    RESULTS_FILINGS = "results_filings"  # exchange results filings: index + XBRL documents
    ANNUAL_REPORTS = "annual_reports"  # annual-report list + PDF documents (gap filler)
    SYMBOL_MASTER = "symbol_master"  # NSE / BSE / Fyers masters + NSE symbol and name changes
    EVENTS = "events"  # exchange feeds: announcements, results, board meetings, pledge, deals ...
    REFERENCE_FINANCIALS = "reference_financials"  # other sources the reconciliation checks
    INDUSTRY = "industry"  # per-symbol industry classification → sector model (industries.yaml)


class RateLimit(_Strict):
    per_sec: PositiveFloat
    per_min: PositiveFloat

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.per_min < self.per_sec:
            raise ValueError("per_min must be >= per_sec")
        return self


class RetryConfig(_Strict):
    max_attempts: PositiveInt
    backoff_base_s: NonNegativeFloat
    backoff_max_s: NonNegativeFloat
    rate_limit_timeout_s: NonNegativeFloat

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.backoff_base_s > self.backoff_max_s:
            raise ValueError("backoff_base_s must be <= backoff_max_s")
        return self


class ApiLimits(_Strict):
    history_max_days: PositiveInt
    quotes_max_symbols: PositiveInt


class DayRange(_Strict):
    """Inclusive range of period lengths in days (how a context span is classified)."""

    min: PositiveInt
    max: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self

    def contains(self, days: int) -> bool:
        return self.min <= days <= self.max


class NseResultsConfig(_Strict):
    """Financial results filings (XBRL) — the replacement for manual Screener uploads."""

    index_path: str  # JSON list of a symbol's results filings, relative to base_url
    periods: list[str] = Field(min_length=1)  # the index's `period` values to request
    xbrl_hosts: list[str] = Field(min_length=1)  # documents are fetched only from these hosts
    max_xbrl_bytes: PositiveInt
    quarter_days: DayRange  # a duration context this long is a quarter
    year_days: DayRange  # ... and this long a fiscal year
    # Filings disseminated at or after this IST time count as known from the next day (rule 4).
    available_after_ist: time
    # Rounding levels a filing may state (LevelOfRoundingUsedInFinancialStatements), keyword →
    # rupees per unit, used when a filer keyed amounts in that unit instead of rupees.
    rounding_levels: dict[str, PositiveFloat]
    # PAT / diluted EPS below this many shares means the amounts cannot be in rupees.
    min_plausible_shares: PositiveFloat
    # A later filing's figure for a period counts as a restatement (a new version) only if it
    # differs by more than both of these (filings round to lakhs or crores differently).
    restatement_tolerance_rel: Fraction
    restatement_tolerance_inr: Annotated[float, Field(ge=0)]
    # Fiscal-year end month used to sum four quarters into a year when a company has no filed
    # annual figures yet to show its own year end (SPEC §3.6 step 5).
    default_fy_end_month: Annotated[int, Field(ge=1, le=12)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.quarter_days.max >= self.year_days.min:
            raise ValueError("quarter_days must end before year_days starts")
        return self


class PdfExtractionConfig(_Strict):
    """Reading statement tables from annual-report PDFs (SPEC §3.6 step 3). Lengths in PDF
    points (1/72 inch)."""

    max_pages: PositiveInt  # a larger PDF is refused
    heading_lines: PositiveInt  # a statement title must be within a page's first N text lines
    line_tolerance_pt: PositiveFloat  # words whose tops differ by less are on one line
    column_gap_pt: PositiveFloat  # amounts whose right edges are this far apart: other columns
    min_column_rows: PositiveInt  # a value column needs amounts on at least this many rows
    min_label_score: Fraction  # fuzzy label similarity (0-1) below this: the row is not mapped
    continuation_min_rows: PositiveInt  # mapped rows the next page needs to continue a statement
    camelot_min_rows: PositiveInt  # pdfplumber mapped fewer rows on a page → try camelot


class PdfConfidenceConfig(_Strict):
    """Confidence of a PDF-derived value: label similarity x label weight x the factors below
    (each applies when its condition holds). At or above ``auto_accept`` the value is stored
    directly; below it waits in the review queue."""

    auto_accept: Fraction
    camelot_factor: Fraction  # read by the camelot fallback
    ambiguous_factor: Fraction  # another item's label matched within ambiguity_margin
    ambiguity_margin: Fraction
    no_header_dates_factor: Fraction  # column dates not printed: taken from the report's year
    check_failed_factor: Fraction  # the column fails its cross-check (see pdf_labels.yaml)
    no_check_factor: Fraction  # the column has no cross-check rows
    check_tolerance_rel: Fraction  # cross-check passes within this relative difference
    fallback_sum_factor: Fraction  # item added up from other items (fallback_sum)


class NseAnnualReportsConfig(_Strict):
    """Annual reports (PDF, or a ZIP holding it) as the balance-sheet / cash-flow gap filler."""

    index_path: str  # JSON list of a symbol's annual reports, relative to base_url
    hosts: list[str] = Field(min_length=1)  # documents are fetched only from these hosts
    max_bytes: PositiveInt  # download size cap, and the cap on a PDF unpacked from a ZIP
    extraction: PdfExtractionConfig
    confidence: PdfConfidenceConfig


class NseSymbolFilesConfig(_Strict):
    """Symbol-master files on ``archives_url`` (SPEC §3.5)."""

    equity_list_path: str  # EQUITY_L.csv
    symbol_changes_path: str
    name_changes_path: str


class NseEventsConfig(_Strict):
    """Market-wide event feeds (SPEC v0.2 §3.8, §10 ``events``). The JSON paths are on
    ``base_url`` and take ``from_date`` / ``to_date``; the bulk and block deal files are on
    ``archives_url`` and hold the latest trading day only."""

    announcements_path: str
    board_meetings_path: str
    results_path: str  # the market-wide results filing list (the per-symbol one is `results`)
    results_periods: list[str] = Field(min_length=1)
    pledge_path: str
    sast_path: str  # SAST regulation 29 disclosures
    pit_path: str  # insider-trading (PIT) disclosures
    bulk_deals_path: str
    block_deals_path: str
    max_days_per_request: PositiveInt  # a longer window is read in pieces


SessionMethod = Literal["curl_cffi", "playwright", "requests"]


class BrowserSessionConfig(_Strict):
    """How a site's browser-like session warms up (``data/providers/web_session.py``)."""

    warmup_urls: list[str] = Field(min_length=1)  # visited in order before API calls
    api_referer: str  # the page an API call is "made from" (Referer; Origin if cross-site)
    impersonate: str = Field(min_length=1)  # curl_cffi browser profile ("chrome" = newest)
    remember_ttl_s: PositiveFloat  # the method that worked is tried first for this long
    blocked_ttl_s: PositiveFloat  # a refused method is skipped for this long
    playwright_timeout_s: PositiveFloat  # page load / network-idle wait
    playwright_block_resources: list[str]  # resource types not loaded (images, fonts, media)


def _session_methods(v: list[SessionMethod]) -> list[SessionMethod]:
    if len(set(v)) != len(v):
        raise ValueError("session methods must not repeat")
    return v


SessionMethods = Annotated[list[SessionMethod], Field(min_length=1),
                           AfterValidator(_session_methods)]  # fmt: skip


class NseConfig(_Strict):
    base_url: str
    quote_path: str  # per-symbol quote API (industryInfo: the four-level classification)
    session: SessionMethods  # tried in order; see BrowserSessionConfig
    browser: BrowserSessionConfig
    archives_url: str
    niftyindices_url: str
    cookie_ttl_s: PositiveFloat
    request_timeout_s: PositiveFloat
    corporate_actions_from_years: PositiveInt
    index_constituent_files: dict[str, str]
    results: NseResultsConfig
    annual_reports: NseAnnualReportsConfig
    symbol_files: NseSymbolFilesConfig
    events: NseEventsConfig


class BseConfig(_Strict):
    """BSE public data (browser-like headers; ``referer`` is sent with every request)."""

    api_url: str
    referer: str
    session: SessionMethods  # tried in order; see BrowserSessionConfig
    browser: BrowserSessionConfig
    cookie_ttl_s: PositiveFloat  # re-visit the warm-up pages for fresh cookies after this
    request_timeout_s: PositiveFloat
    scrip_master_path: str  # active equity scrips, relative to api_url
    announcements_path: str  # corporate announcements (all companies), relative to api_url
    announcements_max_pages: PositiveInt  # pages read per window (newest first)
    results_categories: list[str]  # announcement categories that are results filings
    attachment_url: str  # announcement PDFs: attachment_url + ATTACHMENTNAME


class BhavcopyHistoryConfig(_Strict):
    """Daily OHLCV built from NSE's ``sec_bhavdata_full`` archive files (SPEC §3.2: the price
    fallback after the brokers). Every file read is stored for all symbols, so one download
    serves every stock (and the day's delivery %)."""

    series: list[str] = Field(min_length=1)  # equity series kept (EQ, BE)
    on_demand_max_days: PositiveInt  # a price request may fetch this many missing days itself
    backfill_days_per_run: PositiveInt  # bhavcopy_history job: files per run (1 req/s)
    holiday_after_days: Annotated[int, Field(ge=0)]  # a 404 this many days old = no trading


class MarketLensConfig(_Strict):
    """NSE Market Lens (beta): the JSON its page loads, read only by the reconciliation and only
    when ``enabled`` (SPEC v0.2 §0, §3.2a). The shape is undocumented: every field name used is
    here, so a change needs no code."""

    enabled: bool
    base_url: str
    financials_path: str  # relative to base_url; ``{symbol}`` is replaced
    request_timeout_s: PositiveFloat
    records_key: str | None  # the list of periods inside the JSON (None: the JSON is the list)
    period_end_field: str
    period_type_field: str | None  # "quarter" / "year" words in it; None: all are years
    basis_field: str | None  # "consolidated" / "standalone" words in it; None: consolidated
    amount_unit_inr: PositiveFloat  # rupees per unit of the amounts (1e7: ₹ crore)
    field_map: dict[str, str] = Field(min_length=1)  # item_code → JSON field


class SearchConfig(_Strict):
    """Symbol search (SPEC §3.5): pg_trgm similarity of the query to symbols, names and
    aliases. A match scores max(similarity, word_similarity) in 0-1."""

    min_similarity: Fraction  # weaker matches are not returned
    candidate_limit: PositiveInt  # matching terms read before grouping per company


class SymbolsConfig(_Strict):
    fyers_masters: list[str]  # Fyers public symbol-master CSVs (no login needed)
    request_timeout_s: PositiveFloat
    search: SearchConfig


class BrokerConfig(_Strict):
    """SPEC §0/§3.3: Fyers is the price broker; Kite is complete but off unless enabled (paid
    data subscription). A disabled broker is never built, logged in to or reminded about."""

    enabled: bool
    morning_reminder: bool  # broker_token_check notifies when its token has expired


class ProvidersConfig(_Strict):
    priority: dict[Dataset, list[Provider]]
    rate_limits: dict[Provider, RateLimit]
    staleness_hours: dict[Dataset, PositiveFloat]
    retry: RetryConfig
    api_limits: dict[Provider, ApiLimits]
    token_daily_expiry_ist: dict[Provider, time]
    instruments_cache_hours: PositiveFloat
    yfinance_index_tickers: dict[str, str]
    nse: NseConfig
    bse: BseConfig
    symbols: SymbolsConfig
    market_lens: MarketLensConfig
    brokers: dict[Broker, BrokerConfig]
    bhavcopy: BhavcopyHistoryConfig
    oauth_state_ttl_s: PositiveInt
    history_years: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if set(self.brokers) != set(Broker):
            raise ValueError(f"brokers must configure exactly {sorted(b.value for b in Broker)}")
        missing = set(Dataset) - set(self.priority)
        if missing:
            raise ValueError(f"priority missing datasets: {sorted(missing)}")
        for dataset, chain in self.priority.items():
            if not chain:
                raise ValueError(f"priority.{dataset} must list at least one provider")
            if len(set(chain)) != len(chain):
                raise ValueError(f"priority.{dataset} lists a provider twice")
        return self


# ───────────────────────── valuation.yaml ─────────────────────────


class SizePremiumTier(_Strict):
    max_mcap: PositiveFloat | None  # ₹ Cr; null = open-ended top tier
    premium: Fraction


class BetaConfig(_Strict):
    lookback_years: PositiveInt
    frequency: Literal["daily", "weekly", "monthly"]
    benchmark: str
    blume_adjust: bool
    floor: PositiveFloat
    cap: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.floor >= self.cap:
            raise ValueError("beta floor must be < cap")
        return self


class SensitivityConfig(_Strict):
    wacc_step: PositiveFloat
    wacc_range: PositiveFloat
    tg_values: list[Fraction] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.wacc_step > self.wacc_range:
            raise ValueError("wacc_step must be <= wacc_range")
        return self


class ScenarioDelta(_Strict):
    g1_delta: float
    margin_delta: float
    wacc_delta: float


class Scenarios(_Strict):
    bear: ScenarioDelta
    base: ScenarioDelta
    bull: ScenarioDelta


class DcfConfig(_Strict):
    stage1_years: PositiveInt
    stage2_years: PositiveInt
    terminal_growth: Fraction
    terminal_growth_bounds: tuple[Fraction, Fraction]
    g1_cap_by_default: Fraction
    margin_years: PositiveInt
    reverse_growth_bracket: tuple[float, float]
    sensitivity: SensitivityConfig
    scenarios: Scenarios

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = self.terminal_growth_bounds
        if lo > hi:
            raise ValueError("terminal_growth_bounds must be [low, high]")
        if not lo <= self.terminal_growth <= hi:
            raise ValueError("terminal_growth must lie within terminal_growth_bounds")
        if self.reverse_growth_bracket[0] >= self.reverse_growth_bracket[1]:
            raise ValueError("reverse_growth_bracket must be [low, high]")
        return self


class AnnouncementLag(_Strict):
    quarterly: PositiveInt
    annual: PositiveInt


class BandsConfig(_Strict):
    lookback_years: list[PositiveInt] = Field(min_length=1)
    multiples: list[Literal["pe", "ev_ebitda", "pb"]] = Field(min_length=1)
    min_observations: PositiveInt
    assumed_announcement_lag_days: AnnouncementLag


class EpvConfig(_Strict):
    normalise_years: PositiveInt


class BlendConfig(_Strict):
    min_weight_coverage: Fraction
    asset_heavy_book_multiple: NonNegativeFloat


class RelativeConfig(_Strict):
    roce_exponent: NonNegativeFloat
    growth_exponent: NonNegativeFloat
    min_peers: PositiveInt


class GradeFractions(_Strict):
    """One value per grade; keys mirror ``scoring.grade_cutoffs`` plus D."""

    A_plus: Fraction
    A: Fraction
    B: Fraction
    C: Fraction
    D: Fraction


class ZonesConfig(_Strict):
    fair_upper_mult: Annotated[float, Field(gt=1.0)]
    top_band_cap_sigma: PositiveFloat


class ConfidenceConfig(_Strict):
    low_if_method_cv_above: PositiveFloat
    medium_if_method_cv_above: PositiveFloat
    # Open reconciliation issues (SPEC v0.2 §3.9) lower the confidence this many levels.
    reconciliation_steps_down: Annotated[int, Field(ge=0, le=2)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.medium_if_method_cv_above >= self.low_if_method_cv_above:
            raise ValueError("medium_if_method_cv_above must be < low_if_method_cv_above")
        return self


class ValuationConfig(_Strict):
    risk_free_rate: Fraction
    equity_risk_premium: Fraction
    size_premium: list[SizePremiumTier] = Field(min_length=1)
    beta: BetaConfig
    tax_rate_default: Fraction
    dcf: DcfConfig
    bands: BandsConfig
    epv: EpvConfig
    graham_multiplier: PositiveFloat
    blend: BlendConfig
    relative: RelativeConfig
    mos_by_grade: GradeFractions
    zones: ZonesConfig
    confidence: ConfidenceConfig

    @model_validator(mode="after")
    def _check(self) -> Self:
        tiers = self.size_premium
        if tiers[-1].max_mcap is not None:
            raise ValueError("last size_premium tier must have max_mcap: null")
        bounded = [t.max_mcap for t in tiers[:-1]]
        if any(m is None for m in bounded):
            raise ValueError("only the last size_premium tier may have max_mcap: null")
        caps = [m for m in bounded if m is not None]
        if caps != sorted(set(caps)):
            raise ValueError("size_premium max_mcap must be strictly increasing")
        return self


# ───────────────────────── sectors.yaml ─────────────────────────


class SectorModel(StrEnum):
    FCFF = "fcff"
    BANK = "bank"
    INSURANCE = "insurance"
    CYCLICAL = "cyclical"
    NAV = "nav"
    SOTP = "sotp"


class ValuationMethod(StrEnum):
    DCF_BASE = "dcf_base"
    BAND_PE = "band_pe"
    BAND_EV_EBITDA = "band_ev_ebitda"
    BAND_PB = "band_pb"
    BAND_P_EV = "band_p_ev"
    RELATIVE = "relative"
    RELATIVE_PB = "relative_pb"
    RELATIVE_P_EV = "relative_p_ev"
    JUSTIFIED_PB = "justified_pb"
    NORMALISED_EV_EBITDA = "normalised_ev_ebitda"


# AGENTS.md rule 10: banks/NBFCs/insurance never use FCFF DCF.
_NO_DCF_MODELS = frozenset({SectorModel.BANK, SectorModel.INSURANCE})


class SectorConfig(_Strict):
    model: SectorModel
    weights: dict[ValuationMethod, Fraction] | None = None
    long_run_growth: Fraction | None = None
    normalise_years: PositiveInt | None = None
    asset_heavy: bool = False
    manual_inputs: list[str] | None = None
    nav_discount: Fraction | None = None
    holding_discount: Fraction | None = None
    g1_cap: Fraction | None = None  # overrides valuation.dcf.g1_cap_by_default

    @model_validator(mode="after")
    def _check(self) -> Self:
        m = self.model
        if m in {SectorModel.NAV, SectorModel.SOTP}:
            if self.weights is not None:
                raise ValueError(f"model {m} is a single-method model and takes no weights")
        else:
            if not self.weights:
                raise ValueError(f"model {m} requires weights")
            total = sum(self.weights.values())
            if not math.isclose(total, 1.0, abs_tol=_SUM_TOLERANCE):
                raise ValueError(f"weights must sum to 1.0, got {total:.6f}")
        if m in _NO_DCF_MODELS and self.weights and ValuationMethod.DCF_BASE in self.weights:
            raise ValueError(f"model {m} must not use FCFF DCF (dcf_base)")
        if m is SectorModel.BANK and self.long_run_growth is None:
            raise ValueError("bank model requires long_run_growth (justified P/B)")
        if m is SectorModel.CYCLICAL and self.normalise_years is None:
            raise ValueError("cyclical model requires normalise_years")
        if m is SectorModel.NAV and self.nav_discount is None:
            raise ValueError("nav model requires nav_discount")
        if m is SectorModel.SOTP and self.holding_discount is None:
            raise ValueError("sotp model requires holding_discount")
        return self


class SectorsConfig(RootModel[dict[str, SectorConfig]]):
    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if "default" not in self.root:
            raise ValueError("sectors must define a 'default' entry")
        return self

    def for_sector(self, sector: str | None) -> SectorConfig:
        """Sector entry, or ``default`` when the sector has no specific entry."""
        if sector is not None and sector in self.root:
            return self.root[sector]
        return self.root["default"]


# ───────────────────────── scoring.yaml ─────────────────────────


class PillarWeights(_Strict):
    quality: NonNegativeFloat
    growth: NonNegativeFloat
    valuation: NonNegativeFloat
    health: NonNegativeFloat
    governance: NonNegativeFloat
    technical: NonNegativeFloat

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = sum(self.model_dump().values())
        if not math.isclose(total, 100.0, abs_tol=_SUM_TOLERANCE):
            raise ValueError(f"pillar weights must sum to 100, got {total}")
        return self


class GradeCutoffs(_Strict):
    A_plus: Score
    A: Score
    B: Score
    C: Score

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.A_plus > self.A > self.B > self.C:
            raise ValueError("grade cutoffs must be strictly descending: A_plus > A > B > C")
        return self


class KnockoutsConfig(_Strict):
    max_pledge_pct: Annotated[float, Field(ge=0.0, le=100.0)]
    negative_cfo_years_in_5: PositiveInt
    negative_cfo_window_years: PositiveInt
    min_mcap_cr: NonNegativeFloat
    min_avg_traded_value_cr_20d: NonNegativeFloat
    traded_value_days: PositiveInt
    beneish_m_max: float
    auditor_resignation_years: PositiveInt
    on_asm_gsm: bool
    cap_grade: GradeKey

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.negative_cfo_years_in_5 > self.negative_cfo_window_years:
            raise ValueError("negative_cfo_years_in_5 must be <= negative_cfo_window_years")
        return self


class ScoringMaps(_Strict):
    roce_5y_avg: PiecewiseLinearMap
    cfo_to_ebitda_5y: PiecewiseLinearMap
    fcf_conversion_5y: PiecewiseLinearMap
    piotroski: PiecewiseLinearMap
    sales_cagr_5y: PiecewiseLinearMap
    eps_cagr_5y: PiecewiseLinearMap
    eps_yoy_last4q: PiecewiseLinearMap
    discount_to_fv: PiecewiseLinearMap
    reverse_dcf_gap: PiecewiseLinearMap
    debt_to_equity: PiecewiseLinearMap
    interest_coverage: PiecewiseLinearMap
    net_debt_ebitda: PiecewiseLinearMap
    pledge_pct: PiecewiseLinearMap
    rs_percentile: PiecewiseLinearMap
    roce_trend: PiecewiseLinearMap
    eps_acceleration: PiecewiseLinearMap
    ccc_trend_days: PiecewiseLinearMap
    altman_z2: PiecewiseLinearMap
    promoter_change_qoq_pp: PiecewiseLinearMap
    institutional_change_qoq_pp: PiecewiseLinearMap
    other_income_share: PiecewiseLinearMap
    delivery_ratio: PiecewiseLinearMap
    stage_score: dict[Literal[1, 2, 3, 4], Score]
    trend_score: dict[Literal["up", "range", "down"], Score]
    rpt_score: dict[Literal["clean", "flagged"], Score]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if set(self.stage_score) != {1, 2, 3, 4}:
            raise ValueError("stage_score must define stages 1, 2, 3 and 4")
        if set(self.trend_score) != {"up", "range", "down"}:
            raise ValueError("trend_score must define up, range and down")
        if set(self.rpt_score) != {"clean", "flagged"}:
            raise ValueError("rpt_score must define clean and flagged")
        return self


class EarnedPremiumConfig(_Strict):
    momentum_entry_min: Annotated[int, Field(ge=0, le=8)]
    rs_percentile_min: Score
    near_52w_high_pct: Fraction
    operating_leverage_min_sales_growth: float


class DecisionRule(StrEnum):
    """Decision-matrix cell rules (SPEC §7.5); semantics in ``scoring/decision.py``."""

    STRONG_BUY = "strong_buy"
    BUY_OR_ACCUMULATE = "buy_or_accumulate"
    BUY_ON_PULLBACK = "buy_on_pullback"
    MOMENTUM_OR_WAIT = "momentum_or_wait"
    HOLD_IF_OWNED = "hold_if_owned"
    BUY_WITH_CONFIRMATION = "buy_with_confirmation"
    ACCUMULATE_SLOWLY = "accumulate_slowly"
    VALUE_TRAP_CHECK = "value_trap_check"
    WATCH = "watch"
    WAIT = "wait"
    AVOID = "avoid"
    BOOK_PROFITS = "book_profits"


class ConfirmationConfig(_Strict):
    stages: list[Literal[1, 2, 3, 4]]
    trends: list[Literal["up", "range", "down"]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.stages and not self.trends:
            raise ValueError("confirmation needs at least one stage or trend")
        return self


class ChecklistsConfig(_Strict):
    why_cheap: list[str] = Field(min_length=1)
    value_trap: list[str] = Field(min_length=1)


class DecisionConfig(_Strict):
    matrix: dict[GradeKey, dict[ZoneKey, DecisionRule]]
    confirmation: ConfirmationConfig
    checklists: ChecklistsConfig

    @model_validator(mode="after")
    def _check(self) -> Self:
        grades, zones = set(get_args(GradeKey)), set(get_args(ZoneKey))
        if set(self.matrix) != grades:
            raise ValueError(f"decision matrix must have a row for every grade {sorted(grades)}")
        for grade, row in self.matrix.items():
            if set(row) != zones:
                raise ValueError(
                    f"decision matrix row {grade} must have a cell for every zone {sorted(zones)}"
                )
        return self


class BankMaps(_Strict):
    gnpa_pct: PiecewiseLinearMap
    nim_pct: PiecewiseLinearMap
    car_pct: PiecewiseLinearMap
    roa_pct: PiecewiseLinearMap


class FundamentalsConfig(_Strict):
    cagr_years: list[PositiveInt] = Field(min_length=1)
    cumulative_years: PositiveInt
    dilution_years: PositiveInt
    days_in_year: Annotated[int, Field(ge=360, le=366)]


class ForensicConfig(_Strict):
    beneish_flag_above: float
    altman_safe_above: float
    altman_distress_below: float

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.altman_distress_below >= self.altman_safe_above:
            raise ValueError("altman_distress_below must be < altman_safe_above")
        return self


class ScoringConfig(_Strict):
    weights: PillarWeights
    grade_cutoffs: GradeCutoffs
    knockouts: KnockoutsConfig
    maps: ScoringMaps
    pillar_min_coverage: Fraction
    total_min_weight_coverage: Fraction
    trend_years: PositiveInt
    earned_premium: EarnedPremiumConfig
    decision: DecisionConfig
    bank_maps: BankMaps
    fundamentals: FundamentalsConfig
    forensic: ForensicConfig


# ───────────────────────── technical.yaml ─────────────────────────


class VcpConfig(_Strict):
    lookback_weeks: PositiveInt
    min_contractions: Annotated[int, Field(ge=2)]
    max_final_depth: Fraction
    max_distance_from_pivot: Fraction


class TechnicalConfig(_Strict):
    swing_fractal_n: PositiveInt
    zone_impulse_atr_mult: PositiveFloat
    zone_max_base_candles: PositiveInt
    zone_max_retests_fresh: Annotated[int, Field(ge=0)]
    stage_sma_weeks: PositiveInt
    atr_period: PositiveInt
    rsi_period: PositiveInt
    invalidation_atr_buffer: NonNegativeFloat
    volume_profile_lookback_weeks: PositiveInt
    avwap_anchors: list[Literal["low_52w", "last_results_date", "last_major_swing_low"]] = Field(
        min_length=1
    )
    stage_slope_weeks: PositiveInt
    stage_flat_slope_pct: NonNegativeFloat
    stage_prior_weeks: PositiveInt
    stage_volume_avg_weeks: PositiveInt
    stage_breakout_volume_mult: PositiveFloat
    major_swing_fractal_n: PositiveInt
    zone_base_max_atr_mult: PositiveFloat
    zone_lookback_weeks: PositiveInt
    ote_retracement: tuple[Fraction, Fraction]
    volume_profile_bins: Annotated[int, Field(ge=5)]
    value_area_pct: Fraction
    rs_sma_weeks: PositiveInt
    dma_days: PositiveInt
    high_52w_weeks: PositiveInt
    delivery_avg_days: PositiveInt
    delivery_recent_days: PositiveInt
    updown_volume_weeks: PositiveInt
    vcp: VcpConfig
    buy_zone_point_band_atr: NonNegativeFloat
    rs_percentile_max_age_days: PositiveInt
    min_weekly_bars: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        lo, hi = self.ote_retracement
        if lo >= hi:
            raise ValueError("ote_retracement must be [low, high]")
        if self.major_swing_fractal_n < self.swing_fractal_n:
            raise ValueError("major_swing_fractal_n must be >= swing_fractal_n")
        if self.delivery_recent_days > self.delivery_avg_days:
            raise ValueError("delivery_recent_days must be <= delivery_avg_days")
        return self


# ───────────────────────── jobs.yaml ─────────────────────────


class JobName(StrEnum):
    """SPEC §10 jobs plus ``corporate_actions`` (feeds split/bonus adjustment)."""

    CORPORATE_ACTIONS = "corporate_actions"
    EOD_PRICES = "eod_prices"
    NSE_BHAVCOPY = "nse_bhavcopy"
    TECHNICALS = "technicals"
    VALUATION_SCORES = "valuation_scores"
    ALERTS_INTRADAY = "alerts_intraday"
    SHAREHOLDING = "shareholding"
    INDEX_CONSTITUENTS = "index_constituents"
    RESULTS_BACKFILL = "results_backfill"
    REFRESH_QUEUE = "refresh_queue"
    BACKTESTS = "backtests"
    ANNUAL_REPORTS = "annual_reports"
    SYMBOL_MASTER = "symbol_master"
    RESULTS_WATCH = "results_watch"
    EVENTS = "events"
    RECONCILE = "reconcile"
    BHAVCOPY_HISTORY = "bhavcopy_history"
    BROKER_TOKEN_CHECK = "broker_token_check"
    THESIS = "thesis"
    INDUSTRY_CLASSIFICATION = "industry_classification"


class Season(_Strict):
    """Calendar window: ``months`` of the year, and day-of-month range [first, last]."""

    months: list[Annotated[int, Field(ge=1, le=12)]] = Field(min_length=1)
    days: tuple[Annotated[int, Field(ge=1, le=31)], Annotated[int, Field(ge=1, le=31)]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.days[0] > self.days[1]:
            raise ValueError("days must be [first, last]")
        return self

    def contains(self, month: int, day: int) -> bool:
        return month in self.months and self.days[0] <= day <= self.days[1]


class EodPricesJobConfig(_Strict):
    overlap_days: Annotated[int, Field(ge=0)]


class CorporateActionsJobConfig(_Strict):
    lookback_days: PositiveInt


class DoctorConfig(_Strict):
    """``make doctor`` thresholds (SPEC v0.2 §3.10 home deployment)."""

    disk_warn_gb: PositiveFloat  # free space below this is a warning
    disk_fail_gb: PositiveFloat  # ... and below this a failure
    backup_max_age_hours: PositiveFloat  # the last successful backup older than this: warning
    worker_max_idle_hours: PositiveFloat  # no job run for this long: is the worker running?
    bot_max_idle_minutes: PositiveFloat  # Telegram bot heartbeat older than this: warning
    nse_timeout_s: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.disk_fail_gb > self.disk_warn_gb:
            raise ValueError("disk_fail_gb must be <= disk_warn_gb")
        return self


class CatchUpConfig(_Strict):
    """Missed scheduled jobs run once on worker start (SPEC v0.2 §3.10)."""

    enabled: bool
    lookback_hours: PositiveFloat  # a fire time older than this is not caught up
    skip: list[str]  # left to their next trigger (high-frequency jobs, on-demand queues)

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = set(self.skip) - {j.value for j in JobName}
        if unknown:
            raise ValueError(f"catch_up.skip: unknown jobs {sorted(unknown)}")
        return self


class IndustryClassificationConfig(_Strict):
    """industry_classification job: industry label → sector model for the universe."""

    refresh_days: PositiveInt  # re-read a stock's classification after this many days
    max_per_run: PositiveInt  # NSE allows ~1 request/s: keep a run short


ThesisUnit = Literal["pct", "x", "days", "cr", "inr", "count"]


class ThesisFact(_Strict):
    label: str = Field(min_length=1)
    unit: ThesisUnit


class ThesisConfig(_Strict):
    """Optional LLM thesis (SPEC §8a): a paragraph written from the report's numbers only.
    Every number in the text must match a fact (``number_rel_tolerance``); drafts with an
    unknown number or a ``forbidden_phrases`` entry are rejected."""

    enabled: bool
    model: str = Field(min_length=1)
    timeout_s: PositiveFloat
    temperature: Annotated[float, Field(ge=0, le=2)]
    seed: int
    max_words: PositiveInt
    min_words: NonNegativeInt
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    number_rel_tolerance: Annotated[float, Field(ge=0, le=0.05)]
    max_reasons: NonNegativeInt
    nightly_scope: Literal["watchlist", "none"]
    max_per_run: PositiveInt
    forbidden_phrases: list[str]
    fundamentals: dict[str, ThesisFact]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_words >= self.max_words:
            raise ValueError("min_words must be below max_words")
        return self


class TelegramBotConfig(_Strict):
    """The read-only Telegram bot (SPEC §3.10, P23): long-polling, outbound only, answering only
    the owner's TELEGRAM_CHAT_ID."""

    enabled: bool  # runs in the worker when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
    poll_timeout_s: Annotated[int, Field(ge=1, le=50)]  # getUpdates long-poll (Telegram max 50)
    error_backoff_s: PositiveFloat  # first wait after a failed poll; doubles up to the max
    max_backoff_s: PositiveFloat
    max_message_age_s: PositiveInt  # older commands (sent while the bot was down) are skipped
    buyzone_near_pct: Fraction  # /buyzone also lists stocks this close above their zone
    buyzone_limit: PositiveInt
    status_jobs: list[str] = Field(min_length=1)  # jobs whose last run /status reports
    max_reply_chars: Annotated[int, Field(ge=200, le=4096)]  # Telegram caps a message at 4096

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.error_backoff_s > self.max_backoff_s:
            raise ValueError("error_backoff_s must be <= max_backoff_s")
        unknown = set(self.status_jobs) - {j.value for j in JobName}
        if unknown:
            raise ValueError(f"status_jobs: unknown jobs {sorted(unknown)}")
        return self


class AlertsJobConfig(_Strict):
    market_open: time
    market_close: time
    hysteresis_pct: Fraction
    cooldown_minutes: Annotated[int, Field(ge=0)]
    telegram_timeout_s: PositiveFloat

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.market_open >= self.market_close:
            raise ValueError("alerts.market_open must be before market_close")
        return self


class ResultsBackfillJobConfig(_Strict):
    """results_backfill: ingest exchange results filings (XBRL) incrementally."""

    max_downloads_per_run: PositiveInt  # XBRL documents per run; a backfill spreads over nights
    index_recheck_days: PositiveInt  # outside results season, re-read a symbol's list this often
    max_attempts: PositiveInt  # a filing that fails this many times stays failed


class AnnualReportsJobConfig(_Strict):
    """annual_reports: fill balance-sheet / cash-flow years the XBRL results lack from
    annual-report PDFs (SPEC §3.6 step 3)."""

    max_downloads_per_run: PositiveInt
    max_attempts: PositiveInt  # a report that fails this many times stays failed
    # A fiscal year needs a report when any of these items is missing for it (consolidated if
    # the company files consolidated figures, else standalone).
    required_items: dict[Literal["bs", "cf"], list[str]]
    first_fiscal_year: PositiveInt  # NSE lists annual reports from about this year


class EventClassificationConfig(_Strict):
    """Event categories from an event's title and text (lower-cased). A category matches when
    all words of any one of its rules appear; the first matching category (in file order) wins.
    ``red_flags`` categories are shown as red flags; ``auditor_resignation`` events feed the
    auditor knock-out."""

    categories: dict[str, list[list[str]]] = Field(min_length=1)
    red_flags: list[str]
    auditor_resignation: str  # the category naming an auditor's resignation
    red_flag_days: PositiveInt  # a red-flag event this recent is listed in the report

    @model_validator(mode="after")
    def _check(self) -> Self:
        unknown = {*self.red_flags, self.auditor_resignation} - set(self.categories)
        if unknown:
            raise ValueError(f"categories not defined: {sorted(unknown)}")
        for name, rules in self.categories.items():
            if not rules or any(not r or any(not w.strip() for w in r) for r in rules):
                raise ValueError(f"categories.{name}: every rule needs non-empty words")
            if any(w != w.lower() for r in rules for w in r):
                raise ValueError(f"categories.{name}: words must be lower case")
        return self


class EventsJobConfig(_Strict):
    """events: market-wide exchange feeds → the events table (SPEC §10)."""

    feeds: dict[Provider, list[EventKind]] = Field(min_length=1)
    lookback_days: PositiveInt  # each run re-reads this many days (late and revised entries)
    first_run_days: PositiveInt  # a feed never read before starts this far back
    # events and results_watch never read the exchanges at the same time (SPEC §3.2a): one
    # waits up to this long for the other to finish.
    lock_wait_s: PositiveInt


class ResultsNotifyConfig(_Strict):
    fv_change_rel: Fraction  # fair value moved more than this (relative) → notify


class ResultsWatchJobConfig(_Strict):
    """results_watch: results filings in the exchange feeds → per-stock pipelines → change
    notifications (SPEC v0.2 §3.8)."""

    feeds: dict[Provider, list[EventKind]] = Field(min_length=1)
    lookback_days: PositiveInt
    board_meeting_days_ahead: Annotated[int, Field(ge=0)]  # the calendar window shown
    results_purposes: list[str] = Field(min_length=1)  # board-meeting purposes meaning results
    # Another filing of the same results (the other basis, BSE after NSE) within this many
    # hours of a results run does not start another one.
    rerun_after_hours: PositiveFloat
    notify: ResultsNotifyConfig


ReconSource = Literal["nse_xbrl", "annual_report_pdf", "yfinance", "market_lens"]


class ReconciliationConfig(_Strict):
    """Cross-source checks of each new period (SPEC v0.2 §3.9)."""

    tolerance_rel: Fraction  # a difference above this (relative to the reference) is an issue
    min_diff_inr: Annotated[float, Field(ge=0)]  # ... and above this many rupees (rounding)
    years: PositiveInt  # the latest N fiscal years are checked
    quarters: Annotated[int, Field(ge=0)]  # ... and the latest N quarters (P&L items)
    items: list[str] = Field(min_length=1)
    sources: list[ReconSource] = Field(min_length=2)  # the first with a value is the reference
    unit_factors: list[Annotated[float, Field(gt=1)]]  # a ratio this close to one is "units"
    # Items a source defines differently, so never compared (yfinance EBITDA includes other
    # income; the canonical EBITDA excludes it).
    exclude: dict[ReconSource, list[str]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        from app.data.canonical import fields_for

        unknown = set(self.items) - set(fields_for("fin_annual"))
        if unknown:
            raise ValueError(f"items not in fin_annual: {sorted(unknown)}")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources lists a source twice")
        return self


class PipelineConfig(_Strict):
    """On-demand per-symbol pipeline (SPEC §3.7)."""

    poll_interval_s: PositiveFloat  # the worker checks for queued runs this often
    stale_after_s: PositiveInt  # a running run without a heartbeat this long is resumed
    max_attempts: PositiveInt  # a run interrupted this often is failed
    max_report_age_hours: PositiveFloat  # a stored report older than this is not "fresh"
    max_xbrl_downloads: PositiveInt  # results XBRL documents fetched per run
    max_annual_reports: PositiveInt  # annual-report PDFs read per run (gap filler)
    sse_poll_s: PositiveFloat  # the progress stream checks the run this often
    sse_heartbeat_s: PositiveFloat  # a keep-alive comment is sent at least this often
    sse_max_minutes: PositiveFloat  # a progress stream ends after this long


class BacktestConfig(_Strict):
    benchmark: str
    benchmark_tri: str | None = None
    cost_per_side: Fraction
    stt_buy: Fraction
    stt_sell: Fraction
    execution_lag_days: Annotated[int, Field(ge=0, le=5)]
    equity_curve_points: Literal["daily", "weekly"]


class JobsConfig(_Strict):
    timezone: str
    lock_ttl_s: PositiveInt
    misfire_grace_s: PositiveInt
    schedules: dict[JobName, str]
    universe_index: str
    constituent_indices: list[str] = Field(min_length=1)
    benchmark_indices: list[str]
    eod_prices: EodPricesJobConfig
    corporate_actions: CorporateActionsJobConfig
    alerts: AlertsJobConfig
    telegram_bot: TelegramBotConfig
    thesis: ThesisConfig
    industry_classification: IndustryClassificationConfig
    catch_up: CatchUpConfig
    doctor: DoctorConfig
    backtest: BacktestConfig
    results_backfill: ResultsBackfillJobConfig
    results_watch: ResultsWatchJobConfig
    events: EventsJobConfig
    event_classification: EventClassificationConfig
    reconciliation: ReconciliationConfig
    annual_reports: AnnualReportsJobConfig
    pipeline: PipelineConfig
    shareholding_season: Season
    results_season: Season

    @model_validator(mode="after")
    def _check(self) -> Self:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from apscheduler.triggers.cron import CronTrigger

        try:
            tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        missing = set(JobName) - set(self.schedules)
        if missing:
            raise ValueError(f"schedules missing jobs: {sorted(missing)}")
        for name, expr in self.schedules.items():
            try:
                CronTrigger.from_crontab(expr, timezone=tz)
            except ValueError as exc:
                raise ValueError(f"schedules.{name}: invalid cron {expr!r}: {exc}") from exc
        if self.universe_index not in self.constituent_indices:
            raise ValueError("universe_index must be one of constituent_indices")
        return self


# ───────────────────────── aggregate + loader ─────────────────────────


def normalise_label(label: str) -> str:
    """Industry label key: lower case, punctuation and repeated spaces removed."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", label.lower()).split())


class IndustriesConfig(_Strict):
    """``industries.yaml``: per-source industry label → sectors.yaml key."""

    nse_basic_industry: dict[str, str]
    yfinance_industry: dict[str, str]

    def table(self, source: str) -> dict[str, str]:
        raw = {"nse": self.nse_basic_industry, "yfinance": self.yfinance_industry}.get(source, {})
        return {normalise_label(k): v for k, v in raw.items()}


class AppConfig(_Strict):
    providers: ProvidersConfig
    valuation: ValuationConfig
    sectors: SectorsConfig
    scoring: ScoringConfig
    technical: TechnicalConfig
    jobs: JobsConfig
    industries: IndustriesConfig

    @model_validator(mode="after")
    def _industries_map_to_sectors(self) -> Self:
        bad = sorted({v for table in (self.industries.nse_basic_industry,
                                       self.industries.yfinance_industry)
                      for v in table.values() if v not in self.sectors.root})  # fmt: skip
        if bad:
            raise ValueError(f"industries.yaml maps to unknown sectors: {bad}")
        return self


CONFIG_FILES: tuple[str, ...] = tuple(AppConfig.model_fields)


def _read_section(config_dir: Path, name: str) -> Any:
    path = config_dir / f"{name}.yaml"
    try:
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {name}:
        raise ConfigError(f"{path} must contain exactly one top-level key '{name}'")
    return doc[name]


def load_config(config_dir: Path) -> AppConfig:
    """Load and validate every ``config/*.yaml`` file; raise :class:`ConfigError` on any issue."""
    raw = {name: _read_section(config_dir, name) for name in CONFIG_FILES}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {config_dir}:\n{exc}") from exc


@lru_cache
def get_config() -> AppConfig:
    return load_config(get_settings().config_dir)
