"""On-demand per-symbol pipeline (SPEC v0.2 §3.7).

``symbol → prices → corporate actions/adjust → filings index → XBRL parse → PDF gap-fill →
shareholding/events → reconcile → metrics → valuation → technical → scoring → report``

- **Fresh reports are served as they are.** :func:`start_run` creates no run when the stored
  report is newer than the stock's latest price and latest parsed filing, and younger than
  ``jobs.pipeline.max_report_age_hours`` (unless forced). Nifty 500 reports are rebuilt every
  night by ``valuation_scores``, so they normally load instantly.
- **One active run per symbol**; asking again returns the run in progress.
- **Resumable and idempotent.** Every step's state is stored on the run. A worker picks up a
  queued run (or one whose worker stopped heart-beating for ``stale_after_s``) and continues
  from the first unfinished step; the data steps are upserts, so repeating one is harmless.
- **Optional steps never stop the report.** A failed optional step shows as failed (or a
  warning), the run goes on, and its message is added to the report's ``data_gaps``. A
  failed required step (no prices, no report can be built) fails the run.

Steps call the same code as the nightly jobs, restricted to the symbol and to per-run budgets
(``max_xbrl_downloads``, ``max_annual_reports``).
"""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.alerts.notify import notify
from app.core.config import PipelineConfig
from app.data import prices
from app.data.providers.web_session import BLOCKED_MARKER, blocked_message
from app.data.results_store import _fy_end_month
from app.db.enums import FilingStatus, PipelineStatus, StepStatus
from app.db.models import (
    AnnualReport,
    Event,
    Instrument,
    PipelineRun,
    PriceDaily,
    Report,
    ResultFiling,
    Symbol,
)
from app.jobs.common import ensure_instruments
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.reports.build import Built, build_report
from app.reports.data import load_stock_data
from app.reports.diff import change_message, report_changes, summary
from app.reports.service import persist

logger = logging.getLogger(__name__)

TERMINAL = (PipelineStatus.DONE, PipelineStatus.FAILED)
FINISHED_STEP = (StepStatus.OK, StepStatus.WARNING, StepStatus.SKIPPED)


class StepError(Exception):
    """A step failed; the message is shown to the user."""


@dataclass
class State:
    """What the steps of one run share (rebuilt on resume)."""

    symbol: str
    run_id: int
    notes: list[str] = field(default_factory=list)  # optional-step problems → data_gaps
    built: Built | None = None
    trigger: str = "user"  # user | refresh | nightly | results (PipelineRun.trigger)
    years: int = 0  # fiscal years of fundamentals loaded
    pl_years: int = 0  # ... of which have a P&L (revenue or PAT)


@dataclass(frozen=True)
class StepResult:
    status: StepStatus
    message: str


StepFn = Callable[[JobContext, State], StepResult]


@dataclass(frozen=True)
class StepDef:
    name: str
    label: str
    optional: bool
    fn: StepFn


# ───────────────────────── helpers ─────────────────────────


def _blocked(reason: str | None) -> str | None:
    """The standard message when the exchange is blocking this connection (the UI links it to
    the manual uploads), else None."""
    if reason and BLOCKED_MARKER in reason:
        site = "BSE" if "BSE is blocking" in reason else "NSE"
        return blocked_message(site)
    return None


def _job_result(outcome: JobOutcome, symbol: str, ok: str) -> StepResult:
    """A nightly job's outcome for one symbol → a step result."""
    if outcome.skipped_reason:
        return StepResult(StepStatus.SKIPPED, outcome.skipped_reason)
    failed = outcome.details.get("failed") or []
    if symbol in failed:
        reasons = outcome.details.get("failed_reasons") or {}
        reason = reasons.get(symbol) or (failed[symbol] if isinstance(failed, dict)
                                         else "source unavailable")  # fmt: skip
        return StepResult(StepStatus.WARNING, _blocked(str(reason)) or f"not refreshed: {reason}")
    return StepResult(StepStatus.OK, ok)


def _opts(symbol: str) -> JobOptions:
    return JobOptions(symbols=(symbol,), force=True)


# ───────────────────────── steps ─────────────────────────


