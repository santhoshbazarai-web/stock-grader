"""Filing jobs: shareholding, results_backfill (exchange results XBRL)."""

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from app.core.config import Dataset, Provider, Season
from app.data.canonical import fields_for
from app.data.gaps import GapRecord
from app.data.raw_store import RawStoreError
from app.data.results_ingest import FALLBACK_GAP_FIELD, cache_raw, cutoff, ingest
from app.data.results_store import OFFLINE_SOURCE
from app.data.xbrl import parser_version
from app.db.enums import FilingStatus, StatementType
from app.db.models import FinQuarterly, Instrument, ResultFiling, Shareholding
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
    failed_reasons: dict[str, str] = {}
    for symbol in universe(ctx, options):
        res = ctx.router.shareholding(symbol)
        if res.data is None or res.source is None or res.data.empty:
            failed.append(symbol)
            failed_reasons[symbol] = "; ".join(res.reasons)[:500] or "no rows"
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
    return JobOutcome(written, {"failed": failed, "failed_reasons": failed_reasons})


def results_backfill(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Exchange results filings (XBRL) → fin_quarterly / fin_annual (replaces Screener uploads).

    1. **List.** Read each symbol's filing list from NSE and add unseen documents to the
       ``result_filings`` ledger as pending. In results season (or with --force / --symbols)
       every symbol is re-read on every run; outside it, each at most every
       ``index_recheck_days``.
    2. **Download.** Fetch pending documents (and failed ones below ``max_attempts``), newest
       period first, at most ``max_downloads_per_run`` per run, so a 10-year backfill of the
       universe spreads over several nights. Each is parsed and stored with its real
       announcement date (see ``app.data.results_store`` for how it merges with other sources).
    3. **Fallback.** In season, a symbol whose NSE list cannot be read gets new quarters from
       the ``fin_quarterly`` providers (yfinance) with ``announcement_date`` = the day first
       seen — never earlier than the real one — and a data gap until its filing is stored.
    """
    jcfg = ctx.config.jobs.results_backfill
    today = ctx.today()
    in_season = ctx.config.jobs.results_season.contains(today.month, today.day)
    recheck_all = in_season or options.force or bool(options.symbols)
    symbols = universe(ctx, options)
    listing = list_results(ctx, symbols, recheck_all=recheck_all,
                           fallback=in_season or options.force)  # fmt: skip
    downloads = download_results(ctx, symbols, limit=jcfg.max_downloads_per_run)
    session = ctx.session_factory()
    try:
        pending_left = session.scalar(
            select(func.count())
            .select_from(ResultFiling)
            .where(ResultFiling.status == FilingStatus.PENDING)
        )
    finally:
        session.close()
    details: dict[str, Any] = {**listing, **downloads, "pending_left": pending_left}
    details.pop("fallback_written")
    return JobOutcome(downloads["parsed"] + listing["fallback_written"], details)


def list_results(
    ctx: JobContext, symbols: list[str], *, recheck_all: bool, fallback: bool
) -> dict[str, Any]:
    """Step 1 of results_backfill: read each symbol's NSE filing list into the ledger (outside
    ``recheck_all``, a symbol at most every ``index_recheck_days``). With ``fallback``, a symbol
    whose list can't be read gets new quarters from yfinance, flagged as a data gap."""
    jcfg = ctx.config.jobs.results_backfill
    today = ctx.today()
    oldest = cutoff(today, ctx.config.providers.history_years)
    listed, index_failed, fallback_written, flagged = 0, [], 0, []
    index_reasons: dict[str, str] = {}
    for symbol in symbols:
        key = f"results-index:{symbol}"
        if not recheck_all and ctx.redis.exists(key):
            continue
        res = ctx.router.results_filings(symbol)
        if res.data is None:
            index_failed.append(symbol)
            index_reasons[symbol] = "; ".join(res.reasons)[:500]
            if fallback:
                n = _fallback_quarters(ctx, symbol, today)
                fallback_written += n
                if n:
                    flagged.append(symbol)
            continue
        listed += _list_filings(ctx, symbol, res.data, oldest, res.source)
        ctx.redis.set(key, 1, ex=jcfg.index_recheck_days * 86400)
    return {"listed_new": listed, "index_failed": index_failed, "index_reasons": index_reasons,
            "fallback_flagged": flagged, "fallback_written": fallback_written}  # fmt: skip


