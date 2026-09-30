"""Request/response schemas shared by the API routers (SPEC §8)."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import GradeKey, ZoneKey
from app.db.enums import AlertType, BacktestStatus, FilingStatus, JobStatus, ReviewStatus
from app.reports.dto import StockReport
from app.reports.overrides import Overrides

SYMBOL_PATTERN = r"^[A-Za-z0-9&_.\-]{1,32}$"
Symbol = Annotated[str, Path(pattern=SYMBOL_PATTERN, description="NSE symbol, e.g. RELIANCE")]
SymbolField = Annotated[str, Field(pattern=SYMBOL_PATTERN, description="NSE symbol")]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InstrumentOut(_Out):
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    is_index: bool


class SearchHitOut(BaseModel):
    """A company found by ``GET /api/stocks/search`` (SPEC §3.5)."""

    symbol: str | None = Field(description="NSE symbol; null for a BSE-only company")
    name: str | None
    isin: str | None
    bse_code: str | None
    series: str | None
    sector: str | None
    industry: str | None
    is_index: bool
    in_universe_index: bool = Field(description="A current member of the universe index "
                                    "(jobs.universe_index, Nifty 500)")  # fmt: skip
    active: bool = Field(description="False: missing from the latest exchange masters")
    match: Literal["symbol", "name", "isin", "bse_code", "fyers_symbol", "former_symbol",
                   "former_name", "bse_symbol", "bse_name", "user"] = Field(
        description="What matched")  # fmt: skip
    matched: str = Field(description="The symbol, code, name or alias that matched")
    exact: bool
    score: float = Field(description="pg_trgm similarity 0-1 (0 for an exact code match)")


class AliasOut(BaseModel):
    id: int
    alias: str
    kind: str
    source: str
    valid_until: date | None


class AliasIn(BaseModel):
    alias: str = Field(min_length=2, max_length=200)


class RefreshQueued(BaseModel):
    symbol: str
    queued: bool = Field(description="False when a run for the symbol was already waiting")
    queue_length: int = Field(description="Pipeline runs queued or running")
    run_id: int = Field(description="Follow it on GET /api/pipeline/{run_id}/events")


class Sensitivity(BaseModel):
    symbol: str
    as_of: date
    waccs: list[float]
    terminal_growths: list[float]
    values: list[list[float | None]] = Field(
        description="Per-share value for waccs[i] (rows) x terminal_growths[j] (columns)"
    )
    base_wacc: float
    base_g_terminal: float


class OverridesResponse(BaseModel):
    symbol: str
    overrides: Overrides
    report: StockReport | None = Field(description="Recomputed report (null if no prices yet)")
    reasons: list[str]


class ScreenerRow(BaseModel):
    symbol: str
    name: str | None
    sector: str
    as_of: date
    cmp: float
    grade: str | None
    grade_label: str | None
    zone: str | None
    action: str | None
    total_score: float | None
    fair_value: float | None
    buy_zone_low: float | None
    buy_zone_high: float | None
    pct_to_buy_zone: float | None = Field(
        description="CMP vs the buy zone: 0 inside, >0 above (fall needed), <0 below"
    )
    earned_premium: int | None
    rs_percentile: float | None
    market_cap_cr: float | None


class WatchlistIn(BaseModel):
    symbol: SymbolField
    notes: str | None = Field(None, max_length=2000)


class WatchlistOut(BaseModel):
    symbol: str
    name: str | None
    notes: str | None
    added_at: datetime
    grade: str | None
    zone: str | None
    action: str | None
    cmp: float | None


class AlertIn(BaseModel):
    symbol: SymbolField
    alert_type: AlertType
    is_active: bool = True


class AlertOut(BaseModel):
    id: int
    symbol: str
    alert_type: AlertType
    is_active: bool
    last_triggered_at: datetime | None
    last_triggered_price: float | None
    created_at: datetime


class UploadSummary(BaseModel):
    symbol: str
    statement_type: Literal["consolidated", "standalone"]
    annual_rows: int
    quarterly_rows: int
    shareholding_rows: int
    data_gaps: list[str]
    warnings: list[str]


class XbrlFileResult(BaseModel):
    filename: str
    status: FilingStatus
    periods: list[str] = Field(description='e.g. ["quarter 2024-03-31", "year 2024-03-31"]')
    statement_type: Literal["consolidated", "standalone"] | None
    announcement_date: date | None = Field(
        description="Board-meeting date + 1 day (an upload carries no dissemination time)"
    )
    warnings: list[str]
    error: str | None


class XbrlUploadSummary(BaseModel):
    symbol: str
    files: list[XbrlFileResult]


class ResultFilingOut(BaseModel):
    id: int
    symbol: str
    exchange: str
    document: str
    period_start: date | None
    period_end: date | None
    statement_type: Literal["consolidated", "standalone"] | None
    audited: bool | None
    is_bank: bool | None
    disseminated_at: datetime | None
    announcement_date: date | None
    status: FilingStatus
    attempts: int
    error: str | None
    periods: list[str] | None
    warnings: list[str] | None
    parsed_at: datetime | None
    updated_at: datetime


class FilingsSummary(BaseModel):
    pending: int
    parsed: int
    failed: int
    symbols: int = Field(description="Instruments with at least one parsed filing")
    last_parsed_at: datetime | None


ConfigFileName = Literal["providers", "valuation", "sectors", "scoring", "technical", "jobs"]


class ConfigFile(BaseModel):
    name: ConfigFileName
    yaml: str


class ConfigView(BaseModel):
    files: list[ConfigFile]
    parsed: dict[str, Any] = Field(description="The validated config currently in effect")


class ConfigUpdate(BaseModel):
    name: ConfigFileName
    yaml: str = Field(max_length=200_000)


class ConfigSaved(BaseModel):
    name: ConfigFileName
    saved: bool
    note: str


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grades: list[GradeKey] = Field(min_length=1, description="Buy stocks with these grades…")
    zones: list[ZoneKey] = Field(min_length=1, description="…when in these zones")
    holding_days: int = Field(ge=5, le=2520, description="Holding period in trading days")
    start: date
    end: date
    symbols: list[SymbolField] | None = Field(
        None,
        max_length=1000,
        description="Test these stocks instead of the point-in-time Nifty 500 (survivorship bias)",
    )

    @model_validator(mode="after")
    def _check(self) -> "BacktestRequest":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class BacktestOut(BaseModel):
    id: int
    status: BacktestStatus
    params: dict[str, Any]
    results: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class BacktestSummary(BaseModel):
    id: int
    status: BacktestStatus
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    progress: dict[str, int] | None
    trades: int | None
    cagr: float | None
    benchmark_cagr: float | None
    error: str | None


class JobRunOut(_Out):
    id: int
    job_name: str
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None
    params: dict[str, Any] | None
    rows_written: int | None
    details: dict[str, Any] | None
    error: str | None


class Freshness(BaseModel):
    prices: date | None = Field(description="Latest stored daily bar")
    delivery: date | None
    fundamentals_fetched: datetime | None
    shareholding_period: date | None
    technicals: date | None
    reports: date | None


class JobsView(BaseModel):
    runs: list[JobRunOut]
    freshness: Freshness
    open_data_gaps: int
    refresh_queue: list[str] = Field(description="Symbols with a pipeline run queued or running")


class ScreenerFilters(BaseModel):
    """The screener's query parameters, as stored in a preset."""

    model_config = ConfigDict(extra="forbid")

    grade: list[GradeKey] = []
    zone: list[ZoneKey] = []
    sector: list[str] = []
    action: list[str] = []
    min_earned_premium: int | None = Field(None, ge=0, le=8)
    max_distance_to_buy_zone: float | None = None
    min_mcap_cr: float | None = Field(None, ge=0)
    max_mcap_cr: float | None = Field(None, ge=0)
    sort: str = "total_score"
    order: Literal["asc", "desc"] = "desc"