def _symbol(ctx: JobContext, st: State) -> StepResult:
    session = ctx.session_factory()
    try:
        known = session.scalar(select(Instrument.id).where(Instrument.symbol == st.symbol))
        master = session.scalar(select(Symbol).where(Symbol.nse_symbol == st.symbol))
        have_master = session.scalar(select(func.count()).select_from(Symbol)) or 0
        if known is None and master is None and have_master:
            raise StepError(f"{st.symbol} is not an NSE symbol in the symbol master")
        ensure_instruments(session, [st.symbol])
        session.commit()
    finally:
        session.close()
    sector_note, sector_ok = _classify(ctx, st.symbol)
    if master is not None:
        extra = f", BSE {master.bse_code}" if master.bse_code else ""
        return StepResult(StepStatus.OK if sector_ok else StepStatus.WARNING,
                          f"{master.name} (ISIN {master.isin}{extra}); {sector_note}")  # fmt: skip
    if not have_master:
        return StepResult(StepStatus.WARNING, "symbol master not built yet: not verified; "
                          f"{sector_note}")  # fmt: skip
    return StepResult(StepStatus.OK if sector_ok else StepStatus.WARNING,
                      f"known instrument (not in the symbol master); {sector_note}")  # fmt: skip


def _classify(ctx: JobContext, symbol: str) -> tuple[str, bool]:
    """Sector model for a stock not classified yet (industries.yaml); → (note, mapped)."""
    from app.data.industry_store import classify_symbol

    session = ctx.session_factory()
    try:
        inst = session.scalar(select(Instrument).where(Instrument.symbol == symbol))
        if inst is not None and inst.classified_at is not None:
            if inst.sector:
                return f"sector {inst.sector} ({inst.industry_source}: {inst.basic_industry})", True
            return (f"sector unmapped ({inst.industry_source}: {inst.basic_industry}); "
                    "default model"), False  # fmt: skip
        out = classify_symbol(session, ctx.router, symbol, ctx.config.industries, now=ctx.clock())
        session.commit()
    finally:
        session.close()
    if out.sector is not None and out.fetched:
        return f"sector {out.sector} ({out.source}: {out.label})", True
    return out.message, False


