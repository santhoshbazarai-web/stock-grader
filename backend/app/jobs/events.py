"""Exchange event feeds (SPEC v0.2 §3.8, §10).

- ``events`` (every 30 min, 07:00-23:00): NSE announcements, pledge, SAST and insider-trading
  disclosures and the bulk / block deal files → the events table (classified; red flags
  marked).
- ``results_watch`` (every 15 min): the NSE results filing list, the board-meeting calendar
  and BSE announcements (whose "Result" category is a results filing). A new results filing
  for a universe stock starts that stock's pipeline (trigger ``results``, forced) carrying the
  previous report's grade / zone / action / FV; the pipeline's report step notifies when they
  changed (``app.reports.diff``).

Both read the last ``lookback_days`` (``first_run_days`` for a feed never read) and dedupe on
the feed's ids, and they never read the exchanges at the same time (a shared Redis lock,
§3.2a: no parallel fetchers against one host).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Provider
from app.data.event_store import Linker, store_events
from app.data.events import is_results_meeting
from app.data.router import Outcome
from app.db.enums import EventKind
from app.db.models import Event, Instrument, PipelineRun, Report
from app.jobs.common import universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.pipeline.runner import start_run
from app.reports.diff import quarter_label, summary

logger = logging.getLogger(__name__)
FEEDS_LOCK = "job-lock:exchange-feeds"


class FeedsBusy(RuntimeError):
    """The other feed job kept the exchanges longer than ``events.lock_wait_s``."""


@contextmanager
def _feeds_lock(ctx: JobContext) -> Iterator[None]:
    lock = ctx.redis.lock(FEEDS_LOCK, timeout=ctx.config.jobs.lock_ttl_s,
                          blocking_timeout=ctx.config.jobs.events.lock_wait_s)  # fmt: skip
    if not lock.acquire():
        raise FeedsBusy(f"another feed job held the exchanges for over "
                        f"{ctx.config.jobs.events.lock_wait_s}s")  # fmt: skip
    try:
        yield
    finally:
        try:
            lock.release()
        except LockError:  # expired while we ran: nothing to release
            logger.warning("exchange-feeds lock expired before release")


def _kinds_stored(kind: EventKind, provider: Provider) -> list[EventKind]:
    # BSE's announcement feed also yields its results filings
    if provider is Provider.BSE and kind is EventKind.ANNOUNCEMENT:
        return [EventKind.ANNOUNCEMENT, EventKind.RESULTS]
    return [kind]


def read_feeds(
    ctx: JobContext, feeds: dict[Provider, list[EventKind]], *, lookback_days: int,
    first_run_days: int,
) -> dict[str, Any]:  # fmt: skip
    """Read every (provider, feed), store the rows, commit per feed. Returns a summary with
    ``read`` / ``new`` counts per feed, ``failed`` feeds and ``new_ids``."""
    today = ctx.today()
    read: dict[str, int] = {}
    new: dict[str, int] = {}
    failed: dict[str, str] = {}
    warnings: list[str] = []
    new_ids: list[int] = []
    unlinked = 0
    session = ctx.session_factory()
    try:
        linker = Linker.load(session)
    finally:
        session.close()
    for provider, kinds in feeds.items():
        for kind in kinds:
            name = f"{provider.value}:{kind.value}"
            session = ctx.session_factory()
            try:
                seen = session.scalar(
                    select(func.count()).select_from(Event).where(
                        Event.exchange == provider.value,
                        Event.kind.in_(_kinds_stored(kind, provider)))
                ) or 0  # fmt: skip
            finally:
                session.close()
            start = today - timedelta(days=lookback_days if seen else first_run_days)
            res = ctx.router.events(provider, kind, start, today)
            if res.data is None:
                if res.attempts and all(a.outcome is Outcome.EMPTY for a in res.attempts):
                    read[name] = 0
                    continue
                failed[name] = "; ".join(res.reasons)[:500]
                continue
            warnings += [f"{name}: {w}" for w in res.data.attrs.get("warnings", [])][:10]
            session = ctx.session_factory()
            try:
                stored = store_events(session, res.data, exchange=provider.value,
                                      cfg=ctx.config.jobs.event_classification,
                                      now=ctx.now(), linker=linker)  # fmt: skip
                session.commit()
            finally:
                session.close()
            read[name], new[name] = stored.rows, len(stored.new_ids)
            new_ids += stored.new_ids
            unlinked += stored.unlinked
    return {"read": read, "new": new, "failed": failed, "unlinked": unlinked,
            "warnings": warnings[:30], "new_ids": new_ids}  # fmt: skip


def events(ctx: JobContext, options: JobOptions) -> JobOutcome:
    del options  # market-wide feeds: --symbols does not narrow them
    cfg = ctx.config.jobs.events
    try:
        with _feeds_lock(ctx):
            out = read_feeds(ctx, cfg.feeds, lookback_days=cfg.lookback_days,
                             first_run_days=cfg.first_run_days)  # fmt: skip
    except FeedsBusy as exc:
        return JobOutcome(skipped_reason=str(exc))
    session = ctx.session_factory()
    try:
        flags = [f"{sym or company}: {title}" for sym, company, title in session.execute(
            select(Instrument.symbol, Event.company, Event.title)
            .outerjoin(Instrument, Instrument.id == Event.instrument_id)
            .where(Event.id.in_(out["new_ids"]), Event.red_flag.is_(True))
        )] if out["new_ids"] else []  # fmt: skip
    finally:
        session.close()
    details = {k: v for k, v in out.items() if k != "new_ids"}
    details["new_red_flags"] = flags[:50]
    return JobOutcome(sum(out["new"].values()), details)


# ───────────────────────── results_watch ─────────────────────────


def _baseline(session: Session, iid: int) -> dict[str, Any] | None:
    payload = session.scalar(
        select(Report.payload).where(Report.instrument_id == iid)
        .order_by(Report.as_of.desc(), Report.computed_at.desc()).limit(1)
    )  # fmt: skip
    return summary(payload) if isinstance(payload, dict) else None


def _label(events_: list[Event], fy_end_month: int) -> str:
    ends = [date.fromisoformat(e.data["period_end"]) for e in events_
            if e.data and e.data.get("period_end")]  # fmt: skip
    label = quarter_label(max(ends), fy_end_month) if ends else None
    return f"{label} results" if label else "results"


def trigger_results_runs(ctx: JobContext, symbols: list[str]) -> dict[str, Any]:
    """Unhandled results filings of the last ``lookback_days`` → one pipeline per stock in
    ``symbols`` (the universe). Each event is marked handled with the run it joined."""
    cfg = ctx.config.jobs.results_watch
    now = ctx.now()
    since = datetime.combine(ctx.today() - timedelta(days=cfg.lookback_days), datetime.min.time(),
                             tzinfo=now.tzinfo)  # fmt: skip
    started: list[str] = []
    joined: list[str] = []
    recent: list[str] = []
    outside = 0
    session = ctx.session_factory()
    try:
        pending = list(session.scalars(
            select(Event).where(
                Event.kind == EventKind.RESULTS, Event.handled_at.is_(None),
                func.coalesce(Event.disseminated_at, Event.fetched_at) >= since,
            ).order_by(Event.id)
        ))  # fmt: skip
        by_instrument: dict[int, list[Event]] = {}
        for ev in pending:
            if ev.instrument_id is not None:
                by_instrument.setdefault(ev.instrument_id, []).append(ev)
        names = dict(session.execute(select(Instrument.id, Instrument.symbol)
                                     .where(Instrument.id.in_(by_instrument))).all())  # fmt: skip
        wanted = set(symbols)
        for iid, evs in by_instrument.items():
            symbol = names[iid]
            if symbol not in wanted:
                outside += 1
                for ev in evs:
                    ev.handled_at = now
                continue
            window = now - timedelta(hours=cfg.rerun_after_hours)
            previous = session.scalar(
                select(PipelineRun).where(PipelineRun.instrument_id == iid,
                                          PipelineRun.trigger == "results",
                                          PipelineRun.created_at >= window)
                .order_by(PipelineRun.id.desc()).limit(1)
            )  # fmt: skip
            if previous is not None:
                recent.append(symbol)
                run = previous
            else:
                base = _baseline(session, iid)
                new, _ = start_run(session, symbol, trigger="results", force=True,
                                   cfg=ctx.config.jobs.pipeline, now=now)  # fmt: skip
                assert new is not None  # forced: always a run
                run = new
                ctx_json = dict(run.context or {})
                ctx_json.setdefault("baseline", base)
                ctx_json["label"] = _label(evs, ctx.config.providers.nse.results
                                           .default_fy_end_month)  # fmt: skip
                ctx_json["filings"] = [ev.title for ev in evs][:10]
                run.context = ctx_json
                (started if run.trigger == "results" else joined).append(symbol)
            for ev in evs:
                ev.handled_at, ev.pipeline_run_id = now, run.id
        session.commit()
    finally:
        session.close()
    return {"runs_started": started, "runs_joined": joined, "already_refreshed": recent,
            "outside_universe": outside,
            "unlinked": sum(1 for ev in pending if ev.instrument_id is None)}  # fmt: skip


def board_calendar(ctx: JobContext, symbols: list[str]) -> list[dict[str, Any]]:
    """Universe stocks with a results board meeting from today to ``board_meeting_days_ahead``
    days ahead (the companies to expect results from)."""
    cfg = ctx.config.jobs.results_watch
    today = ctx.today()
    session = ctx.session_factory()
    try:
        rows = session.execute(
            select(Instrument.symbol, Event.event_date, Event.data)
            .join(Instrument, Instrument.id == Event.instrument_id)
            .where(Event.kind == EventKind.BOARD_MEETING, Instrument.symbol.in_(symbols),
                   Event.event_date.between(today, today + timedelta(
                       days=cfg.board_meeting_days_ahead)))
            .order_by(Event.event_date, Instrument.symbol)
        ).all()  # fmt: skip
    finally:
        session.close()
    out: dict[tuple[str, date], dict[str, Any]] = {}
    for sym, day, data in rows:
        if is_results_meeting((data or {}).get("purpose"), cfg.results_purposes):
            out.setdefault((sym, day), {"symbol": sym, "date": day.isoformat()})
    return list(out.values())


def results_watch(ctx: JobContext, options: JobOptions) -> JobOutcome:
    cfg = ctx.config.jobs.results_watch
    try:
        with _feeds_lock(ctx):
            out = read_feeds(ctx, cfg.feeds, lookback_days=cfg.lookback_days,
                             first_run_days=cfg.lookback_days)  # fmt: skip
    except FeedsBusy as exc:
        return JobOutcome(skipped_reason=str(exc))
    symbols = universe(ctx, options)
    runs = trigger_results_runs(ctx, symbols)
    calendar = board_calendar(ctx, symbols)
    details = {k: v for k, v in out.items() if k != "new_ids"}
    details.update(runs)
    details["expected_results"] = calendar[:100]
    details["expected_today"] = sum(1 for c in calendar if c["date"] == ctx.today().isoformat())
    return JobOutcome(len(runs["runs_started"]) + len(runs["runs_joined"]), details)
