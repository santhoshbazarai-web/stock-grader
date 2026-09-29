"""Filing-season jobs: shareholding, results_watch."""

import logging
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from app.core.config import Dataset, Provider, Season
from app.data.canonical import fields_for
from app.data.gaps import GapRecord
from app.db.enums import StatementType
from app.db.models import FinQuarterly, Shareholding
from app.db.upsert import upsert
from app.jobs.common import ensure_instruments, frame_records, non_empty_columns, universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)


def _outside(ctx: JobContext, options: JobOptions, season: Season) -> str | None:
    today = ctx.today()
    if options.force or options.symbols or season.contains(today.month, today.day):
        return None
    return f"outside season ({today.isoformat()})"


def _with_period(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["period_end"] = [pd.Timestamp(i).date() for i in out.index]
    return out


def shareholding(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Fetch the latest shareholding filings. Only columns the source actually supplies are
    written, so NSE's promoter/public split never blanks FII/DII values from a Screener upload."""
    if reason := _outside(ctx, options, ctx.config.jobs.shareholding_season):
        return JobOutcome(skipped_reason=reason)
    written, failed = 0, []
    for symbol in universe(ctx, options):
        res = ctx.router.shareholding(symbol)
        if res.data is None or res.source is None or res.data.empty:
            failed.append(symbol)
            continue
        session = ctx.session_factory()
        try:
            iid = ensure_instruments(session, [symbol])[symbol]
            df = _with_period(res.data)
            cols = non_empty_columns(df, [*fields_for("shareholding"), "filing_date"])
            rows = [
                {
                    **r,
                    "instrument_id": iid,
                    "source": res.source.value,
                    "fetched_at": res.fetched_at,
                }
                for r in frame_records(df, ["period_end", *cols])
            ]
            written += upsert(session, Shareholding, rows)
            session.commit()
        finally:
            session.close()
    return JobOutcome(written, {"failed": failed})


def results_watch(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Spot quarters newer than anything stored. New quarters from a fallback source are saved
    (non-empty columns only) with ``announcement_date`` = the day first seen — never earlier
    than the real announcement, so backtests cannot look ahead — and flagged with a data gap
    asking for a fresh Screener export."""
    if reason := _outside(ctx, options, ctx.config.jobs.results_season):
        return JobOutcome(skipped_reason=reason)
    today = ctx.today()
    written, failed, flagged = 0, [], []
    for symbol in universe(ctx, options):
        res = ctx.router.fin_quarterly(symbol)
        if res.data is None or res.source is None:
            failed.append(symbol)
            continue
        if res.source is Provider.SCREENER or res.data.empty:
            continue  # nothing newer than the upload itself
        session = ctx.session_factory()
        try:
            iid = ensure_instruments(session, [symbol])[symbol]
            latest = session.scalar(
                select(func.max(FinQuarterly.period_end)).where(FinQuarterly.instrument_id == iid)
            )
            df = _with_period(res.data)
            new = df[df["period_end"] > latest] if latest else df
            if new.empty:
                continue
            new = new.copy()
            if "announcement_date" not in new or new["announcement_date"].isna().all():
                new["announcement_date"] = today  # first-seen date (see docstring)
            new["statement_type"] = res.data.attrs.get(
                "statement_type", StatementType.CONSOLIDATED.value
            )
            cols = non_empty_columns(new, fields_for("fin_quarterly"))
            base = ["period_end", "announcement_date", "statement_type"]
            rows = [
                {
                    **r,
                    "instrument_id": iid,
                    "source": res.source.value,
                    "fetched_at": res.fetched_at,
                }
                for r in frame_records(new, [*base, *cols])
            ]
            written += upsert(session, FinQuarterly, rows)
            periods = [p.isoformat() for p in new["period_end"]]
            ctx.gaps.record(
                GapRecord(
                    Dataset.FIN_QUARTERLY,
                    symbol,
                    f"new quarter(s) {', '.join(periods)} seen via {res.source.value}; "
                    "upload a fresh Screener export",
                    [res.source.value],
                    field="screener_refresh",
                )
            )
            flagged.append(symbol)
            session.commit()
        finally:
            session.close()
    details: dict[str, Any] = {"flagged": flagged, "failed": failed}
    return JobOutcome(written, details)