def _prices(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.market import eod_prices

    outcome = eod_prices(ctx, _opts(st.symbol))
    session = ctx.session_factory()
    try:
        last = session.scalar(
            select(func.max(PriceDaily.date))
            .join(Instrument, Instrument.id == PriceDaily.instrument_id)
            .where(Instrument.symbol == st.symbol)
        )
    finally:
        session.close()
    result = _job_result(outcome, st.symbol, "")
    if last is None:
        raise StepError(f"no prices for {st.symbol}: {result.message or 'none returned'}")
    if result.status is StepStatus.OK:
        return StepResult(StepStatus.OK, f"daily bars to {last:%d %b %Y}")
    return StepResult(StepStatus.WARNING, f"{result.message}; using stored bars to {last:%d %b %Y}")


def _indianapi(ctx: JobContext, st: State) -> StepResult:
    """Statements from the Indian API before any NSE step, so a report works with NSE
    blocked. Uses the cache unless due (results, age, or the Refresh button on a cache older
    than a day); reports the years per statement and this month's calls."""
    from app.data.providers.indianapi import usage_text
    from app.jobs.indianapi import StopRun, fetch_symbol

    client = ctx.indianapi
    budget = ctx.config.providers.indianapi.monthly_request_budget
    try:
        out = fetch_symbol(ctx, st.symbol, user=st.trigger == "refresh")
    except StopRun as exc:
        return StepResult(StepStatus.WARNING, str(exc))
    used = client.used_this_month() if client is not None and client.configured else None
    quota = f" · {usage_text(used, budget)}" if used is not None else ""
    if out.status == "failed":
        return StepResult(StepStatus.WARNING, f"{out.message}{quota}")
    return StepResult(StepStatus.OK, f"{out.message}{quota}")


def _corporate_actions(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.market import corporate_actions

    outcome = corporate_actions(ctx, _opts(st.symbol))
    readjusted = st.symbol in (outcome.details.get("readjusted") or [])
    return _job_result(outcome, st.symbol,
                       "prices re-adjusted for new splits/bonuses" if readjusted
                       else "no new splits or bonuses")  # fmt: skip


def _filings_index(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.fundamentals import list_results

    out = list_results(ctx, [st.symbol], recheck_all=True, fallback=True)
    if st.symbol in out["index_failed"]:
        fell_back = " (new quarters from yfinance, flagged)" if out["fallback_written"] else ""
        blocked = _blocked(out.get("index_reasons", {}).get(st.symbol))
        if blocked:
            return StepResult(StepStatus.WARNING, f"{blocked}{fell_back}")
        return StepResult(StepStatus.WARNING, f"NSE results list unavailable{fell_back}")
    return StepResult(StepStatus.OK, f"{out['listed_new']} new filing(s) listed")


def _xbrl_parse(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.fundamentals import download_results

    cfg = ctx.config.jobs.pipeline
    out = download_results(ctx, [st.symbol], limit=cfg.max_xbrl_downloads)
    session = ctx.session_factory()
    try:
        left = session.scalar(
            select(func.count()).select_from(ResultFiling)
            .join(Instrument, Instrument.id == ResultFiling.instrument_id)
            .where(Instrument.symbol == st.symbol, ResultFiling.status == FilingStatus.PENDING)
        ) or 0  # fmt: skip
    finally:
        session.close()
    tried = out["downloaded"] + out.get("reparsed", 0)
    msg = f"{out['parsed']} of {tried} document(s) stored" if tried else "no new results filings"
    if out.get("reparsed"):
        msg += f" ({out['reparsed']} re-parsed from the cache)"
    if left:
        msg += f"; {left} older ones left for the nightly job"
    if out["failed"]:
        msg += f"; {len(out['failed'])} failed"
    if out.get("known_parse_failures"):
        msg += (f"; {out['known_parse_failures']} known parse failure(s) not retried until the "
                f"parser changes ({out['parser_version']})")  # fmt: skip
    msg += f". Years found: {_years_found(ctx, st.symbol)}"
    return StepResult(StepStatus.WARNING if out["failed"] else StepStatus.OK, msg)


def _pdf_gap_fill(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.annual_reports import annual_reports

    outcome = annual_reports(ctx, _opts(st.symbol),
                             limit=ctx.config.jobs.pipeline.max_annual_reports)  # fmt: skip
    d = outcome.details
    if st.symbol in (d.get("index_failed") or []):
        blocked = _blocked((d.get("index_reasons") or {}).get(st.symbol))
        return StepResult(StepStatus.WARNING, blocked or "NSE annual-report list unavailable")
    if not d.get("downloaded"):
        missing = (d.get("gap_years_without_report") or {}).get(st.symbol)
        return StepResult(StepStatus.OK, "no balance-sheet / cash-flow gaps to fill"
                          if not missing else f"no report listed for FY{', FY'.join(
                              str(y) for y in missing[:5])}")  # fmt: skip
    msg = f"{d.get('parsed', 0)} of {d['downloaded']} annual report(s) read"
    if d.get("failed"):
        return StepResult(StepStatus.WARNING, f"{msg}; {len(d['failed'])} failed")
    return StepResult(StepStatus.OK, msg)


def _shareholding(ctx: JobContext, st: State) -> StepResult:
    """Shareholding for the symbol. Corporate events come from market-wide feeds read by the
    ``events`` / ``results_watch`` jobs (not per symbol): this step reports what is on file."""
    from app.jobs.fundamentals import shareholding

    outcome = shareholding(ctx, _opts(st.symbol))
    result = _job_result(outcome, st.symbol, "latest shareholding pattern stored")
    since = ctx.today() - timedelta(days=ctx.config.jobs.event_classification.red_flag_days)
    session = ctx.session_factory()
    try:
        n, flags = session.execute(
            select(func.count(), func.count().filter(Event.red_flag.is_(True)))
            .select_from(Event).join(Instrument, Instrument.id == Event.instrument_id)
            .where(Instrument.symbol == st.symbol, Event.event_date >= since)
        ).one()  # fmt: skip
    finally:
        session.close()
    events = f"{n} corporate event(s) in the last year" + (f", {flags} red flag(s)" if flags
                                                           else "")  # fmt: skip
    return StepResult(result.status, f"{result.message}; {events}")


def _reconcile(ctx: JobContext, st: State) -> StepResult:
    """SPEC §3.9: the latest periods across NSE XBRL, annual-report PDFs, yfinance and (when
    enabled) Market Lens. Open issues lower the valuation confidence (in the report build)."""
    from app.jobs.reconcile import reconcile_symbol

    out = reconcile_symbol(ctx, st.symbol)
    if not out["compared"]:
        return StepResult(StepStatus.OK, out.get("note") or "only one source for the latest "
                          "periods: nothing to compare")  # fmt: skip
    msg = f"{out['compared']} figure(s) compared ({', '.join(out['sources'])})"
    if out["unavailable"]:
        msg += f"; unavailable: {', '.join(out['unavailable'])}"
    if out["open"]:
        return StepResult(StepStatus.WARNING, f"{msg}; {out['open']} difference(s) above "
                          f"{ctx.config.jobs.reconciliation.tolerance_rel:.0%} (banner on the "
                          "stock page)")  # fmt: skip
    return StepResult(StepStatus.OK, f"{msg}; all agree")


def _build(ctx: JobContext, st: State) -> Built:
    session = ctx.session_factory()
    try:
        data = load_stock_data(session, st.symbol, ctx.config)
    except (prices.NoPriceData, prices.UnadjustedPrices) as exc:
        raise StepError(str(exc)) from exc
    finally:
        session.close()
    data.notes = [*data.notes, *st.notes]
    st.years = len(data.annual)
    pl = [c for c in ("revenue", "pat") if c in data.annual.columns]
    st.pl_years = int(data.annual[pl].notna().any(axis=1).sum()) if pl else 0
    return build_report(data, ctx.config)


def _years_found(ctx: JobContext, symbol: str) -> str:
    from app.data.coverage import years_summary

    session = ctx.session_factory()
    try:
        iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
        if iid is None:
            return "no statements stored"
        month = _fy_end_month(session, iid, ctx.config.providers.nse.results)
        return years_summary(session, symbol, month)
    finally:
        session.close()


def _metrics(ctx: JobContext, st: State) -> StepResult:
    """Fails loudly (the report is still built, with prices and technicals) when there is no
    fiscal year with a P&L to compute metrics from, naming what is stored and where to get
    the rest; warns with the metrics it could not compute otherwise."""
    st.built = _build(ctx, st)
    r = st.built.report
    found = _years_found(ctx, st.symbol)
    missing = [k for k, v in r.fundamentals.items() if v is None]
    shown = ", ".join(missing[:6]) + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
    if st.pl_years == 0:
        return StepResult(StepStatus.FAILED, (
            f"no fundamental metrics: no fiscal year with a P&L (revenue / PAT) on file "
            f"[{found}]. Needs the Results XBRL step to store filings, or an XBRL / Screener "
            f"upload (Settings → Uploads). Not computed: {shown or 'all'}"))  # fmt: skip
    msg = f"{st.pl_years} fiscal year(s) with a P&L [{found}]"
    if missing:
        return StepResult(StepStatus.WARNING, f"{msg}; not computed (inputs missing, see the "
                          f"data gaps): {shown}")  # fmt: skip
    return StepResult(StepStatus.OK, msg)


def _valuation(ctx: JobContext, st: State) -> StepResult:
    if st.built is None:  # resumed after a restart
        st.built = _build(ctx, st)
    lv = st.built.report.levels
    if lv.fair_value is None:
        return StepResult(StepStatus.WARNING, "no fair value (inputs missing; see data gaps)")
    return StepResult(StepStatus.OK, f"fair value ₹{lv.fair_value:,.0f}, zone "
                      f"{(st.built.report.zone or 'n/a').replace('_', ' ')}")  # fmt: skip


def _technical(ctx: JobContext, st: State) -> StepResult:
    from app.jobs.technicals import technicals_for

    try:
        out = technicals_for(ctx, st.symbol)
    except (prices.NoPriceData, prices.UnadjustedPrices) as exc:
        return StepResult(StepStatus.WARNING, str(exc))
    st.built = None  # the new snapshot (RS percentile) feeds the scores
    rank = out["rs_percentile"]
    rs = f"RS percentile {rank:.0f} of {out['ranked_against'] + 1}" if rank is not None \
        else "RS percentile needs the nightly technicals run"  # fmt: skip
    return StepResult(StepStatus.OK if rank is not None else StepStatus.WARNING,
                      f"stage {out['stage']}, {rs}")  # fmt: skip


def _scoring(ctx: JobContext, st: State) -> StepResult:
    if st.built is None:
        st.built = _build(ctx, st)
    r = st.built.report
    grade = r.grade_label or r.grade or "n/a"
    return StepResult(
        StepStatus.OK, f"grade {grade}, action {(r.action or 'n/a').replace('_', ' ')}"
    )


def _report(ctx: JobContext, st: State) -> StepResult:
    """Store the report. A results-triggered run (SPEC §3.8) then compares it with the report
    before the run and notifies (in-app + Telegram) when grade, zone or action changed or FV
    moved more than ``results_watch.notify.fv_change_rel``, once per run."""
    if st.built is None:
        st.built = _build(ctx, st)
    report = st.built.report
    msg = f"report stored (as of {report.as_of:%d %b %Y})"
    session = ctx.session_factory()
    try:
        persist(session, st.built)
        run = session.get(PipelineRun, st.run_id, with_for_update=True)
        assert run is not None
        run.report_as_of = report.as_of
        context = dict(run.context or {})
        if "baseline" in context and not context.get("notified"):
            after = summary(report.model_dump(mode="json"))
            before = context["baseline"]
            changes = report_changes(
                before, after, fv_change_rel=ctx.config.jobs.results_watch.notify.fv_change_rel
            ) if before else []  # fmt: skip
            if changes:
                title, body = change_message(st.symbol, context.get("label"), changes, after)
                notify(session, kind="results", title=title, body=body,
                       notifier=ctx.notifier, symbol=st.symbol, price=report.cmp)  # fmt: skip
                msg += f"; notified: {title}"
            else:
                msg += "; no grade, zone, action or FV change to notify" if before else \
                    "; first report for the stock: nothing to compare"  # fmt: skip
            context.update(notified=True, after=after, changes=[c.text for c in changes])
            run.context = context
        session.commit()
    finally:
        session.close()
    return StepResult(StepStatus.OK, msg)


STEPS: tuple[StepDef, ...] = (
    StepDef("symbol", "Symbol", False, _symbol),
    StepDef("indianapi", "Indian API fundamentals", True, _indianapi),
    StepDef("prices", "Prices", False, _prices),
    StepDef("corporate_actions", "Corporate actions & adjustment", True, _corporate_actions),
    StepDef("filings_index", "Filings index", True, _filings_index),
    StepDef("xbrl_parse", "Results XBRL", True, _xbrl_parse),
    StepDef("pdf_gap_fill", "Annual-report PDFs", True, _pdf_gap_fill),
    StepDef("shareholding_events", "Shareholding & events", True, _shareholding),
    StepDef("reconcile", "Reconciliation", True, _reconcile),
    StepDef("metrics", "Fundamental metrics", True, _metrics),
    StepDef("valuation", "Valuation", False, _valuation),
    StepDef("technical", "Technicals", True, _technical),
    StepDef("scoring", "Scoring", False, _scoring),
    StepDef("report", "Report", False, _report),
)
STEP_BY_NAME = {s.name: s for s in STEPS}


# ───────────────────────── freshness, runs ─────────────────────────


@dataclass(frozen=True)
class Freshness:
    fresh: bool
    report_as_of: date | None
    reason: str


def report_freshness(session: Session, symbol: str, cfg: PipelineConfig, now: datetime
                     ) -> Freshness:  # fmt: skip
    """SPEC §3.7 step 1: is the stored report fresher than the latest price date and the
    latest filing, and younger than ``max_report_age_hours``?"""
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
    if iid is None:
        return Freshness(False, None, "unknown symbol")
    rep = session.execute(
        select(Report.as_of, Report.computed_at).where(Report.instrument_id == iid)
        .order_by(Report.as_of.desc(), Report.computed_at.desc()).limit(1)
    ).first()  # fmt: skip
    if rep is None:
        return Freshness(False, None, "no report yet")
    as_of, computed = rep
    last_price = session.scalar(select(func.max(PriceDaily.date))
                                .where(PriceDaily.instrument_id == iid))  # fmt: skip
    if last_price is not None and last_price > as_of:
        return Freshness(False, as_of, f"prices to {last_price} are newer than the report")
    filed = [session.scalar(select(func.max(ResultFiling.parsed_at))
                            .where(ResultFiling.instrument_id == iid)),
             session.scalar(select(func.max(AnnualReport.parsed_at))
                            .where(AnnualReport.instrument_id == iid))]  # fmt: skip
    newest = max((f for f in filed if f is not None), default=None)
    if newest is not None and newest > computed:
        return Freshness(False, as_of, "a filing was stored after the report was built")
    if now - computed > timedelta(hours=cfg.max_report_age_hours):
        return Freshness(False, as_of, f"built more than {cfg.max_report_age_hours:g} h ago")
    return Freshness(True, as_of, "the stored report is up to date")


def new_steps() -> list[dict[str, Any]]:
    return [{"name": s.name, "label": s.label, "optional": s.optional,
             "status": StepStatus.PENDING.value, "message": None, "started_at": None,
             "finished_at": None} for s in STEPS]  # fmt: skip


def start_run(
    session: Session, symbol: str, *, trigger: str, force: bool, cfg: PipelineConfig,
    now: datetime,
) -> tuple[PipelineRun | None, Freshness]:  # fmt: skip
    """The run to follow for ``symbol``: ``None`` when the stored report is fresh (and not
    ``force``), else the active run, else a new queued one. Does not commit."""
    sym = symbol.strip().upper()
    fresh = report_freshness(session, sym, cfg, now)
    if fresh.fresh and not force:
        return None, fresh
    active = session.scalar(
        select(PipelineRun).where(PipelineRun.symbol == sym,
                                  PipelineRun.status.not_in(TERMINAL))
        .order_by(PipelineRun.id.desc()).limit(1)
    )  # fmt: skip
    if active is not None:
        return active, fresh
    run = PipelineRun(symbol=sym, trigger=trigger, force=force, status=PipelineStatus.QUEUED,
                      steps=new_steps(), version=1,
                      instrument_id=session.scalar(select(Instrument.id)
                                                   .where(Instrument.symbol == sym)))  # fmt: skip
    session.add(run)
    session.flush()
    return run, fresh


def active_symbols(session: Session) -> list[str]:
    """Symbols with a run queued or running, oldest first."""
    return list(session.scalars(
        select(PipelineRun.symbol).where(PipelineRun.status.not_in(TERMINAL))
        .order_by(PipelineRun.id)
    ))  # fmt: skip


def claim_next(session: Session, cfg: PipelineConfig, now: datetime) -> int | None:
    """Take the oldest queued run, or a running one whose worker stopped heart-beating
    (``FOR UPDATE SKIP LOCKED``: concurrent workers never take the same run). Commits."""
    stale = now - timedelta(seconds=cfg.stale_after_s)
    run = session.scalar(
        select(PipelineRun)
        .where((PipelineRun.status == PipelineStatus.QUEUED)
               | ((PipelineRun.status == PipelineStatus.RUNNING)
                  & (PipelineRun.heartbeat_at < stale)))
        .order_by(PipelineRun.id).limit(1).with_for_update(skip_locked=True)
    )  # fmt: skip
    if run is None:
        session.rollback()
        return None
    if run.attempts >= cfg.max_attempts:
        run.status, run.error, run.finished_at = PipelineStatus.FAILED, \
            f"interrupted {run.attempts} times; giving up", now  # fmt: skip
        run.version += 1
        session.commit()
        return None
    run.status, run.attempts, run.heartbeat_at = PipelineStatus.RUNNING, run.attempts + 1, now
    run.started_at = run.started_at or now
    run.version += 1
    session.commit()
    return run.id


def _save(ctx: JobContext, run_id: int, **changes: Any) -> None:
    """Apply changes to the run (``step`` = (index, fields) updates one step)."""
    session = ctx.session_factory()
    try:
        run = session.get(PipelineRun, run_id, with_for_update=True)
        assert run is not None
        if "step" in changes:
            i, fields = changes.pop("step")
            steps = [dict(s) for s in run.steps]
            steps[i].update(fields)
            run.steps = steps
        for k, v in changes.items():
            setattr(run, k, v)
        run.heartbeat_at = ctx.now()
        run.version += 1
        session.commit()
    finally:
        session.close()


def _heartbeat(ctx: JobContext, run_id: int, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            session = ctx.session_factory()
            try:
                session.execute(update(PipelineRun).where(PipelineRun.id == run_id)
                                .values(heartbeat_at=ctx.now()))  # fmt: skip
                session.commit()
            finally:
                session.close()
        except Exception:  # a missed beat only risks a (safe) resume elsewhere
            logger.exception("pipeline heartbeat failed for run %s", run_id)


def _notes(steps: Iterable[dict[str, Any]]) -> list[str]:
    return [f"pipeline {s['label'].lower()}: {s['message']}" for s in steps
            if s["optional"] and s["status"] in ("warning", "failed") and s["message"]]  # fmt: skip


def execute(ctx: JobContext, run_id: int) -> PipelineStatus:
    """Run (or resume) a claimed run to the end."""
    session = ctx.session_factory()
    try:
        run = session.get(PipelineRun, run_id)
        assert run is not None
        symbol, trigger, steps = run.symbol, run.trigger, [dict(s) for s in run.steps]
    finally:
        session.close()
    st = State(symbol, run_id, trigger=trigger)
    stop = threading.Event()
    every = ctx.config.jobs.pipeline.stale_after_s / 3
    beat = threading.Thread(target=_heartbeat, args=(ctx, run_id, every, stop), daemon=True)
    beat.start()
    try:
        for i, s in enumerate(steps):
            if s["status"] in FINISHED_STEP or (s["optional"] and s["status"] == "failed"):
                continue
            step = STEP_BY_NAME[s["name"]]
            st.notes = _notes(steps[:i])
            _save(ctx, run_id, step=(i, {"status": "running", "started_at": ctx.now().isoformat(),
                                         "message": None}))  # fmt: skip
            try:
                result = step.fn(ctx, st)
            except StepError as exc:
                result = StepResult(StepStatus.FAILED, str(exc))
            except Exception as exc:
                logger.exception("pipeline %s: step %s failed", symbol, step.name)
                result = StepResult(StepStatus.FAILED, f"{type(exc).__name__}: {exc}")
            fields = {"status": result.status.value, "message": result.message[:500],
                      "finished_at": ctx.now().isoformat()}  # fmt: skip
            steps[i].update(fields)
            _save(ctx, run_id, step=(i, fields))
            if result.status is StepStatus.FAILED and not step.optional:
                for j in range(i + 1, len(steps)):
                    steps[j].update(status="skipped", message="an earlier step failed")
                session = ctx.session_factory()
                try:
                    run = session.get(PipelineRun, run_id)
                    assert run is not None
                    run.steps = steps
                    run.status, run.error = PipelineStatus.FAILED, f"{step.label}: {result.message}"
                    run.finished_at = ctx.now()
                    run.version += 1
                    session.commit()
                finally:
                    session.close()
                return PipelineStatus.FAILED
        _save(ctx, run_id, status=PipelineStatus.DONE, finished_at=ctx.now(), error=None)
        return PipelineStatus.DONE
    finally:
        stop.set()


def run_pending(
    ctx: JobContext, *, max_runs: int | None = None
) -> list[tuple[int, PipelineStatus]]:
    """Claim and execute queued (or abandoned) runs until none is left."""
    done: list[tuple[int, PipelineStatus]] = []
    while max_runs is None or len(done) < max_runs:
        session = ctx.session_factory()
        try:
            run_id = claim_next(session, ctx.config.jobs.pipeline, ctx.now())
        finally:
            session.close()
        if run_id is None:
            break
        done.append((run_id, execute(ctx, run_id)))
    return done


def worker_loop(ctx: JobContext, stop: threading.Event) -> None:
    """The worker's pipeline thread: picks up a queued run within ``poll_interval_s``."""
    interval = ctx.config.jobs.pipeline.poll_interval_s
    while not stop.is_set():
        try:
            if not run_pending(ctx):
                stop.wait(interval)
        except Exception:
            logger.exception("pipeline worker loop error")
            stop.wait(interval * 5)


def utcnow() -> datetime:
    return datetime.now(UTC)
