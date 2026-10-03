"""Enumerations stored as VARCHAR + CHECK constraint (non-native enums migrate more easily)."""

from enum import StrEnum


class StatementType(StrEnum):
    """Rule 5: consolidated first; standalone only as a flagged fallback."""

    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"


class CorporateActionType(StrEnum):
    SPLIT = "split"
    BONUS = "bonus"
    DIVIDEND = "dividend"
    RIGHTS = "rights"
    OTHER = "other"


class SurveillanceList(StrEnum):
    ASM_LT = "asm_lt"
    ASM_ST = "asm_st"
    GSM = "gsm"
    FNO_BAN = "fno_ban"


class Timeframe(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class Broker(StrEnum):
    FYERS = "fyers"
    KITE = "kite"


class AlertType(StrEnum):
    ENTERS_BUY_ZONE = "enters_buy_zone"
    CROSSES_FV = "crosses_fv"
    CROSSES_TOP_BAND = "crosses_top_band"
    CROSSES_INVALIDATION = "crosses_invalidation"
    PRICE_ABOVE = "price_above"  # needs ``threshold`` (₹)
    PRICE_BELOW = "price_below"  # needs ``threshold`` (₹)
    RESULTS_DATE = (
        "results_date"  # ``threshold`` = days before; default jobs.alerts.results_days_before
    )


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # another run held the lock, or outside the job's season


class BacktestStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PeriodType(StrEnum):
    """What a fin_line_items value covers."""

    QUARTER = "quarter"  # a quarter's flow (P&L)
    YEAR = "year"  # a fiscal year's flow (P&L, cash flow)
    INSTANT = "instant"  # a balance at period end (balance sheet)


class LineStatement(StrEnum):
    PL = "pl"
    BS = "bs"
    CF = "cf"
    RATIO = "ratio"


class FilingStatus(StrEnum):
    """A results filing (XBRL) in the ingestion ledger."""

    PENDING = "pending"  # listed by the exchange, not yet downloaded
    PARSED = "parsed"  # stored into fin_quarterly / fin_annual
    FAILED = "failed"  # download or parse failed (retried up to results_backfill.max_attempts)


class ReviewStatus(StrEnum):
    """A value read from an annual-report PDF (SPEC v0.2 §3.6 step 3)."""

    AUTO_ACCEPTED = "auto_accepted"  # confidence >= auto_accept: stored without review
    PENDING = "pending"  # low confidence: waits in the review queue
    ACCEPTED = "accepted"  # the owner accepted the value as read
    CORRECTED = "corrected"  # the owner entered the right value
    REJECTED = "rejected"  # the owner rejected it: not stored


class SymbolStatus(StrEnum):
    """A company in the symbol master (SPEC v0.2 §3.5)."""

    ACTIVE = "active"  # in today's NSE list or active on BSE
    INACTIVE = "inactive"  # was listed; missing from the latest masters (delisted, suspended)


class AliasKind(StrEnum):
    FORMER_SYMBOL = "former_symbol"  # NSE symbol change
    FORMER_NAME = "former_name"  # NSE name change
    BSE_SYMBOL = "bse_symbol"  # BSE's short symbol, when it differs from NSE's
    BSE_NAME = "bse_name"  # BSE's company name, when it differs from NSE's
    USER = "user"  # added by the owner


class PipelineStatus(StrEnum):
    """An on-demand pipeline run (SPEC §3.7)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"  # a report was stored (possibly with warnings from optional steps)
    FAILED = "failed"  # a required step failed: no new report


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    WARNING = "warning"  # finished, with something missing (listed in the report's data gaps)
    FAILED = "failed"
    SKIPPED = "skipped"


class EventKind(StrEnum):
    """A corporate event from an exchange feed (SPEC v0.2 §3.8, §10 ``events``)."""

    ANNOUNCEMENT = "announcement"
    BOARD_MEETING = "board_meeting"  # the calendar: a meeting date and its purpose
    RESULTS = "results"  # a financial-results filing (NSE results feed, BSE "Result")
    PLEDGE = "pledge"  # promoter pledge disclosure (SEBI SAST reg. 31)
    SAST = "sast"  # substantial acquisition disclosure (SAST reg. 29)
    INSIDER_TRADE = "insider_trade"  # PIT disclosure (SEBI PIT reg. 7)
    BULK_DEAL = "bulk_deal"
    BLOCK_DEAL = "block_deal"


class IssueStatus(StrEnum):
    """A cross-source reconciliation difference (SPEC v0.2 §3.9)."""

    OPEN = "open"  # the sources disagree: lowers valuation confidence, banner on the stock page
    RESOLVED = "resolved"  # a later check found them in agreement
    IGNORED = "ignored"  # the owner dismissed it (explained); not counted
