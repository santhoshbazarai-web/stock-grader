"""One results filing → fin_quarterly / fin_annual, recorded in the ``result_filings`` ledger.

Shared by the ``results_backfill`` job (documents listed and downloaded from NSE) and
``POST /api/uploads/xbrl`` (a document from NSE's or BSE's website, uploaded by hand).
"""

import hashlib
from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Dataset, NseResultsConfig
from app.data.gaps import GapRecord, GapRecorder
from app.data.raw_store import RawStore
from app.data.results_store import EXCHANGE_SOURCE, store_filing
from app.data.xbrl import XbrlFormatError, announcement_date, parse_results
from app.db.enums import FilingStatus
from app.db.models import DataGap, ResultFiling
from app.db.upsert import upsert
from app.fundamentals.xbrl_map import get_xbrl_map

# data_gaps.field of the gap left by results_backfill's fallback (a quarter stored from yfinance
# while the exchange filing was unavailable); resolved once a filing is stored.
FALLBACK_GAP_FIELD = "results_filing"


def cache_raw(store: RawStore | None, source: str, name: str, content: bytes) -> str | None:
    """Cache the raw bytes before parsing (SPEC §3.2a); the path relative to the cache root.
    Raises :class:`RawStoreError` when the cache is not writable: nothing is parsed then."""
    if store is None:
        return None
    return store.relative(store.save(source, name, content))


def upload_document_id(content: bytes) -> str:
    return f"upload:{hashlib.sha256(content).hexdigest()}"


def ingest(
    session: Session,
    filing_row: ResultFiling,
    *,
    symbol: str,
    content: bytes,
    cfg: NseResultsConfig,
    gaps: GapRecorder,
    now: datetime,
) -> None:
    """Parse ``content`` and store it; the ledger row records the outcome either way (the
    caller commits). A document for another company, or one whose basis (standalone /
    consolidated) nobody states, is refused: rule 5 needs the basis."""
    filing_row.attempts += 1
    try:
        filing = parse_results(
            content, cfg, period_start=filing_row.period_start, period_end=filing_row.period_end
        )
    except XbrlFormatError as exc:
        _fail(filing_row, str(exc))
        return
    if filing.symbol and filing.symbol.strip().upper() != symbol.upper():
        _fail(filing_row, f"document is for {filing.symbol}, not {symbol}")
        return
    basis = filing.statement_type or filing_row.statement_type
    if basis is None:
        _fail(filing_row, "neither the document nor the exchange listing says standalone or "
                          "consolidated")  # fmt: skip
        return
    warnings = list(filing.warnings)
    if filing_row.statement_type not in (None, filing.statement_type) and filing.statement_type:
        warnings.append(
            f"exchange listing says {filing_row.statement_type}, document says "
            f"{filing.statement_type}; using the document"
        )
    announced = announcement_date(filing_row.disseminated_at, filing.board_meeting,
                                  cfg.available_after_ist)  # fmt: skip
    periods = store_filing(
        session,
        instrument_id=filing_row.instrument_id,
        filing=filing,
        filing_row=filing_row,
        statement_type=basis,
        announcement=announced,
        fetched_at=now,
        cfg=cfg,
    )
    filing_row.status = FilingStatus.PARSED
    filing_row.error = None
    filing_row.periods = periods
    filing_row.warnings = warnings or None
    filing_row.parsed_at = now
    filing_row.period_start = filing.period_start
    filing_row.period_end = filing.period_end
    filing_row.statement_type = basis
    filing_row.audited = filing.audited if filing.audited is not None else filing_row.audited
    filing_row.is_bank = filing.is_bank
    filing_row.announcement_date = announced

    if announced is None:
        gaps.record(GapRecord(Dataset.FIN_QUARTERLY, symbol, "results filing without a "
                              "dissemination or board-meeting date", [EXCHANGE_SOURCE],
                              "announcement_date"))  # fmt: skip
    if filing.annual is not None:
        for name in get_xbrl_map().unmapped("fin_annual"):
            gaps.record(GapRecord(Dataset.FIN_ANNUAL, symbol, "not in exchange results filings "
                                  "(XBRL); upload a Screener export to fill it",
                                  [EXCHANGE_SOURCE], name))  # fmt: skip
    session.execute(
        update(DataGap)
        .where(
            DataGap.instrument_id == filing_row.instrument_id,
            DataGap.field.in_([FALLBACK_GAP_FIELD, "screener_refresh"]),
            DataGap.resolved_at.is_(None),
        )
        .values(resolved_at=func.now())
    )


def _fail(filing_row: ResultFiling, error: str) -> None:
    filing_row.status = FilingStatus.FAILED
    filing_row.error = error[:2000]


def ensure_uploaded_row(session: Session, *, instrument_id: int, content: bytes) -> ResultFiling:
    """The ledger row for an uploaded document (one per identical file)."""
    document = upload_document_id(content)
    upsert(
        session,
        ResultFiling,
        [{"instrument_id": instrument_id, "exchange": "upload", "document": document,
          "status": FilingStatus.PENDING}],
        update=[],
    )  # fmt: skip
    row = session.scalar(
        select(ResultFiling).where(
            ResultFiling.instrument_id == instrument_id, ResultFiling.document == document
        )
    )
    assert row is not None
    return row


def cutoff(today: date, years: int) -> date:
    """Oldest period end worth fetching (``providers.history_years``)."""
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - years, day=28)
