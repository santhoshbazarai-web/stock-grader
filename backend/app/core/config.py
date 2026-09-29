"""Typed, validated view of ``config/*.yaml``.

Every tunable threshold, weight and formula parameter lives in YAML. These models only
describe the *shape* and *internal consistency* of that YAML (types, ranges, ordering,
weights summing correctly); they carry no defaults for model parameters so a missing key
fails at startup instead of silently falling back.

Load with :func:`load_config` (explicit directory) or :func:`get_config` (cached, uses
``Settings.config_dir``). Any problem raises :class:`ConfigError` listing every issue.
"""

import math
from datetime import time
from enum import StrEnum
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    RootModel,
    ValidationError,
    model_validator,
)

from app.core.settings import get_settings

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Score = Annotated[float, Field(ge=0.0, le=100.0)]
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


class NseConfig(_Strict):
    base_url: str
    archives_url: str
    niftyindices_url: str
    cookie_ttl_s: PositiveFloat
    request_timeout_s: PositiveFloat
    corporate_actions_from_years: PositiveInt
    index_constituent_files: dict[str, str]


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
    oauth_state_ttl_s: PositiveInt
    history_years: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
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


class BandsConfig(_Strict):
    lookback_years: list[PositiveInt] = Field(min_length=1)
    multiples: list[Literal["pe", "ev_ebitda", "pb"]] = Field(min_length=1)
    min_observations: PositiveInt


class EpvConfig(_Strict):
    normalise_years: PositiveInt


class BlendConfig(_Strict):
    min_weight_coverage: Fraction
    asset_heavy_book_multiple: NonNegativeFloat


class RelativeConfig(_Strict):
    roce_exponent: NonNegativeFloat
    growth_exponent: NonNegativeFloat


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
    negative_cfo_years_in_5: Annotated[int, Field(ge=1, le=5)]
    min_mcap_cr: NonNegativeFloat
    min_avg_traded_value_cr_20d: NonNegativeFloat
    beneish_m_max: float
    auditor_resignation_years: PositiveInt
    on_asm_gsm: bool
    cap_grade: Literal["A_plus", "A", "B", "C", "D"]


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
    stage_score: dict[Literal[1, 2, 3, 4], Score]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if set(self.stage_score) != {1, 2, 3, 4}:
            raise ValueError("stage_score must define stages 1, 2, 3 and 4")
        return self


class EarnedPremiumConfig(_Strict):
    momentum_entry_min: Annotated[int, Field(ge=0, le=8)]
    rs_percentile_min: Score
    near_52w_high_pct: Fraction


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
    earned_premium: EarnedPremiumConfig
    bank_maps: BankMaps
    fundamentals: FundamentalsConfig
    forensic: ForensicConfig


# ───────────────────────── technical.yaml ─────────────────────────


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
    RESULTS_WATCH = "results_watch"


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


class AppConfig(_Strict):
    providers: ProvidersConfig
    valuation: ValuationConfig
    sectors: SectorsConfig
    scoring: ScoringConfig
    technical: TechnicalConfig
    jobs: JobsConfig


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