class PresetIn(BaseModel):
    filters: ScreenerFilters


class PresetOut(BaseModel):
    name: str
    filters: ScreenerFilters
    updated_at: datetime


class UploadedDataset(BaseModel):
    symbol: str
    name: str | None
    statement_type: Literal["consolidated", "standalone"]
    annual_years: int
    first_fiscal_year: int | None
    last_fiscal_year: int | None
    quarters: int
    uploaded_at: datetime


# ───────────── annual-report PDFs, review queue, coverage (SPEC v0.2 §3.6 steps 3-4) ─────────────


class AnnualReportOut(BaseModel):
    id: int
    symbol: str
    exchange: str = Field(description="nse | upload")
    document: str
    fiscal_year: int | None
    disseminated_at: datetime | None
    usable_from: date | None = Field(description="First day a signal may use it (rule 4)")
    status: FilingStatus
    attempts: int
    error: str | None
    page_count: int | None
    statements: list[dict[str, Any]] | None = Field(
        description="Per statement found: statement, basis, pages, method, unit, column_dates, "
        "checks"
    )
    warnings: list[str] | None
    has_document: bool = Field(description="The raw file is cached (can be opened / re-read)")
    candidates: dict[str, int] = Field(description="Values read, per review status")
    parsed_at: datetime | None
    updated_at: datetime


