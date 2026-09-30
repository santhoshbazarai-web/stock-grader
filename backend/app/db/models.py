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
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, ComputedMixin, SourcedMixin, TimestampMixin, str_enum
from app.db.enums import (
    AlertType,
    AliasKind,
    BacktestStatus,
    Broker,
    CorporateActionType,
    FilingStatus,
    JobStatus,
    LineStatement,
    PeriodType,
    PipelineStatus,
    ReviewStatus,
    StatementType,
    SurveillanceList,
    SymbolStatus,
    Timeframe,
)

__all__ = [
    "Alert",
    "AnnualReport",
    "Backtest",
    "Base",
    "BrokerToken",
    "CorporateAction",
    "DataGap",
    "DeliveryDaily",
    "FinAnnual",
    "FinLineItem",
    "FinQuarterly",
    "IndexMembership",
    "Instrument",
    "JobRun",
    "Notification",
    "PdfLineCandidate",
    "PipelineRun",
    "PriceDaily",
    "Report",
    "ResultFiling",
    "Score",
    "ScreenerPreset",
    "Shareholding",
    "SurveillanceFlag",
    "Symbol",
    "SymbolAlias",
    "TechnicalSnapshot",
    "UserOverride",
    "ValuationSnapshot",
    "WatchlistItem",
]


def _trgm(name: str, column: str) -> Index:
    """pg_trgm GIN index on lower(column), for fuzzy search (SPEC §3.5). ``fastupdate`` off:
    these tables change rarely, and a pending list makes the planner avoid the index."""
    expr = func.lower(literal_column(column)).label(f"lower_{column}")
    return Index(name, expr, postgresql_using="gin", postgresql_ops={expr.name: "gin_trgm_ops"},
                 postgresql_with={"fastupdate": "off"})  # fmt: skip


def _instrument_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))


# ───────────────────────── reference data ─────────────────────────


class Instrument(SourcedMixin, Base):
    __tablename__ = "instruments"
    __upsert_key__ = ("symbol",)
    __table_args__ = (
        UniqueConstraint("symbol"),
        UniqueConstraint("isin"),
        _trgm("ix_instruments_symbol_trgm", "symbol"),  # symbol search (SPEC §3.5)
        _trgm("ix_instruments_name_trgm", "name"),
    )

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


