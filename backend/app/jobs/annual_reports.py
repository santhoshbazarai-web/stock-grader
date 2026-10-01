"""annual_reports: fill balance-sheet / cash-flow years the results XBRL lacks from annual-report
PDFs (SPEC v0.2 §3.6 step 3)."""

import logging
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from app.data.annual_report_store import fiscal_year_end, ingest_report
from app.data.raw_store import RawStoreError
from app.data.results_ingest import cache_raw
from app.db.enums import FilingStatus, LineStatement, PeriodType, StatementType
from app.db.models import AnnualReport, FinLineItem, Instrument
from app.db.upsert import upsert
from app.fundamentals.pdf_labels import get_pdf_labels
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.common import ensure_instruments, universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)
_PERIOD = {"bs": (LineStatement.BS, PeriodType.INSTANT), "cf": (LineStatement.CF, PeriodType.YEAR)}


def gap_years(ctx: JobContext, iid: int, years: list[int]) -> list[int]:
    """Fiscal years missing any ``annual_reports.required_items`` (from any source) for the
    company's basis: consolidated if it has consolidated line items, else standalone."""
    required = ctx.config.jobs.annual_reports.required_items
    ncfg = ctx.config.providers.nse.results
    session = ctx.session_factory()
    try:
        consolidated = session.scalar(
            select(func.count()).select_from(FinLineItem).where(
                FinLineItem.instrument_id == iid,
                FinLineItem.basis == StatementType.CONSOLIDATED,
            )
        )  # fmt: skip
        basis = StatementType.CONSOLIDATED if consolidated else StatementType.STANDALONE
        ends = {y: fiscal_year_end(session, iid, y, ncfg) for y in years}
        have = set(
            session.execute(
                select(FinLineItem.period_end, FinLineItem.statement, FinLineItem.item_code)
                .where(
                    FinLineItem.instrument_id == iid,
                    FinLineItem.basis == basis,
                    FinLineItem.period_end.in_(set(ends.values())),
                    FinLineItem.period_type.in_([PeriodType.INSTANT, PeriodType.YEAR]),
                )
                .distinct()
            ).all()
        )
    finally:
        session.close()
    return [
        y for y in years
        if any((ends[y], _PERIOD[s][0], code) not in have
               for s, codes in required.items() for code in codes)
    ]  # fmt: skip


