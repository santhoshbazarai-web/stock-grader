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
