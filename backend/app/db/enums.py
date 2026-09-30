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
    FAILED = "failed"  # download or parse failed (retried up to results_watch.max_attempts)


class ReviewStatus(StrEnum):
    """A value read from an annual-report PDF (SPEC v0.2 §3.6 step 3)."""

    AUTO_ACCEPTED = "auto_accepted"  # confidence >= auto_accept: stored without review
    PENDING = "pending"  # low confidence: waits in the review queue
    ACCEPTED = "accepted"  # the owner accepted the value as read
    CORRECTED = "corrected"  # the owner entered the right value
    REJECTED = "rejected"  # the owner rejected it: not stored