def download_results(ctx: JobContext, symbols: list[str], *, limit: int) -> dict[str, Any]:
    """Step 2 of results_backfill: download and store pending documents (and ones whose
    download failed, below ``max_attempts``), newest period first, at most ``limit``.

    A document that downloaded but did not parse is recorded with the parser version
    (``parse_failed_version``) and is neither downloaded nor parsed again until that version
    changes (a new xbrl_map.yaml version or parser revision); then it is re-parsed from the
    raw cache, downloaded only if the cache is missing."""
    jcfg, ncfg = ctx.config.jobs.results_backfill, ctx.config.providers.nse.results
    current = parser_version()
    downloaded, parsed, reparsed, failed = 0, 0, 0, {}
    mine = (Instrument.symbol.in_(symbols)) & (ResultFiling.exchange.in_(("nse", OFFLINE_SOURCE)))
    failed_download = (
        (ResultFiling.status == FilingStatus.FAILED)
        & ResultFiling.parse_failed_version.is_(None)
        & (ResultFiling.attempts < jcfg.max_attempts)
    )
    parser_changed = (
        (ResultFiling.status == FilingStatus.FAILED)
        & ResultFiling.parse_failed_version.is_not(None)
        & (ResultFiling.parse_failed_version != current)
    )
    session = ctx.session_factory()
    try:
        todo = session.execute(
            select(ResultFiling.id, Instrument.symbol)
            .join(Instrument, Instrument.id == ResultFiling.instrument_id)
            .where(mine, (ResultFiling.status == FilingStatus.PENDING) | failed_download
                   | parser_changed)
            .order_by(
                ResultFiling.period_end.desc().nulls_last(),
                ResultFiling.disseminated_at.desc().nulls_last(),
            )
            .limit(limit)
        ).all()  # fmt: skip
        known_failures = session.scalar(
            select(func.count()).select_from(ResultFiling)
            .join(Instrument, Instrument.id == ResultFiling.instrument_id)
            .where(mine, ResultFiling.status == FilingStatus.FAILED,
                   ResultFiling.parse_failed_version == current)
        ) or 0  # fmt: skip
    finally:
        session.close()
    for filing_id, symbol in todo:
        session = ctx.session_factory()
        try:
            row = session.get(ResultFiling, filing_id)
            assert row is not None
            content = _cached(ctx, row) if row.parse_failed_version else None
            if content is not None:
                reparsed += 1
            else:
                doc = ctx.router.results_document(row.document, symbol)
                downloaded += 1
                if doc.data is None:
                    row.attempts += 1
                    row.status = FilingStatus.FAILED
                    row.error = "; ".join(doc.reasons)[:2000]
                    row.parse_failed_version = None
                else:
                    try:  # cache the raw document before parsing it (SPEC §3.2a)
                        row.raw_path = cache_raw(ctx.raw_store, "nse", row.document, doc.data)
                        content = doc.data
                    except RawStoreError as exc:
                        row.attempts += 1
                        row.status = FilingStatus.FAILED
                        row.error = str(exc)[:2000]
                        row.parse_failed_version = None
            if content is not None:
                ingest(session, row, symbol=symbol, content=content, cfg=ncfg,
                       gaps=ctx.gaps, now=ctx.now())  # fmt: skip
            if row.status is FilingStatus.PARSED:
                parsed += 1
            else:
                failed[row.document] = row.error or "failed"
            session.commit()
        finally:
            session.close()
    return {"downloaded": downloaded, "reparsed": reparsed, "parsed": parsed,
            "failed": dict(list(failed.items())[:50]),
            "known_parse_failures": known_failures, "parser_version": current}  # fmt: skip