class PdfCandidateOut(BaseModel):
    id: int
    symbol: str
    annual_report_id: int
    fiscal_year: int | None = Field(description="The report's fiscal year")
    statement: Literal["bs", "cf"]
    basis: Literal["consolidated", "standalone"]
    period_end: date
    item_code: str
    value_cr: float | None = Field(description="As read, in ₹ crore (None: no unit stated)")
    corrected_value_cr: float | None
    raw_value: float = Field(description="As printed, in the page's unit")
    raw_label: str
    pages: list[int]
    method: str
    confidence: float
    reasons: list[str]
    status: ReviewStatus
    stored: bool = Field(description="In fin_line_items (not when the exchange XBRL has it)")
    note: str | None
    reviewed_at: datetime | None


class ReviewRequest(BaseModel):
    action: Literal["accept", "correct", "reject"]
    value_cr: float | None = Field(default=None, description="correct: the value in ₹ crore")
    item_code: str | None = Field(
        default=None, pattern=r"^[a-z_]{1,64}$",
        description="correct: another item of the same statement, when the label was mis-mapped",
    )  # fmt: skip


class ReviewSummary(BaseModel):
    pending: int
    reports_parsed: int
    reports_failed: int


class CoverageCellOut(BaseModel):
    fiscal_year: int
    statement: Literal["P&L", "BS", "CF"]
    sources: list[str] = Field(description="Best first: xbrl, pdf, derived, screener, yfinance; "
                               "empty = a gap")  # fmt: skip
    items: int
    pending_review: int


class CoverageBasis(BaseModel):
    basis: Literal["consolidated", "standalone"]
    cells: list[CoverageCellOut]


class CoverageGridOut(BaseModel):
    symbol: str
    years: list[int]
    fy_end_month: int
    bases: list[CoverageBasis]


# ───────────── on-demand pipeline (SPEC v0.2 §3.7) ─────────────


class PipelineStep(BaseModel):
    name: str
    label: str
    optional: bool
    status: Literal["pending", "running", "ok", "warning", "failed", "skipped"]
    message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class PipelineRunOut(BaseModel):
    id: int
    symbol: str
    trigger: str
    status: Literal["queued", "running", "done", "failed"]
    steps: list[PipelineStep]
    version: int
    attempts: int
    error: str | None
    report_as_of: date | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PipelineRequest(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    force: bool = Field(default=False, description="Run even when the stored report is fresh")


class PipelineStart(BaseModel):
    symbol: str
    fresh: bool = Field(description="The stored report is up to date: no run was started")
    reason: str
    report_as_of: date | None
    run: PipelineRunOut | None
