"""ORM models for every table in SPEC §3.4.

Conventions
- Money amounts are ₹ crore, per-share values are ₹, percentages are 0-100, ratios are fractions.
- Financial fields are nullable: missing data stays ``NULL`` and gets a ``data_gaps`` row
  (AGENTS.md rule 1). Never write 0 as a stand-in.
- Ingested data tables carry ``source`` + ``fetched_at`` (``SourcedMixin``); derived snapshots
  carry ``computed_at``.
- ``__upsert_key__`` names each table's natural key (a unique constraint) for ``app.db.upsert``.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, ComputedMixin, SourcedMixin, TimestampMixin, str_enum
from app.db.enums import (
    AlertType,
    BacktestStatus,
    Broker,
    CorporateActionType,
    JobStatus,
    StatementType,
    SurveillanceList,
    Timeframe,
)

__all__ = [
    "Alert",
    "Backtest",
    "Base",
    "BrokerToken",
    "CorporateAction",
    "DataGap",
    "DeliveryDaily",
    "FinAnnual",
    "FinQuarterly",
    "IndexMembership",
    "Instrument",
    "JobRun",
    "PriceDaily",
    "Report",
    "Score",
    "Shareholding",
    "SurveillanceFlag",
    "TechnicalSnapshot",
    "UserOverride",
    "ValuationSnapshot",
    "WatchlistItem",
]


def _instrument_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))


# ───────────────────────── reference data ─────────────────────────


class Instrument(SourcedMixin, Base):
    __tablename__ = "instruments"
    __upsert_key__ = ("symbol",)
    __table_args__ = (UniqueConstraint("symbol"), UniqueConstraint("isin"))

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))  # NSE trading symbol, e.g. "RELIANCE"
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(200))
    series: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str | None] = mapped_column(String(200))  # NSE industry
    sector: Mapped[str | None] = mapped_column(String(64))  # key into config/sectors.yaml
    is_index: Mapped[bool] = mapped_column(Boolean, server_default="false")
    listing_date: Mapped[date | None]
    face_value: Mapped[float | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


# ───────────────────────── prices ─────────────────────────


class PriceDaily(SourcedMixin, Base):
    """Raw OHLCV plus split/bonus-adjusted columns (rule 6). ``adj_*`` NULL until adjusted."""

    __tablename__ = "prices_daily"
    __upsert_key__ = ("instrument_id", "date")
    __table_args__ = (PrimaryKeyConstraint("instrument_id", "date"),)

    instrument_id: Mapped[int] = _instrument_fk()
    date: Mapped[date]
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    volume: Mapped[int] = mapped_column(BigInteger)
    adj_factor: Mapped[float | None]
    adj_open: Mapped[float | None]
    adj_high: Mapped[float | None]
    adj_low: Mapped[float | None]
    adj_close: Mapped[float | None]
    adj_volume: Mapped[int | None] = mapped_column(BigInteger)


class CorporateAction(SourcedMixin, Base):
    __tablename__ = "corporate_actions"
    __upsert_key__ = ("instrument_id", "ex_date", "action_type")
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date", "action_type"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    ex_date: Mapped[date]
    action_type: Mapped[CorporateActionType] = mapped_column(str_enum(CorporateActionType))
    record_date: Mapped[date | None]
    announcement_date: Mapped[date | None]
    # split/bonus ratio: ``ratio_new`` shares for every ``ratio_old`` held (bonus 1:1 → 2 for 1)
    ratio_old: Mapped[float | None]
    ratio_new: Mapped[float | None]
    dividend_per_share: Mapped[float | None]
    description: Mapped[str | None] = mapped_column(Text)


class DeliveryDaily(SourcedMixin, Base):
    __tablename__ = "delivery_daily"
    __upsert_key__ = ("instrument_id", "date")
    __table_args__ = (PrimaryKeyConstraint("instrument_id", "date"),)

    instrument_id: Mapped[int] = _instrument_fk()
    date: Mapped[date]
    traded_qty: Mapped[int | None] = mapped_column(BigInteger)
    deliverable_qty: Mapped[int | None] = mapped_column(BigInteger)
    delivery_pct: Mapped[float | None]
    traded_value_cr: Mapped[float | None]


# ───────────────────────── fundamentals ─────────────────────────


class _FinancialsCommon(SourcedMixin):
    """P&L lines shared by annual and quarterly statements (₹ Cr unless noted)."""

    statement_type: Mapped[StatementType] = mapped_column(str_enum(StatementType))
    period_end: Mapped[date]
    # Rule 4: backtests read fundamentals only after this date. NULL = unknown (→ data gap).
    announcement_date: Mapped[date | None]
    revenue: Mapped[float | None]
    cogs: Mapped[float | None]
    ebitda: Mapped[float | None]
    other_income: Mapped[float | None]
    depreciation: Mapped[float | None]
    ebit: Mapped[float | None]
    interest: Mapped[float | None]
    pbt: Mapped[float | None]
    tax: Mapped[float | None]
    pat: Mapped[float | None]
    minority_interest_pl: Mapped[float | None]
    eps_diluted: Mapped[float | None]  # ₹
    shares_diluted_cr: Mapped[float | None]
    # Provider-specific or sector-specific lines (e.g. bank NII, GNPA) not yet canonicalised.
    extra: Mapped[dict[str, Any] | None]


class FinAnnual(_FinancialsCommon, Base):
    __tablename__ = "fin_annual"
    __upsert_key__ = ("instrument_id", "statement_type", "period_end")
    __table_args__ = (
        UniqueConstraint("instrument_id", "statement_type", "period_end"),
        Index("ix_fin_annual_instrument_announcement", "instrument_id", "announcement_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    fiscal_year: Mapped[int]  # FY ending, e.g. 2025 for Apr-2024..Mar-2025
    sga: Mapped[float | None]
    # balance sheet
    total_assets: Mapped[float | None]
    current_assets: Mapped[float | None]
    current_liabilities: Mapped[float | None]
    total_equity: Mapped[float | None]
    retained_earnings: Mapped[float | None]
    minority_interest_bs: Mapped[float | None]
    total_debt: Mapped[float | None]
    cash_and_equivalents: Mapped[float | None]
    non_operating_investments: Mapped[float | None]
    receivables: Mapped[float | None]
    inventory: Mapped[float | None]
    payables: Mapped[float | None]
    net_block: Mapped[float | None]
    book_value_per_share: Mapped[float | None]  # ₹
    # cash flow
    cfo: Mapped[float | None]
    purchase_of_fixed_assets: Mapped[float | None]
    sale_of_fixed_assets: Mapped[float | None]
    dividends_paid: Mapped[float | None]


class FinQuarterly(_FinancialsCommon, Base):
    __tablename__ = "fin_quarterly"
    __upsert_key__ = ("instrument_id", "statement_type", "period_end")
    __table_args__ = (
        UniqueConstraint("instrument_id", "statement_type", "period_end"),
        Index("ix_fin_quarterly_instrument_announcement", "instrument_id", "announcement_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()


class Shareholding(SourcedMixin, Base):
    """Quarterly shareholding pattern, keyed by filing date for point-in-time use (rule 4)."""

    __tablename__ = "shareholding"
    __upsert_key__ = ("instrument_id", "period_end")
    __table_args__ = (UniqueConstraint("instrument_id", "period_end"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    period_end: Mapped[date]
    filing_date: Mapped[date | None]
    promoter_pct: Mapped[float | None]
    promoter_pledge_pct: Mapped[float | None]  # % of promoter holding pledged
    fii_pct: Mapped[float | None]
    dii_pct: Mapped[float | None]
    mf_pct: Mapped[float | None]
    public_pct: Mapped[float | None]
    num_shareholders: Mapped[int | None]


# ───────────────────────── universe & surveillance ─────────────────────────


class IndexMembership(SourcedMixin, Base):
    """Point-in-time index membership; ``effective_to`` NULL = still a member."""

    __tablename__ = "index_membership"
    __upsert_key__ = ("index_name", "instrument_id", "effective_from")
    __table_args__ = (UniqueConstraint("index_name", "instrument_id", "effective_from"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    index_name: Mapped[str] = mapped_column(String(64))  # e.g. "NIFTY 500"
    instrument_id: Mapped[int] = _instrument_fk()
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]
    weight_pct: Mapped[float | None]


class SurveillanceFlag(SourcedMixin, Base):
    """ASM / GSM / F&O-ban listings; ``effective_to`` NULL = currently listed."""

    __tablename__ = "surveillance_flags"
    __upsert_key__ = ("instrument_id", "list_name", "effective_from")
    __table_args__ = (UniqueConstraint("instrument_id", "list_name", "effective_from"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    list_name: Mapped[SurveillanceList] = mapped_column(str_enum(SurveillanceList))
    stage: Mapped[str | None] = mapped_column(String(16))
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]


# ───────────────────────── derived snapshots ─────────────────────────


class ValuationSnapshot(ComputedMixin, Base):
    __tablename__ = "valuation_snapshots"
    __upsert_key__ = ("instrument_id", "as_of")
    __table_args__ = (UniqueConstraint("instrument_id", "as_of"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    as_of: Mapped[date]
    cmp: Mapped[float | None]
    baseline: Mapped[float | None]
    fair_value: Mapped[float | None]
    top_band: Mapped[float | None]
    mos_pct: Mapped[float | None]
    zone: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[str | None] = mapped_column(String(16))
    methods: Mapped[list[Any] | None]  # [{name, value, weight}]
    reverse_dcf: Mapped[dict[str, Any] | None]
    sensitivity: Mapped[dict[str, Any] | None]
    inputs: Mapped[dict[str, Any] | None]  # assumptions used, incl. applied user overrides
    reasons: Mapped[list[str]] = mapped_column(server_default="[]")


class TechnicalSnapshot(ComputedMixin, Base):
    __tablename__ = "technical_snapshots"
    __upsert_key__ = ("instrument_id", "as_of", "timeframe")
    __table_args__ = (
        UniqueConstraint("instrument_id", "as_of", "timeframe"),
        CheckConstraint("stage BETWEEN 1 AND 4", name="stage_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    as_of: Mapped[date]
    timeframe: Mapped[Timeframe] = mapped_column(str_enum(Timeframe))
    stage: Mapped[int | None]
    rs_percentile: Mapped[float | None]
    trend_state: Mapped[str | None] = mapped_column(String(32))
    buy_zone_low: Mapped[float | None]
    buy_zone_high: Mapped[float | None]
    invalidation: Mapped[float | None]
    atr: Mapped[float | None]
    detail: Mapped[dict[str, Any] | None]  # zones, swings, AVWAPs, volume profile
    reasons: Mapped[list[str]] = mapped_column(server_default="[]")


class Score(ComputedMixin, Base):
    __tablename__ = "scores"
    __upsert_key__ = ("instrument_id", "as_of")
    __table_args__ = (UniqueConstraint("instrument_id", "as_of"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    as_of: Mapped[date]
    quality: Mapped[float | None]
    growth: Mapped[float | None]
    valuation: Mapped[float | None]
    health: Mapped[float | None]
    governance: Mapped[float | None]
    technical: Mapped[float | None]
    total: Mapped[float | None]
    provisional_grade: Mapped[str | None] = mapped_column(String(8))
    grade: Mapped[str | None] = mapped_column(String(8))
    knockouts: Mapped[list[str]] = mapped_column(server_default="[]")
    earned_premium: Mapped[int | None]
    action: Mapped[str | None] = mapped_column(String(32))
    sub_scores: Mapped[dict[str, Any] | None]
    reasons: Mapped[list[str]] = mapped_column(server_default="[]")


class Report(ComputedMixin, Base):
    """Assembled StockReport DTO (SPEC §8)."""

    __tablename__ = "reports"
    __upsert_key__ = ("instrument_id", "as_of")
    __table_args__ = (UniqueConstraint("instrument_id", "as_of"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    as_of: Mapped[date]
    payload: Mapped[dict[str, Any]]
    sources: Mapped[dict[str, Any] | None]
    thesis: Mapped[str | None] = mapped_column(Text)


# ───────────────────────── user & ops ─────────────────────────


class WatchlistItem(TimestampMixin, Base):
    __tablename__ = "watchlist"
    __upsert_key__ = ("instrument_id",)
    __table_args__ = (UniqueConstraint("instrument_id"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    notes: Mapped[str | None] = mapped_column(Text)


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"
    __upsert_key__ = ("instrument_id", "alert_type")
    __table_args__ = (UniqueConstraint("instrument_id", "alert_type"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    alert_type: Mapped[AlertType] = mapped_column(str_enum(AlertType))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    last_triggered_at: Mapped[datetime | None]
    last_triggered_price: Mapped[float | None]


class BrokerToken(TimestampMixin, Base):
    """Fernet-encrypted access token (rule 8). Never store or log the plaintext."""

    __tablename__ = "broker_tokens"
    __upsert_key__ = ("broker",)

    broker: Mapped[Broker] = mapped_column(str_enum(Broker), primary_key=True)
    access_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]


class JobRun(Base):
    __tablename__ = "job_runs"
    __upsert_key__ = ("id",)
    __table_args__ = (Index("ix_job_runs_job_started", "job_name", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus))
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    params: Mapped[dict[str, Any] | None]
    rows_written: Mapped[int | None]
    details: Mapped[dict[str, Any] | None]  # per-job summary: counts, failed symbols, sources
    error: Mapped[str | None] = mapped_column(Text)


class DataGap(Base):
    """Rule 1: every missing value surfaces here instead of being defaulted."""

    __tablename__ = "data_gaps"
    __upsert_key__ = ("instrument_id", "dataset", "field", "period")
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "dataset", "field", "period", postgresql_nulls_not_distinct=True
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE")
    )
    dataset: Mapped[str] = mapped_column(String(32))  # config Dataset value
    field: Mapped[str | None] = mapped_column(String(64))
    period: Mapped[date | None]
    reason: Mapped[str] = mapped_column(Text)
    providers_tried: Mapped[list[str]] = mapped_column(server_default="[]")
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]


class UserOverride(TimestampMixin, Base):
    """Per-stock assumption overrides (e.g. ``g1``, ``wacc``, ``sector_model``)."""

    __tablename__ = "user_overrides"
    __upsert_key__ = ("instrument_id", "key")
    __table_args__ = (UniqueConstraint("instrument_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict[str, Any]]  # {"value": ...} so any JSON type can be stored


class Backtest(TimestampMixin, Base):
    """A backtest request (SPEC §8, §11) and, once run, its results."""

    __tablename__ = "backtests"
    __upsert_key__ = ("id",)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    status: Mapped[BacktestStatus] = mapped_column(str_enum(BacktestStatus))
    params: Mapped[dict[str, Any]]
    results: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
