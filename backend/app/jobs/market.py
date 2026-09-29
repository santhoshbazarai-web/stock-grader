"""Market-data jobs: corporate_actions, eod_prices, nse_bhavcopy, index_constituents."""

import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Dataset
from app.data.gaps import GapRecord
from app.db.enums import CorporateActionType
from app.db.models import (
    CorporateAction,
    DeliveryDaily,
    IndexMembership,
    Instrument,
    PriceDaily,
    SurveillanceFlag,
)
from app.db.upsert import upsert
from app.jobs.common import (
    count_by,
    ensure_instruments,
    frame_records,
    instrument_ids,
    last_price_date,
    py,
    readjust,
    universe,
)
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)

OHLCV = ["open", "high", "low", "close", "volume"]
CA_FIELDS = [
    "ex_date",
    "action_type",
    "ratio_old",
    "ratio_new",
    "dividend_per_share",
    "record_date",
    "description",
]
ADJUSTING = {CorporateActionType.SPLIT.value, CorporateActionType.BONUS.value}


def _years_ago(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - years, day=28)


# ───────────────────────── corporate actions ─────────────────────────


def _fetch_corporate_actions(
    ctx: JobContext, session: Session, symbol: str, instrument_id: int, start: date
) -> tuple[int, bool] | None:
    """Upsert actions since ``start``; → (rows, any split/bonus) or None if unavailable."""
    res = ctx.router.corporate_actions(symbol, start, ctx.today())
    if res.data is None or res.source is None:
        return None
    df = res.data
    if df.empty:
        return 0, False
    rows = [
        {
            **r,
            "instrument_id": instrument_id,
            "source": res.source.value,
            "fetched_at": res.fetched_at,
        }
        for r in frame_records(df, CA_FIELDS)
    ]
    upsert(session, CorporateAction, rows)
    return len(rows), bool(set(df["action_type"]) & ADJUSTING)