def annual_reports(ctx: JobContext, options: JobOptions, *, limit: int | None = None) -> JobOutcome:
    """1. **Gaps.** For each symbol, the completed fiscal years (back to
       ``providers.history_years`` and ``first_fiscal_year``) missing a required balance-sheet
       or cash-flow item.
    2. **List.** Read the symbol's annual reports from NSE and add, for each gap year, its own
       report to the ``annual_reports`` ledger as pending, or else the next year's report (its
       comparative column covers the year).
    3. **Read.** Download pending reports (and failed ones below ``max_attempts``), newest year
       first, at most ``max_downloads_per_run``; cache each before reading it; values go to the
       review queue and, when confident, to fin_line_items (``app.data.annual_report_store``).
    """
    jcfg = ctx.config.jobs.annual_reports
    nse = ctx.config.providers.nse
    today = ctx.today()
    first = max(jcfg.first_fiscal_year, today.year - ctx.config.providers.history_years)
    symbols = universe(ctx, options)

    listed, index_failed, no_report = 0, [], {}
    for symbol in symbols:
        session = ctx.session_factory()
        try:
            iid = ensure_instruments(session, [symbol])[symbol]
            session.commit()
            years = [y for y in range(first, today.year + 1)
                     if fiscal_year_end(session, iid, y, nse.results) < today]  # fmt: skip
        finally:
            session.close()
        gaps = gap_years(ctx, iid, years)
        if not gaps:
            continue
        res = ctx.router.annual_reports(symbol)
        if res.data is None:
            index_failed.append(symbol)
            continue
        n, missing = _list_reports(ctx, iid, res.data, gaps)
        listed += n
        if missing:
            no_report[symbol] = missing

    session = ctx.session_factory()
    try:
        todo = session.execute(
            select(AnnualReport.id, Instrument.symbol)
            .join(Instrument, Instrument.id == AnnualReport.instrument_id)
            .where(
                Instrument.symbol.in_(symbols),
                AnnualReport.exchange == "nse",
                (AnnualReport.status == FilingStatus.PENDING)
                | ((AnnualReport.status == FilingStatus.FAILED)
                   & (AnnualReport.attempts < jcfg.max_attempts)),
            )
            .order_by(AnnualReport.fiscal_year.desc().nulls_last(), AnnualReport.id)
            .limit(limit or jcfg.max_downloads_per_run)
        ).all()  # fmt: skip
    finally:
        session.close()

    parsed, failed = 0, {}
    labels, xmap = get_pdf_labels(), get_xbrl_map()
    for report_id, symbol in todo:
        session = ctx.session_factory()
        try:
            row = session.get(AnnualReport, report_id)
            assert row is not None
            doc = ctx.router.annual_report_document(row.document, symbol)
            if doc.data is None:
                row.attempts += 1
                row.status, row.error = FilingStatus.FAILED, "; ".join(doc.reasons)[:2000]
            else:
                try:  # cache the raw document before reading it (SPEC §3.2a)
                    row.raw_path = cache_raw(ctx.raw_store, "nse", row.document, doc.data)
                except RawStoreError as exc:
                    row.attempts += 1
                    row.status, row.error = FilingStatus.FAILED, str(exc)[:2000]
                else:
                    ingest_report(session, row, content=doc.data, nse=nse, labels=labels,
                                  xmap=xmap, now=ctx.now())  # fmt: skip
            if row.status is FilingStatus.PARSED:
                parsed += 1
            else:
                failed[row.document] = row.error or "failed"
            session.commit()
        finally:
            session.close()

    details: dict[str, Any] = {
        "listed_new": listed,
        "downloaded": len(todo),
        "parsed": parsed,
        "failed": dict(list(failed.items())[:50]),
        "index_failed": index_failed,
        "gap_years_without_report": no_report,
    }
    return JobOutcome(parsed, details)


def _list_reports(
    ctx: JobContext, iid: int, listing: pd.DataFrame, gaps: list[int]
) -> tuple[int, list[int]]:
    """Add the reports covering ``gaps`` to the ledger. Returns (new rows, gap years with no
    report listed)."""
    by_year: dict[int, dict[str, Any]] = {}
    for rec in listing.to_dict("records"):
        year = rec.get("fiscal_year")
        if year is None or pd.isna(year):
            continue
        when = rec.get("disseminated_at")
        prev = by_year.get(int(year))
        # several documents for a year (a corrigendum, a re-upload): the latest
        if prev is None or (when is not None and (prev["disseminated_at"] is None
                                                  or when > prev["disseminated_at"])):  # fmt: skip
            by_year[int(year)] = {"url": rec["url"], "disseminated_at": _plain_ts(when)}
    rows, missing = [], []
    for year in gaps:
        pick = by_year.get(year) or by_year.get(year + 1)
        if pick is None:
            missing.append(year)
            continue
        rows.append({
            "instrument_id": iid, "exchange": "nse", "document": pick["url"],
            "fiscal_year": year if year in by_year else year + 1,
            "disseminated_at": pick["disseminated_at"], "status": FilingStatus.PENDING,
        })  # fmt: skip
    session = ctx.session_factory()
    try:
        known = set(session.scalars(
            select(AnnualReport.document).where(AnnualReport.instrument_id == iid)))  # fmt: skip
        new = [r for r in {r["document"]: r for r in rows}.values() if r["document"] not in known]
        if new:
            upsert(session, AnnualReport, new, update=[])
            session.commit()
    finally:
        session.close()
    return len(new), missing


def _plain_ts(value: Any) -> datetime | None:
    if value is None or (not isinstance(value, datetime) and pd.isna(value)):
        return None
    return pd.Timestamp(value).to_pydatetime()