class Symbol(TimestampMixin, Base):
    """Symbol master (SPEC v0.2 §3.5): one row per ISIN, joining NSE ``EQUITY_L``, the BSE scrip
    master and the Fyers symbol master. ``instrument_id`` links companies listed on NSE to
    their instrument (history is keyed there); BSE-only companies have none.

    Search uses pg_trgm GIN indexes on lower(name) here, on symbol_aliases.alias and on
    instruments.symbol / name; they are created in migration b5f6a7c8d9e0 (expression indexes
    are not declared on the models)."""

    __tablename__ = "symbols"
    __upsert_key__ = ("isin",)
    __table_args__ = (
        UniqueConstraint("isin"),
        Index("ix_symbols_nse_symbol", "nse_symbol"),
        Index("ix_symbols_bse_code", "bse_code"),
        Index("ix_symbols_fyers_symbol", "fyers_symbol"),
        Index("ix_symbols_instrument_id", "instrument_id"),
        _trgm("ix_symbols_name_trgm", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    isin: Mapped[str] = mapped_column(String(12))
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(200))
    nse_symbol: Mapped[str | None] = mapped_column(String(32))
    nse_series: Mapped[str | None] = mapped_column(String(8))
    bse_code: Mapped[str | None] = mapped_column(String(10))  # e.g. "500180"
    bse_id: Mapped[str | None] = mapped_column(String(32))  # BSE's short symbol
    fyers_symbol: Mapped[str | None] = mapped_column(String(48))  # e.g. "NSE:HDFCBANK-EQ"
    listing_date: Mapped[date | None]
    face_value: Mapped[float | None]
    status: Mapped[SymbolStatus] = mapped_column(str_enum(SymbolStatus))
    sources: Mapped[list[str]]  # masters that list it: nse, bse, fyers
    last_seen: Mapped[date | None]  # the last master refresh that listed it


class SymbolAlias(Base):
    """Other names a company is found by: former symbols and names (NSE change files), BSE's
    symbol and name, and aliases the owner adds."""

    __tablename__ = "symbol_aliases"
    __upsert_key__ = ("symbol_id", "kind", "alias")
    __table_args__ = (
        UniqueConstraint("symbol_id", "kind", "alias"),
        _trgm("ix_symbol_aliases_alias_trgm", "alias"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(200))
    kind: Mapped[AliasKind] = mapped_column(str_enum(AliasKind))
    source: Mapped[str] = mapped_column(String(16))  # nse | bse | user
    valid_until: Mapped[date | None]  # when it stopped being the symbol / name
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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
    # P&L summed from the year's four quarterly results because no annual figures were filed
    # (or parsed) for it: SPEC §3.6 step 5. See fin_line_items rows with derived = true.
    is_derived: Mapped[bool] = mapped_column(Boolean, server_default="false")
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


class ResultFiling(TimestampMixin, Base):
    """Ledger of exchange results filings (XBRL): what the exchange lists, and whether each
    document was downloaded and stored. ``document`` is the XBRL URL, or ``upload:<sha256>``
    for an uploaded file. Figures land in fin_quarterly / fin_annual with ``source='nse'``."""

    __tablename__ = "result_filings"
    __upsert_key__ = ("instrument_id", "document")
    __table_args__ = (
        UniqueConstraint("instrument_id", "document"),
        Index("ix_result_filings_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    exchange: Mapped[str] = mapped_column(String(16))  # nse | upload
    document: Mapped[str] = mapped_column(String(512))
    period_start: Mapped[date | None]
    period_end: Mapped[date | None]
    statement_type: Mapped[StatementType | None] = mapped_column(str_enum(StatementType))
    audited: Mapped[bool | None]
    is_bank: Mapped[bool | None]
    # When the exchange published it, and the first day a close-based signal may use it.
    disseminated_at: Mapped[datetime | None]
    announcement_date: Mapped[date | None]
    status: Mapped[FilingStatus] = mapped_column(str_enum(FilingStatus))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    # The document as cached before parsing, relative to settings.raw_data_dir (SPEC §3.2a).
    raw_path: Mapped[str | None] = mapped_column(String(512))
    periods: Mapped[list[str] | None]  # e.g. ["quarter 2024-03-31", "year 2024-03-31"]
    warnings: Mapped[list[str] | None]
    parsed_at: Mapped[datetime | None]


class FinLineItem(Base):
    """Long-format fundamentals (SPEC v0.2 §3.4, §3.6): one value of one mapped XBRL line item
    for one period, basis and version. A later filing that reports a different figure for the
    same period (a restatement, usually seen in its comparative columns) adds a version instead
    of overwriting; versions are ordered by when each figure became public. Analysis uses the
    latest version (fin_quarterly / fin_annual are rebuilt from it); backtests use the version
    available at each date. ``value_inr`` is in rupees for amounts, else in ``unit``."""

    __tablename__ = "fin_line_items"
    __upsert_key__ = (
        "instrument_id", "period_end", "period_type", "statement", "basis", "item_code", "version",
    )  # fmt: skip
    __table_args__ = (
        UniqueConstraint(*__upsert_key__, name="uq_fin_line_items_key"),  # default name > 63 chars
        Index("ix_fin_line_items_instrument_basis", "instrument_id", "basis", "period_end"),
        Index("ix_fin_line_items_filing", "filing_id"),
        Index("ix_fin_line_items_annual_report", "annual_report_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    isin: Mapped[str | None] = mapped_column(String(12))
    period_end: Mapped[date]
    period_type: Mapped[PeriodType] = mapped_column(str_enum(PeriodType))
    statement: Mapped[LineStatement] = mapped_column(str_enum(LineStatement))
    basis: Mapped[StatementType] = mapped_column(str_enum(StatementType))
    item_code: Mapped[str] = mapped_column(String(64))
    value_inr: Mapped[float]
    unit: Mapped[str] = mapped_column(String(16))  # amount | per_share | pct | shares
    version: Mapped[int]
    source: Mapped[str] = mapped_column(String(32))  # nse_xbrl | upload_xbrl | derived
    filing_id: Mapped[int | None] = mapped_column(
        ForeignKey("result_filings.id", ondelete="SET NULL")
    )
    announced_at: Mapped[datetime | None]  # when the exchange published the filing
    usable_from: Mapped[date | None]  # first day a close-based signal may use it (rule 4)
    derived: Mapped[bool] = mapped_column(Boolean, server_default="false")
    tag: Mapped[str | None] = mapped_column(String(512))  # the XBRL element(s) that matched
    map_version: Mapped[int | None]  # xbrl_map.yaml version (pdf_labels.yaml for PDF values)
    # Annual-report PDF values (source annual_report_pdf): the report and the read's confidence.
    annual_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("annual_reports.id", ondelete="CASCADE")
    )
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AnnualReport(TimestampMixin, Base):
    """Ledger of annual reports (PDF) read as the balance-sheet / cash-flow gap filler (SPEC
    v0.2 §3.6 step 3). ``document`` is the exchange URL, or ``upload:<sha256>``. Values read
    land in pdf_line_candidates; accepted ones in fin_line_items (source annual_report_pdf)."""

    __tablename__ = "annual_reports"
    __upsert_key__ = ("instrument_id", "document")
    __table_args__ = (
        UniqueConstraint("instrument_id", "document"),
        Index("ix_annual_reports_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int] = _instrument_fk()
    exchange: Mapped[str] = mapped_column(String(16))  # nse | upload
    document: Mapped[str] = mapped_column(String(512))
    fiscal_year: Mapped[int | None]  # the year the report covers (FY ending in this year)
    disseminated_at: Mapped[datetime | None]
    usable_from: Mapped[date | None]  # first day a close-based signal may use it (rule 4)
    status: Mapped[FilingStatus] = mapped_column(str_enum(FilingStatus))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    raw_path: Mapped[str | None] = mapped_column(String(512))  # the cached PDF (SPEC §3.2a)
    page_count: Mapped[int | None]
    # per statement found: statement, basis, pages, method, unit, column dates, checks
    statements: Mapped[list[Any] | None]
    warnings: Mapped[list[str] | None]
    labels_version: Mapped[int | None]  # pdf_labels.yaml version
    parsed_at: Mapped[datetime | None]


class PdfLineCandidate(TimestampMixin, Base):
    """One value read from an annual report, and its review state (the review queue holds the
    ``pending`` ones). ``stored`` says whether it is in fin_line_items: an accepted value is not
    stored where the exchange XBRL already has the item (``note`` says so)."""

    __tablename__ = "pdf_line_candidates"
    __upsert_key__ = (
        "annual_report_id", "basis", "statement", "period_end", "item_code",
    )  # fmt: skip
    __table_args__ = (
        UniqueConstraint(*__upsert_key__, name="uq_pdf_line_candidates_key"),
        Index("ix_pdf_line_candidates_status", "status"),
        Index("ix_pdf_line_candidates_instrument", "instrument_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    annual_report_id: Mapped[int] = mapped_column(
        ForeignKey("annual_reports.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[int] = _instrument_fk()
    statement: Mapped[LineStatement] = mapped_column(str_enum(LineStatement))
    basis: Mapped[StatementType] = mapped_column(str_enum(StatementType))
    period_end: Mapped[date]
    period_type: Mapped[PeriodType] = mapped_column(str_enum(PeriodType))
    item_code: Mapped[str] = mapped_column(String(64))
    value_inr: Mapped[float | None]  # as read, scaled to rupees (None: the page has no unit)
    raw_value: Mapped[float]  # as printed
    raw_label: Mapped[str] = mapped_column(String(512))
    pages: Mapped[list[Any]]  # 1-based page numbers
    method: Mapped[str] = mapped_column(String(16))  # pdfplumber | camelot
    confidence: Mapped[float]
    reasons: Mapped[list[str]]
    status: Mapped[ReviewStatus] = mapped_column(str_enum(ReviewStatus))
    corrected_value_inr: Mapped[float | None]  # the owner's value (status corrected)
    stored: Mapped[bool] = mapped_column(Boolean, server_default="false")
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None]


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


class PipelineRun(TimestampMixin, Base):
    """One on-demand pipeline run for a symbol (SPEC §3.7). ``steps`` holds each step's
    ``{name, label, optional, status, message, started_at, finished_at}``; a run is resumed from
    its first unfinished step. ``version`` increases on every change (the SSE endpoint streams
    each new version); ``heartbeat_at`` lets another worker take over a run whose worker died."""

    __tablename__ = "pipeline_runs"
    __upsert_key__ = ("id",)
    __table_args__ = (
        Index("ix_pipeline_runs_status", "status", "created_at"),
        Index("ix_pipeline_runs_symbol", "symbol", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(16))  # user | refresh | nightly | results
    force: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status: Mapped[PipelineStatus] = mapped_column(str_enum(PipelineStatus))
    steps: Mapped[list[Any]]
    version: Mapped[int] = mapped_column(Integer, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    report_as_of: Mapped[date | None]
    started_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


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
    # Evaluator memory between runs: last price seen and which side of the level it was on.
    state: Mapped[dict[str, Any] | None]


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


class ScreenerPreset(TimestampMixin, Base):
    """Saved screener filters (SPEC §9: "Filters are saved as presets")."""

    __tablename__ = "screener_presets"
    __upsert_key__ = ("name",)
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    filters: Mapped[dict[str, Any]]


class Notification(Base):
    """In-app notification (SPEC §8 alerts), optionally also delivered to Telegram."""

    __tablename__ = "notifications"
    __upsert_key__ = ("id",)
    __table_args__ = (Index("ix_notifications_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    symbol: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # alert type, or "test"
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    price: Mapped[float | None]
    read_at: Mapped[datetime | None]
    telegram: Mapped[str] = mapped_column(String(16))  # sent | failed | disabled
    telegram_error: Mapped[str | None] = mapped_column(Text)
