"""Request/response schemas shared by the API routers (SPEC §8)."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import GradeKey, ZoneKey
from app.db.enums import AlertType, BacktestStatus, JobStatus
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


class RefreshQueued(BaseModel):
    symbol: str
    queued: bool = Field(description="False when the symbol was already waiting")
    queue_length: int


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
    refresh_queue: list[str]


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
