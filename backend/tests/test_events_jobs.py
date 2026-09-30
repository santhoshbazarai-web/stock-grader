"""SPEC v0.2 §3.8-3.9 end to end over the test database: the events job (linking,
classification, red flags, windows, dedupe), results_watch (a new results filing → a forced
pipeline run → a change notification), reconciliation (issues, causes, resolve, ignore,
confidence) and the event-fed auditor knock-out."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select, update

from app.core.config import JobName
from app.data.events import (
    parse_nse_announcements,
    parse_nse_board_meetings,
    parse_nse_deals,
    parse_nse_pit,
    parse_nse_pledges,
    parse_nse_results_feed,
)
from app.db.enums import (
    EventKind,
    IssueStatus,
    JobStatus,
    LineStatement,
    PeriodType,
    PipelineStatus,
    StatementType,
)
from app.db.models import (
    Event,
    FinLineItem,
    Instrument,
    Notification,
    PipelineRun,
    ReconciliationIssue,
    Report,
    WatchlistItem,
)
from app.db.upsert import upsert
from app.devtools.synthetic import seed_company, seed_index
from app.jobs.common import ensure_instruments
from app.jobs.reconcile import reconcile_symbol
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from app.pipeline.runner import run_pending, start_run
from app.reports.build import build_report
from app.reports.data import load_stock_data
from tests.jobs_support import NOW, TODAY, Env

FIX = Path(__file__).parent / "fixtures" / "events"
CR = 1e7


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def set_instrument(env: Env, symbol: str, **fields: Any) -> int:
    with env.session() as s:
        iid = ensure_instruments(s, [symbol])[symbol]
        if fields:
            s.execute(update(Instrument).where(Instrument.id == iid).values(**fields))
        s.commit()
        return iid


def events_of(env: Env, **where: Any) -> list[Event]:
    with env.session() as s:
        q = select(Event).order_by(Event.id)
        for k, v in where.items():
            q = q.where(getattr(Event, k) == v)
        return list(s.scalars(q))


# ───────────────────────── events job ─────────────────────────


def test_events_job_links_classifies_and_dedupes(env: Env) -> None:
    demo = set_instrument(env, "DEMOIT", isin="INE9DEMO0001", name="Demo Infotech Limited")
    bank = set_instrument(env, "DEMOBANK", name="Demo Bank Limited")
    env.nse.feeds = {
        EventKind.ANNOUNCEMENT: parse_nse_announcements(fixture("nse_announcements.json")),
        EventKind.PLEDGE: parse_nse_pledges(fixture("nse_pledge.json")),
        EventKind.INSIDER_TRADE: parse_nse_pit(fixture("nse_pit.json")),
        EventKind.BULK_DEAL: parse_nse_deals((FIX / "bulk.csv").read_text(), EventKind.BULK_DEAL),
        EventKind.BLOCK_DEAL: parse_nse_deals((FIX / "block.csv").read_text(),
                                              EventKind.BLOCK_DEAL),
    }  # fmt: skip
    env.nse.feed_errors = {EventKind.SAST}
    rec = run_job(REGISTRY[JobName.EVENTS], env.ctx)
    assert rec.status is JobStatus.SUCCESS and rec.outcome is not None
    d = rec.outcome.details
    assert rec.outcome.rows_written == 3 + 1 + 1 + 2 + 1
    assert d["new"]["nse:announcement"] == 3 and "nse:sast" in d["failed"]
    assert d["unlinked"] == 0
    assert "DEMOIT: Resignation of Statutory Auditors" in d["new_red_flags"]
    # a feed read for the first time starts first_run_days back, later runs lookback_days
    first = {k: s for k, s, _ in env.nse.feed_requests}
    assert first[EventKind.ANNOUNCEMENT] == TODAY - timedelta(days=30)

    auditor = events_of(env, category="auditor_resignation")
    assert [(e.instrument_id, e.red_flag, e.exchange) for e in auditor] == [(demo, True, "nse")]
    [pledge] = events_of(env, kind=EventKind.PLEDGE)
    assert pledge.instrument_id == bank  # no symbol in the feed: matched on the company name
    assert pledge.category == "pledge_invocation" and pledge.red_flag
    [rating] = events_of(env, category="credit_rating_downgrade")
    assert rating.instrument_id == bank and rating.red_flag
    [pit] = events_of(env, kind=EventKind.INSIDER_TRADE)
    assert pit.category == "promoter_sale" and not pit.red_flag
    assert pit.data is not None and pit.data["shares"] == 150_000

    env.nse.feed_requests.clear()
    env.nse.feed_errors = set()
    again = run_job(REGISTRY[JobName.EVENTS], env.ctx)
    assert again.outcome is not None and again.outcome.rows_written == 0  # SAST has no rows
    starts = {k: s for k, s, _ in env.nse.feed_requests}
    assert starts[EventKind.ANNOUNCEMENT] == TODAY - timedelta(days=2)
    assert starts[EventKind.SAST] == TODAY - timedelta(days=30)  # never stored a row yet
    assert len(events_of(env, kind=EventKind.ANNOUNCEMENT)) == 3


# ───────────────────────── results_watch ─────────────────────────


@pytest.fixture
def synth(env: Env) -> Env:
    with env.session() as s:
        seed_index(s, env.ctx.config.jobs.universe_index)
        iid = seed_company(s, "SYNTH")
        s.add(WatchlistItem(instrument_id=iid))  # the universe: index members + watchlist
        s.commit()
    return env


def results_feed(symbol: str, basis: str = "Consolidated", xml: str = "Q4_CONS") -> pd.DataFrame:
    host = "https://nsearchives.nseindia.com/corporate/xbrl"
    return parse_nse_results_feed([{
        "symbol": symbol, "companyName": f"{symbol} Ltd", "fromDate": "01-Jan-2024",
        "toDate": "31-Mar-2024", "consolidated": basis, "audited": "Audited",
        "xbrl": f"{host}/{symbol}_{xml}.xml", "broadCastDate": "14-Jun-2024 16:05:00",
    }], ["nsearchives.nseindia.com"])  # fmt: skip


def user_run(env: Env, symbol: str) -> None:
    with env.session() as s:
        run, _ = start_run(s, symbol, trigger="user", force=True,
                           cfg=env.ctx.config.jobs.pipeline, now=NOW)  # fmt: skip
        s.commit()
    assert run is not None
    assert run_pending(env.ctx) == [(run.id, PipelineStatus.DONE)]


def test_results_watch_triggers_pipeline_and_notifies(synth: Env) -> None:
    env = synth
    set_instrument(env, "OUTSIDE")
    user_run(env, "SYNTH")  # the report before the results
    with env.session() as s:
        report = s.scalar(select(Report).join(Instrument).where(Instrument.symbol == "SYNTH"))
        assert report is not None
        before = dict(report.payload)
        fv = before["levels"]["fair_value"]
        assert fv
        # pretend the previous report said something else
        fake_zone = "deep_discount" if before["zone"] != "deep_discount" else "fair"
        before.update(grade="D", grade_label="D", zone=fake_zone,
                      levels={**before["levels"], "fair_value": fv / 2})  # fmt: skip
        report.payload = before
        s.commit()

    env.nse.feeds = {
        EventKind.RESULTS: pd.concat([results_feed("SYNTH"), results_feed("OUTSIDE")],
                                     ignore_index=True),
        EventKind.BOARD_MEETING: parse_nse_board_meetings([
            {"bm_symbol": "SYNTH", "bm_purpose": "Financial Results",
             "bm_date": (TODAY + timedelta(days=3)).strftime("%d-%b-%Y")}]),
    }  # fmt: skip
    rec = run_job(REGISTRY[JobName.RESULTS_WATCH], env.ctx)
    assert rec.status is JobStatus.SUCCESS and rec.outcome is not None
    d = rec.outcome.details
    assert d["runs_started"] == ["SYNTH"] and d["outside_universe"] == 1
    assert d["expected_results"] == [
        {"symbol": "SYNTH", "date": (TODAY + timedelta(days=3)).isoformat()}
    ]
    [ev] = [e for e in events_of(env, kind=EventKind.RESULTS) if e.symbol == "SYNTH"]
    assert ev.handled_at is not None and ev.pipeline_run_id is not None
    with env.session() as s:
        run = s.get(PipelineRun, ev.pipeline_run_id)
        assert run is not None and run.trigger == "results" and run.force
        assert run.context is not None and run.context["label"] == "Q4 FY24 results"
        assert run.context["baseline"]["grade"] == "D"

    assert run_pending(env.ctx) == [(ev.pipeline_run_id, PipelineStatus.DONE)]
    with env.session() as s:
        [note] = s.scalars(select(Notification).where(Notification.kind == "results")).all()
        run = s.get(PipelineRun, ev.pipeline_run_id)
        assert run is not None and run.context is not None
    assert note.symbol == "SYNTH" and note.telegram == "disabled"
    assert note.title.startswith("SYNTH Q4 FY24 results: Grade D→")
    assert "FV ₹" in note.title and "zone Deep Discount→" in note.title
    assert run.context["notified"] is True and run.context["changes"]
    report_step = next(st for st in run.steps if st["name"] == "report")
    assert "notified: SYNTH Q4 FY24 results" in report_step["message"]

    # the other basis of the same results within rerun_after_hours: no second run
    env.nse.feeds[EventKind.RESULTS] = results_feed("SYNTH", "Standalone", "Q4_STAND")
    again = run_job(REGISTRY[JobName.RESULTS_WATCH], env.ctx)
    assert again.outcome is not None
    assert again.outcome.details["already_refreshed"] == ["SYNTH"]
    assert again.outcome.details["runs_started"] == []
    with env.session() as s:
        assert len(s.scalars(select(PipelineRun)).all()) == 2


def test_results_run_without_change_does_not_notify(synth: Env) -> None:
    env = synth
    user_run(env, "SYNTH")
    env.nse.feeds = {EventKind.RESULTS: results_feed("SYNTH")}
    run_job(REGISTRY[JobName.RESULTS_WATCH], env.ctx)
    [(run_id, status)] = run_pending(env.ctx)
    assert status is PipelineStatus.DONE
    with env.session() as s:
        assert s.scalars(select(Notification)).all() == []
        run = s.get(PipelineRun, run_id)
        assert run is not None and run.context is not None and run.context["changes"] == []
    msg = next(st for st in run.steps if st["name"] == "report")["message"]
    assert msg.endswith("no grade, zone, action or FV change to notify")


def test_feed_jobs_skip_while_the_other_holds_the_exchanges(env: Env) -> None:
    lock = env.ctx.redis.lock("job-lock:exchange-feeds", timeout=60)
    assert lock.acquire()
    c = env.ctx.config
    events_cfg = c.jobs.events.model_copy(update={"lock_wait_s": 1})
    env.ctx.config = c.model_copy(update={"jobs": c.jobs.model_copy(update={"events": events_cfg})})
    try:
        rec = run_job(REGISTRY[JobName.EVENTS], env.ctx)
    finally:
        lock.release()
    assert rec.status is JobStatus.SKIPPED and env.nse.feed_requests == []


# ───────────────────────── reconciliation ─────────────────────────

FY24, FY23 = date(2024, 3, 31), date(2023, 3, 31)


def line(iid: int, code: str, value_cr: float, *, end: date = FY24, version: int = 1,
         basis: StatementType = StatementType.CONSOLIDATED) -> dict[str, Any]:  # fmt: skip
    statement = {"total_assets": LineStatement.BS, "total_equity": LineStatement.BS,
                 "cfo": LineStatement.CF}.get(code, LineStatement.PL)  # fmt: skip
    return {
        "instrument_id": iid, "isin": None, "period_end": end,
        "period_type": PeriodType.INSTANT if statement is LineStatement.BS else PeriodType.YEAR,
        "statement": statement, "basis": basis, "item_code": code, "value_inr": value_cr * CR,
        "unit": "amount", "version": version, "source": "nse", "filing_id": None,
        "announced_at": None, "usable_from": None, "derived": False, "tag": f"in-capmkt:{code}",
        "map_version": 1, "annual_report_id": None, "confidence": None,
    }  # fmt: skip


@pytest.fixture
def filed(env: Env) -> tuple[Env, int]:
    iid = set_instrument(env, "RECO")
    rows = [line(iid, c, v) for c, v in (
        ("revenue", 1000), ("pat", 100), ("pbt", 130), ("interest", 10), ("depreciation", 20),
        ("other_income", 5), ("cfo", 120), ("total_assets", 5000))]  # fmt: skip
    rows += [line(iid, "total_equity", 1900), line(iid, "total_equity", 2000, version=2),
             line(iid, "revenue", 800, basis=StatementType.STANDALONE),
             line(iid, "revenue", 900, end=FY23)]  # fmt: skip
    with env.session() as s:
        upsert(s, FinLineItem, rows)
        s.commit()
    return env, iid


def yf(**values: float) -> pd.DataFrame:
    return pd.DataFrame([values], index=pd.DatetimeIndex([pd.Timestamp(FY24)], name="period_end"))


def issues(env: Env, iid: int) -> dict[str, ReconciliationIssue]:
    with env.session() as s:
        rows = s.scalars(
            select(ReconciliationIssue).where(ReconciliationIssue.instrument_id == iid)
        )
        return {i.item_code: i for i in rows}


def test_reconcile_finds_causes_then_resolves(filed: tuple[Env, int]) -> None:
    env, iid = filed
    env.quarterly.annual_frames["RECO"] = yf(
        revenue=800, pat=100.5, total_equity=1900, total_assets=5000 / 1e5, cfo=120,
        ebitda=999)  # fmt: skip
    out = reconcile_symbol(env.ctx, "RECO")
    assert out["open"] == 3 and out["new"] == 3 and out["sources"] == ["NSE XBRL", "yfinance"]
    assert out["compared"] == 5  # revenue, pat, cfo, total assets, equity (EBITDA excluded)
    found = issues(env, iid)
    assert {k: v.cause for k, v in found.items()} == {
        "revenue": "basis",
        "total_equity": "restatement",
        "total_assets": "units",
    }
    rev = found["revenue"]
    assert rev.status is IssueStatus.OPEN and rev.source == "yfinance"
    assert rev.reference_source == "nse_xbrl" and rev.diff_rel == pytest.approx(0.2)
    assert rev.period_type is PeriodType.YEAR and rev.basis is StatementType.CONSOLIDATED
    assert rev.values == {"nse_xbrl": 1000 * CR, "yfinance": 800 * CR}

    # the owner ignores one; a re-check with the same figures keeps it ignored
    with env.session() as s:
        s.execute(update(ReconciliationIssue).where(ReconciliationIssue.item_code == "revenue")
                  .values(status=IssueStatus.IGNORED))  # fmt: skip
        s.commit()
    out = reconcile_symbol(env.ctx, "RECO")
    assert out["ignored"] == 1 and out["open"] == 2 and out["new"] == 0
    assert issues(env, iid)["revenue"].status is IssueStatus.IGNORED

    # sources agree again → resolved
    env.quarterly.annual_frames["RECO"] = yf(revenue=1000, pat=100, total_equity=2000,
                                             total_assets=5000, cfo=120)  # fmt: skip
    out = reconcile_symbol(env.ctx, "RECO")
    assert out["open"] == 0 and out["resolved"] == 2
    after = issues(env, iid)
    assert after["total_assets"].status is IssueStatus.RESOLVED
    assert after["total_assets"].resolved_at is not None


def test_reconcile_job_and_unavailable_source(filed: tuple[Env, int]) -> None:
    env, _ = filed
    rec = run_job(REGISTRY[JobName.RECONCILE], env.ctx, JobOptions(symbols=("RECO",)))
    assert rec.status is JobStatus.SUCCESS and rec.outcome is not None
    d = rec.outcome.details
    assert d["symbols"] == 1 and d["with_open_issues"] == []
    assert list(d["unavailable"]["RECO"]) == ["yfinance annual", "yfinance quarterly"]


def test_open_issue_lowers_confidence_and_is_listed(synth: Env) -> None:
    env = synth
    with env.session() as s:
        base = build_report(load_stock_data(s, "SYNTH", env.ctx.config), env.ctx.config).report
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
        assert iid is not None and base.levels.confidence is not None
        s.add(ReconciliationIssue(
            instrument_id=iid, period_end=FY24, period_type=PeriodType.YEAR,
            basis=StatementType.CONSOLIDATED, item_code="pat", source="yfinance",
            reference_source="nse_xbrl", reference_value_inr=100 * CR, value_inr=90 * CR,
            diff_rel=0.1, values={}, cause=None, status=IssueStatus.OPEN,
            reasons=["PAT (year to 31 Mar 2024, consolidated): yfinance ₹90.00 cr vs NSE XBRL "
                     "₹100.00 cr, 10.0% apart", "no cause found (unexplained difference)"],
            detected_at=NOW, checked_at=NOW))  # fmt: skip
        s.commit()
        report = build_report(load_stock_data(s, "SYNTH", env.ctx.config),
                              env.ctx.config).report  # fmt: skip
    levels = ["high", "medium", "low"]
    expected = levels[min(2, levels.index(base.levels.confidence) + 1)]
    assert report.levels.confidence == expected
    assert report.reconciliation_issues == [
        "PAT (year to 31 Mar 2024, consolidated): yfinance ₹90.00 cr vs NSE XBRL ₹100.00 cr, "
        "10.0% apart"]  # fmt: skip
    assert any("open reconciliation issue(s)" in r for r in report.valuation.reasons)


# ───────────────────────── auditor knock-out from events ─────────────────────────


def event(iid: int | None, day: date, title: str, category: str | None, *,
          red: bool = False, source_id: str) -> Event:  # fmt: skip
    return Event(exchange="nse", kind=EventKind.ANNOUNCEMENT, source_id=source_id,
                 instrument_id=iid, title=title, category=category, red_flag=red,
                 event_date=day, disseminated_at=datetime.combine(day, datetime.min.time()),
                 fetched_at=NOW)  # fmt: skip


def test_auditor_knockout_from_events(synth: Env) -> None:
    env = synth
    with env.session() as s:
        data = load_stock_data(s, "SYNTH", env.ctx.config)
        as_of = data.daily.index.max().date()
        # no announcements stored: the resignation record is unknown
        assert data.auditor_resignations is None
        # announcements stored back beyond the 3-year window, none about SYNTH's auditor → []
        s.add(event(None, as_of - timedelta(days=4 * 365), "Old news", None, source_id="old"))
        s.commit()
        assert load_stock_data(s, "SYNTH", env.ctx.config).auditor_resignations == []
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
        s.add(event(iid, as_of - timedelta(days=30), "Resignation of Statutory Auditors",
                    "auditor_resignation", red=True, source_id="aud"))  # fmt: skip
        s.commit()
        data = load_stock_data(s, "SYNTH", env.ctx.config)
        report = build_report(data, env.ctx.config).report
    assert data.auditor_resignations == [as_of - timedelta(days=30)]
    assert "auditor" in report.knockouts.triggered
    assert any(f.startswith("event: Resignation of Statutory Auditors") for f in report.red_flags)