def corporate_actions(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Refresh corporate actions (last ``lookback_days``, or full history with --full) and
    re-adjust prices of symbols with a split/bonus."""
    today = ctx.today()
    full_start = _years_ago(today, ctx.config.providers.nse.corporate_actions_from_years)
    start = (
        full_start
        if options.full
        else today - timedelta(days=ctx.config.jobs.corporate_actions.lookback_days)
    )
    symbols = universe(ctx, options)
    written, failed, readjusted = 0, [], []
    for symbol in symbols:
        session = ctx.session_factory()
        try:
            iid = ensure_instruments(session, [symbol])[symbol]
            got = _fetch_corporate_actions(ctx, session, symbol, iid, start)
            if got is None:
                failed.append(symbol)
                continue
            n, adjusting = got
            written += n
            if adjusting:
                _readjust_and_flag(ctx, session, symbol, iid)
                readjusted.append(symbol)
            session.commit()
        finally:
            session.close()
    return JobOutcome(
        written,
        {
            "symbols": len(symbols),
            "failed": failed,
            "readjusted": readjusted,
            "from": start.isoformat(),
        },
    )


def _readjust_and_flag(ctx: JobContext, session: Session, symbol: str, iid: int) -> None:
    result = readjust(session, iid)
    if not result.complete:
        ctx.gaps.record(
            GapRecord(
                Dataset.CORPORATE_ACTIONS,
                symbol,
                "; ".join(result.warnings),
                [],
                field="adj_close",
            )
        )


# ───────────────────────── EOD prices ─────────────────────────


def _store_bars(session: Session, iid: int, df: pd.DataFrame, source: str, fetched_at: Any) -> int:
    if df.empty:
        return 0
    bars = df[OHLCV].copy()
    bars["date"] = [d.date() for d in pd.DatetimeIndex(bars.index)]
    rows = [
        {**r, "instrument_id": iid, "source": source, "fetched_at": fetched_at}
        for r in frame_records(bars, ["date", *OHLCV])
    ]
    return upsert(session, PriceDaily, rows)  # raw columns only; adj_* left to readjust()


def eod_prices(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Fetch new daily bars for the universe and benchmark indices, then recompute split/bonus
    adjustment. New symbols (or --full) get ``history_years`` of bars and their full
    corporate-action history first, so the first adjustment is complete."""
    today = ctx.today()
    history_start = _years_ago(today, ctx.config.providers.history_years)
    overlap = timedelta(days=ctx.config.jobs.eod_prices.overlap_days)
    ca_start = _years_ago(today, ctx.config.providers.nse.corporate_actions_from_years)
    written, failed, stale, sources = 0, [], [], []

    def one(symbol: str, iid: int, is_index: bool, session: Session) -> None:
        nonlocal written
        last = last_price_date(session, iid)
        backfill = options.full or last is None
        start = history_start if backfill or last is None else max(history_start, last - overlap)
        fetch = ctx.router.index_ohlcv if is_index else ctx.router.daily_ohlcv
        res = fetch(symbol, start, today)
        if res.data is None or res.source is None:
            failed.append(symbol)
            return
        if res.stale:
            stale.append(symbol)
        sources.append(res.source.value)
        if backfill and not is_index:
            _fetch_corporate_actions(ctx, session, symbol, iid, ca_start)
        written += _store_bars(session, iid, res.data, res.source.value, res.fetched_at)
        _readjust_and_flag(ctx, session, symbol, iid)

    symbols = universe(ctx, options)
    indices = [] if options.symbols else list(ctx.config.jobs.benchmark_indices)
    for items, is_index in ((symbols, False), (indices, True)):
        for symbol in items:
            session = ctx.session_factory()
            try:
                iid = ensure_instruments(session, [symbol], is_index=is_index)[symbol]
                one(symbol, iid, is_index, session)
                session.commit()  # per symbol: progress survives a later failure
            finally:
                session.close()
    return JobOutcome(
        written,
        {
            "symbols": len(symbols),
            "indices": len(indices),
            "failed": failed,
            "stale": stale,
            "sources": dict(count_by(sources)),
        },
    )


# ───────────────────────── point-in-time listings ─────────────────────────


def _apply_listing(
    session: Session,
    *,
    current: dict[int, dict[str, Any]],
    open_rows: dict[int, Any],
    today: date,
    insert_row: Callable[[int, dict[str, Any]], None],
    close_row: Callable[[Any], None],
    refresh_row: Callable[[Any, dict[str, Any]], None],
) -> tuple[int, int]:
    """Diff today's listing against open rows: open new ones from today, close ones that
    disappeared as of today, refresh attributes of the rest. → (opened, closed)."""
    opened = closed = 0
    for iid, attrs in current.items():
        if iid in open_rows:
            refresh_row(open_rows[iid], attrs)
        else:
            insert_row(iid, attrs)
            opened += 1
    for iid, row in open_rows.items():
        if iid not in current:
            close_row(row)
            closed += 1
    return opened, closed


def _sync_surveillance(
    ctx: JobContext, session: Session, df: pd.DataFrame, source: str, fetched_at: Any, today: date
) -> dict[str, Any]:
    ids = instrument_ids(session, set(df["symbol"]))
    stats: dict[str, Any] = {"unknown_symbols": sorted(set(df["symbol"]) - set(ids))}
    for list_name, group in df.groupby("list_name"):
        current: dict[int, dict[str, Any]] = {
            ids[str(r["symbol"])]: {str(k): v for k, v in r.items()}
            for r in group.to_dict("records")
            if r["symbol"] in ids
        }
        open_rows = {
            f.instrument_id: f
            for f in session.scalars(
                select(SurveillanceFlag).where(
                    SurveillanceFlag.list_name == list_name, SurveillanceFlag.effective_to.is_(None)
                )
            )
        }

        def insert_row(iid: int, attrs: dict[str, Any], ln: Any = list_name) -> None:
            upsert(
                session,
                SurveillanceFlag,
                [
                    {
                        "instrument_id": iid,
                        "list_name": ln,
                        "stage": py(attrs.get("stage")),
                        "effective_from": today,
                        "effective_to": None,
                        "source": source,
                        "fetched_at": fetched_at,
                    }
                ],
            )

        def close_row(row: SurveillanceFlag) -> None:
            row.effective_to = today

        def refresh_row(row: SurveillanceFlag, attrs: dict[str, Any]) -> None:
            row.stage = py(attrs.get("stage"))
            row.fetched_at = fetched_at

        stats[str(list_name)] = _apply_listing(
            session,
            current=current,
            open_rows=open_rows,
            today=today,
            insert_row=insert_row,
            close_row=close_row,
            refresh_row=refresh_row,
        )
    # lists absent from today's data entirely: everything on them was removed
    for f in session.scalars(
        select(SurveillanceFlag).where(
            SurveillanceFlag.effective_to.is_(None),
            SurveillanceFlag.list_name.not_in(set(df["list_name"])),
        )
    ):
        f.effective_to = today
    return stats


def nse_bhavcopy(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Delivery % for the day's bhavcopy, plus current ASM/GSM/F&O-ban lists."""
    day = options.day or ctx.today()
    details: dict[str, Any] = {"day": day.isoformat()}
    written = 0
    session = ctx.session_factory()
    try:
        res = ctx.router.delivery(day)
        if res.data is None or res.source is None:
            raise RuntimeError(f"no delivery data for {day}: {res.reasons}")
        df = res.data.drop_duplicates("symbol", keep="first")
        if options.symbols:
            df = df[df["symbol"].isin({s.upper() for s in options.symbols})]
        ids = instrument_ids(session, set(df["symbol"]))
        known = df[df["symbol"].isin(ids)].copy()
        known["instrument_id"] = known["symbol"].map(ids)
        cols = [
            "instrument_id",
            "date",
            "traded_qty",
            "deliverable_qty",
            "delivery_pct",
            "traded_value_cr",
        ]
        rows = [
            {**r, "source": res.source.value, "fetched_at": res.fetched_at}
            for r in frame_records(known, cols)
        ]
        written += upsert(session, DeliveryDaily, rows)
        details.update(
            delivery_rows=len(rows),
            unknown_symbols=len(df) - len(known),
            warnings=res.data.attrs.get("warnings", []),
        )

        surv = ctx.router.surveillance()
        if surv.data is None or surv.source is None:
            raise RuntimeError(f"no surveillance data: {surv.reasons}")
        details["surveillance"] = _sync_surveillance(
            ctx, session, surv.data, surv.source.value, surv.fetched_at, ctx.today()
        )
        written += len(surv.data)
        session.commit()
    finally:
        session.close()
    return JobOutcome(written, details)


# ───────────────────────── index constituents ─────────────────────────


def _sync_index(
    session: Session, index: str, df: pd.DataFrame, source: str, fetched_at: Any, today: date
) -> tuple[int, dict[str, Any]]:
    """Upsert the index's instruments and diff its open memberships. → (rows, stats)."""
    isin_owner = dict(
        session.execute(
            select(Instrument.isin, Instrument.symbol).where(
                Instrument.isin.in_(set(df["isin"].dropna()))
            )
        ).all()
    )
    renamed: list[str] = []
    rows: list[dict[str, Any]] = []
    for r in df.to_dict("records"):
        isin = r["isin"] or None
        if isin and isin_owner.get(isin) not in (None, r["symbol"]):
            renamed.append(f"{isin_owner[isin]} -> {r['symbol']}")
            isin = None  # symbol changed: the old row keeps the ISIN; flagged for review
        rows.append(
            {
                "symbol": r["symbol"],
                "name": r["name"],
                "industry": r["industry"],
                "series": r["series"],
                "isin": isin,
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    written = upsert(session, Instrument, rows)
    ids = instrument_ids(session, set(df["symbol"]))
    current: dict[int, dict[str, Any]] = {ids[s]: {} for s in df["symbol"] if s in ids}
    open_rows = {
        m.instrument_id: m
        for m in session.scalars(
            select(IndexMembership).where(
                IndexMembership.index_name == index, IndexMembership.effective_to.is_(None)
            )
        )
    }

    def insert_row(iid: int, _attrs: dict[str, Any]) -> None:
        upsert(
            session,
            IndexMembership,
            [
                {
                    "index_name": index,
                    "instrument_id": iid,
                    "effective_from": today,
                    "effective_to": None,
                    "weight_pct": None,
                    "source": source,
                    "fetched_at": fetched_at,
                }
            ],
        )

    def close_row(row: IndexMembership) -> None:
        row.effective_to = today

    def refresh_row(row: IndexMembership, _attrs: dict[str, Any]) -> None:
        row.fetched_at = fetched_at

    opened, closed = _apply_listing(
        session,
        current=current,
        open_rows=open_rows,
        today=today,
        insert_row=insert_row,
        close_row=close_row,
        refresh_row=refresh_row,
    )
    stats = {"members": len(current), "joined": opened, "left": closed, "possible_renames": renamed}
    return written, stats


def index_constituents(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Refresh instruments and point-in-time index membership. An index whose download fails
    or comes back empty is left untouched (never read as 'everyone left the index')."""
    today = ctx.today()
    details: dict[str, Any] = {}
    written = 0
    for index in ctx.config.jobs.constituent_indices:
        res = ctx.router.index_constituents(index)
        if res.data is None or res.source is None or res.data.empty:
            details[index] = {"error": "; ".join(res.reasons)}
            continue
        session = ctx.session_factory()
        try:
            n, details[index] = _sync_index(
                session, index, res.data, res.source.value, res.fetched_at, today
            )
            written += n
            session.commit()
        finally:
            session.close()
    failed = [k for k, v in details.items() if "error" in v]
    if len(failed) == len(ctx.config.jobs.constituent_indices):
        raise RuntimeError(f"no index constituents could be fetched: {details}")
    return JobOutcome(written, {**details, "failed": failed})


__all__ = ["corporate_actions", "eod_prices", "index_constituents", "nse_bhavcopy"]