def _cached(ctx: JobContext, row: ResultFiling) -> bytes | None:
    if ctx.raw_store is None or not row.raw_path:
        return None
    try:
        return ctx.raw_store.read(row.raw_path)
    except (OSError, RawStoreError):
        return None


def _list_filings(
    ctx: JobContext, symbol: str, listing: pd.DataFrame, oldest: date, source: Provider | None
) -> int:
    """Add unseen documents to the ledger as pending. Returns how many were new. Filings from
    the development offline exchange are ledgered as such, so their figures never pass for
    NSE-filed ones."""
    exchange = OFFLINE_SOURCE if source is Provider.OFFLINE else "nse"
    session = ctx.session_factory()
    try:
        iid = ensure_instruments(session, [symbol])[symbol]
        known = set(
            session.scalars(select(ResultFiling.document).where(ResultFiling.instrument_id == iid))
        )
        rows = []
        for raw in listing.to_dict("records"):
            rec = {k: _plain(v) for k, v in raw.items()}
            for k in ("period_start", "period_end"):
                if isinstance(rec[k], datetime):
                    rec[k] = rec[k].date()
            too_old = rec["period_end"] is not None and rec["period_end"] < oldest
            if rec["url"] in known or too_old:
                continue
            rows.append(
                {
                    "instrument_id": iid,
                    "exchange": exchange,
                    "document": rec["url"],
                    "period_start": rec["period_start"],
                    "period_end": rec["period_end"],
                    "statement_type": rec["statement_type"],
                    "audited": rec["audited"],
                    "is_bank": rec["is_bank"],
                    "disseminated_at": rec["disseminated_at"],
                    "status": FilingStatus.PENDING,
                }
            )
        written = upsert(session, ResultFiling, rows, update=[]) if rows else 0
        session.commit()
        return written
    finally:
        session.close()


def _plain(value: Any) -> Any:
    """pandas cell → plain Python (NaT/NaN → None, Timestamp → datetime)."""
    if value is None or (not isinstance(value, str | bool) and pd.isna(value)):
        return None
    return value.to_pydatetime() if isinstance(value, pd.Timestamp) else value


def _fallback_quarters(ctx: JobContext, symbol: str, today: date) -> int:
    """Quarters newer than anything stored, from the fin_quarterly providers (yfinance), saved
    with non-empty columns only and ``announcement_date`` = the day first seen."""
    res = ctx.router.fin_quarterly(symbol)
    if res.data is None or res.source is None or res.source is Provider.SCREENER:
        return 0
    if res.data.empty:
        return 0
    session = ctx.session_factory()
    try:
        iid = ensure_instruments(session, [symbol])[symbol]
        latest = session.scalar(
            select(func.max(FinQuarterly.period_end)).where(FinQuarterly.instrument_id == iid)
        )
        df = _with_period(res.data)
        new = df[df["period_end"] > latest] if latest else df
        if new.empty:
            return 0
        new = new.copy()
        if "announcement_date" not in new or new["announcement_date"].isna().all():
            new["announcement_date"] = today  # first-seen date (see docstring)
        new["statement_type"] = res.data.attrs.get(
            "statement_type", StatementType.CONSOLIDATED.value
        )
        cols = non_empty_columns(new, fields_for("fin_quarterly"))
        base = ["period_end", "announcement_date", "statement_type"]
        rows = [
            {**r, "instrument_id": iid, "source": res.source.value, "fetched_at": res.fetched_at}
            for r in frame_records(new, [*base, *cols])
        ]
        written = upsert(session, FinQuarterly, rows)
        periods = [p.isoformat() for p in new["period_end"]]
        ctx.gaps.record(
            GapRecord(
                Dataset.FIN_QUARTERLY,
                symbol,
                f"exchange results filings unavailable; quarter(s) {', '.join(periods)} stored "
                f"from {res.source.value} until the XBRL filing is ingested",
                [Provider.NSE.value, res.source.value],
                field=FALLBACK_GAP_FIELD,
            )
        )
        session.commit()
        return written
    finally:
        session.close()
