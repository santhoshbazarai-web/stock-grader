"""fundamentals_indianapi (app/jobs/indianapi.py) end to end on the recorded fixtures, with a fake
HTTP session in place of stock.indianapi.in: storage, raw cache, names, priority under filed
figures, and every failure (wrong company, 404, 429, no key, exhausted budget) ending as a
data gap, never a crash and never wrong data."""

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select, update

from app.core.config import JobName
from app.data.providers.indianapi import DbQuota, IndianApiClient
from app.db.enums import LineStatement, PeriodType, StatementType
from app.db.models import (
    ApiUsage,
    DataGap,
    FinAnnual,
    FinLineItem,
    FinQuarterly,
    Instrument,
    VendorName,
    VendorResponse,
)
from app.jobs.common import ensure_instruments
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from tests.jobs_support import NOW, Env

FIX = Path(__file__).parent / "fixtures" / "indianapi"
STATS = {"yoy_results": "pl", "balancesheet": "bs", "cashflow": "cf", "quarter_results": "qr"}
COMPANIES = {  # symbol → (fixture prefix, ISIN, master name, vendor name)
    "HDFCBANK": ("hdfcbank", "INE040A01034", "HDFC Bank Limited", "HDFC Bank"),
    "TCS": ("tcs", "INE467B01029", "Tata Consultancy Services Limited",
            "Tata Consultancy Services"),
}  # fmt: skip


@dataclass
class Resp:
    status_code: int
    content: bytes


@dataclass
class FakeVendor:
    """Answers by vendor name like stock.indianapi.in, from the fixtures. ``mode``: ok | wrong
    (every name answers with TCS) | 404 | 429."""

    mode: str = "ok"
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str],
            timeout: float) -> Resp:  # fmt: skip
        path = url.removeprefix("https://stock.indianapi.in")
        self.calls.append((path, dict(params)))
        self.headers_seen.append(headers)
        if self.mode in ("404", "429"):
            return Resp(int(self.mode), b"")
        name = params.get("name") or params.get("stock_name")
        prefix = next((p for p, _, _, v in COMPANIES.values() if v == name), None)
        if self.mode == "wrong":
            prefix = "tcs"
        if prefix is None:
            return Resp(404, b'{"error": "not found"}')
        f = FIX / (f"{prefix}_stock.json" if path == "/stock"
                   else f"{prefix}_{STATS[params['stats']]}.json")  # fmt: skip
        return Resp(200, f.read_bytes()) if f.exists() else Resp(404, b"")


def setup(env: Env, vendor: FakeVendor, *, key: str | None = "test-key-0123",
          budget: int | None = None) -> None:  # fmt: skip
    cfg = env.ctx.config.providers.indianapi
    if budget is not None:
        cfg = cfg.model_copy(update={"monthly_request_budget": budget})
    env.ctx.indianapi = IndianApiClient(
        cfg, SecretStr(key) if key is not None else None, quota=DbQuota(env.Session, cfg,
                                                                        clock=lambda: NOW),
        limiter=None, retry=env.ctx.config.providers.retry, session=vendor,  # type: ignore[arg-type]
        sleep=lambda _: None, clock=lambda: NOW,
    )  # fmt: skip
    with env.session() as s:
        for sym, (_, isin, name, _) in COMPANIES.items():
            iid = ensure_instruments(s, [sym])[sym]
            s.execute(update(Instrument).where(Instrument.id == iid).values(isin=isin, name=name))
        s.commit()


def run(env: Env, *symbols: str, force: bool = False) -> Any:
    return run_job(REGISTRY[JobName.FUNDAMENTALS_INDIANAPI], env.ctx,
                   JobOptions(symbols=symbols, force=force))  # fmt: skip


def gaps(env: Env, symbol: str) -> list[str]:
    with env.session() as s:
        q = (select(DataGap).join(Instrument, Instrument.id == DataGap.instrument_id)
             .where(Instrument.symbol == symbol))  # fmt: skip
        return [g.reason for g in s.scalars(q)]


def years(env: Env, symbol: str) -> dict[int, FinAnnual]:
    with env.session() as s:
        q = (select(FinAnnual).join(Instrument, Instrument.id == FinAnnual.instrument_id)
             .where(Instrument.symbol == symbol))  # fmt: skip
        rows = s.scalars(q).all()
    return {r.fiscal_year: r for r in rows}


def test_hdfcbank_twelve_years_stored_from_the_vendor(env: Env) -> None:
    vendor = FakeVendor()
    setup(env, vendor)
    rec = run(env, "HDFCBANK")
    d = rec.outcome.details
    assert rec.status.value == "success" and list(d["stored"]) == ["HDFCBANK"]
    assert d["stored"]["HDFCBANK"].startswith("P&L 12 yr, BS 12 yr, CF 12 yr (bank model")
    assert d["calls"] == 5 and d["usage"] == "Indian API: 5/500 calls this month"
    # the name tried first is the master name without "Limited"; the key only in the header
    assert vendor.calls[0] == ("/stock", {"name": "HDFC Bank"})
    assert all(h == {"X-Api-Key": "test-key-0123"} for h in vendor.headers_seen)
    rows = years(env, "HDFCBANK")
    assert sorted(rows) == list(range(2015, 2027))
    fy26 = rows[2026]
    assert fy26.source == "indianapi" and fy26.vendor_reclassified
    assert fy26.statement_type is StatementType.CONSOLIDATED
    assert fy26.pat == pytest.approx(76025.97) and fy26.pbt == pytest.approx(102141.45)
    assert fy26.total_equity == pytest.approx(586059.47)
    assert fy26.extra["deposits"] == pytest.approx(3099638.29)
    assert fy26.extra["advances"] == pytest.approx(3048329.65)
    assert rows[2015].revenue == pytest.approx(50666) and rows[2015].pat == pytest.approx(10703)
    with env.session() as s:
        q = s.scalars(
            select(FinQuarterly).where(FinQuarterly.period_end == date(2026, 3, 31))
        ).one()
        # owners' share (21,074 incl. minority), PBT from quarter_results (/stock's is wrong)
        assert q.pat == pytest.approx(20350.76) and q.pbt == pytest.approx(27672)
        assert s.scalar(select(func.count()).select_from(FinQuarterly)) >= 13
        li = s.scalars(select(FinLineItem).where(
            FinLineItem.item_code == "deposits", FinLineItem.period_end == date(2026, 3, 31)
        )).one()  # fmt: skip
        assert li.source == "indianapi" and li.vendor_reclassified and li.usable_from is None
        assert li.value_inr == pytest.approx(3099638.29e7)
        assert li.tag == "indianapi stock:BAL:TotalDeposits"
        resp = s.scalars(select(VendorResponse).order_by(VendorResponse.id)).all()
        assert [r.endpoint for r in resp] == ["/stock", "yoy_results", "balancesheet", "cashflow",
                                             "quarter_results"]  # fmt: skip
        assert resp[0].identity_ok is True and resp[0].note == "ISIN INE040A01034 matches"
        assert all("test-key" not in json.dumps(r.params) for r in resp)
        assert all(r.raw_path and r.raw_path.startswith("indianapi/") for r in resp)
        assert s.scalar(select(VendorName.vendor_name)) == "HDFC Bank"
        assert s.scalar(select(ApiUsage.calls)) == 5


def test_a_current_cache_costs_no_call(env: Env) -> None:
    vendor = FakeVendor()
    setup(env, vendor)
    run(env, "TCS")
    n = len(vendor.calls)
    rec = run(env, "TCS")
    assert len(vendor.calls) == n and list(rec.outcome.details["cached"]) == ["TCS"]
    assert "is current" in rec.outcome.details["cached"]["TCS"]
    rows = years(env, "TCS")
    assert sorted(rows) == list(range(2015, 2027)) and rows[2026].cfo == pytest.approx(52094)


def test_an_exchange_filed_figure_is_never_overwritten(env: Env) -> None:
    vendor = FakeVendor()
    setup(env, vendor)
    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "TCS"))
        s.execute(insert(FinLineItem), [{
            "instrument_id": iid, "period_end": date(2026, 3, 31), "period_type": PeriodType.YEAR,
            "statement": LineStatement.PL, "basis": StatementType.CONSOLIDATED,
            "item_code": "revenue", "value_inr": 267000e7, "unit": "amount", "version": 1,
            "source": "nse_xbrl", "derived": False,
        }])  # fmt: skip
        s.add(FinAnnual(instrument_id=iid, statement_type=StatementType.CONSOLIDATED,
                        period_end=date(2026, 3, 31), fiscal_year=2026, revenue=267000.0,
                        source="nse", fetched_at=NOW))  # fmt: skip
        s.commit()
    run(env, "TCS")
    fy26 = years(env, "TCS")[2026]
    assert fy26.revenue == pytest.approx(267000.0) and fy26.source == "nse"  # filed figure kept
    assert fy26.pbt == pytest.approx(65487) and fy26.vendor_reclassified  # vendor fills the rest


def test_wrong_company_is_never_stored(env: Env) -> None:
    setup(env, FakeVendor(mode="wrong"))
    rec = run(env, "HDFCBANK")
    assert list(rec.outcome.details["failed"]) == ["HDFCBANK"]
    assert years(env, "HDFCBANK") == {}
    g = gaps(env, "HDFCBANK")
    assert any("vendor returned a different company" in x and "Tata Consultancy" in x for x in g)
    with env.session() as s:
        ids = s.scalars(select(VendorResponse.identity_ok)).all()
        assert ids and all(i is False for i in ids)  # kept for audit, never mapped
        assert s.scalar(select(func.count()).select_from(FinLineItem)) == 0


def test_not_found_under_any_name(env: Env) -> None:
    setup(env, FakeVendor(mode="404"))
    rec = run(env, "HDFCBANK")
    assert list(rec.outcome.details["failed"]) == ["HDFCBANK"]
    assert any("name_fallbacks" in x for x in gaps(env, "HDFCBANK"))
    assert years(env, "HDFCBANK") == {}


def test_429_after_retries_is_a_gap(env: Env) -> None:
    vendor = FakeVendor(mode="429")
    setup(env, vendor)
    rec = run(env, "HDFCBANK")
    assert list(rec.outcome.details["failed"]) == ["HDFCBANK"]
    assert len(vendor.calls) == env.ctx.config.providers.retry.max_attempts
    assert any("Indian API /stock unavailable" in x and "HTTP 429" in x
               for x in gaps(env, "HDFCBANK"))  # fmt: skip


def test_missing_key_is_a_gap_without_calls(env: Env) -> None:
    vendor = FakeVendor()
    setup(env, vendor, key=None)
    rec = run(env, "HDFCBANK")
    assert vendor.calls == [] and list(rec.outcome.details["failed"]) == ["HDFCBANK"]
    assert "Indian API not configured: add INDIANAPI_KEY in .env" in gaps(env, "HDFCBANK")


def test_exhausted_budget_stops_the_run(env: Env) -> None:
    vendor = FakeVendor()
    setup(env, vendor, budget=7)  # stops at 6 calls: one stock (5), then 1 call
    rec = run(env, "HDFCBANK", "TCS")
    d = rec.outcome.details
    assert list(d["stored"]) == ["HDFCBANK"] and d["stopped"].startswith(
        "Indian API budget reached"
    )
    assert len(vendor.calls) == 6
    assert any("budget reached" in x for x in gaps(env, "TCS"))
    assert years(env, "TCS") == {}  # TCS's /stock came back but not its statements: nothing stored


def test_cli_coverage_shows_the_vendor_years_and_source(env: Env) -> None:
    from app.data.coverage import coverage, years_summary

    setup(env, FakeVendor())
    run(env, "HDFCBANK")
    with env.session() as s:
        rows = {r.statement: r for r in coverage(s, ["HDFCBANK"])}
        summary = years_summary(s, "HDFCBANK", 3)
    for st in ("P&L", "BS", "CF"):
        assert (rows[st].earliest_fy, rows[st].latest_fy, rows[st].years) == (2015, 2026, 12)
        assert rows[st].sources == ("indianapi",) and rows[st].row().endswith("[indianapi]")
    assert summary.startswith("P&L 12 yr [indianapi], BS 12 yr [indianapi], CF 12 yr")


def test_refresh_policy() -> None:
    from datetime import timedelta

    from app.core.config import load_config
    from app.data.indianapi_store import refresh_due
    from tests.conftest import REPO_CONFIG_DIR

    cfg = load_config(REPO_CONFIG_DIR).providers.indianapi
    assert refresh_due(None, now=NOW, cfg=cfg).due
    recent = NOW - timedelta(hours=5)
    assert not refresh_due(recent, now=NOW, cfg=cfg).due
    three_days = NOW - timedelta(days=3)
    assert refresh_due(three_days, now=NOW, cfg=cfg,
                       results_since=NOW.date()).reason.startswith("results announced")  # fmt: skip
    old = NOW - timedelta(days=cfg.refresh_days + 1)
    assert refresh_due(old, now=NOW, cfg=cfg).reason == f"cache older than {cfg.refresh_days} days"
    # the Refresh button: only a cache older than a day is fetched again
    assert not refresh_due(recent, now=NOW, cfg=cfg, user=True).due
    assert "cache used" in refresh_due(recent, now=NOW, cfg=cfg, user=True).reason
    day_old = NOW - timedelta(hours=cfg.user_refresh_min_age_hours + 1)
    assert refresh_due(day_old, now=NOW, cfg=cfg, user=True).reason == "refresh requested"


def _pipeline_step(env: Env, symbol: str) -> tuple[str, str]:
    from app.db.models import PipelineRun
    from app.pipeline.runner import run_pending, start_run

    with env.session() as s:
        r, _ = start_run(s, symbol, trigger="user", force=True,
                         cfg=env.ctx.config.jobs.pipeline, now=NOW)  # fmt: skip
        assert r is not None
        s.commit()
        run_id = r.id
    run_pending(env.ctx)
    with env.session() as s:
        got = s.get(PipelineRun, run_id)
        assert got is not None
        step = next(x for x in got.steps if x["name"] == "indianapi")
    return step["status"], step["message"] or ""


def test_pipeline_step_runs_before_prices_with_years_and_quota(env: Env) -> None:
    setup(env, FakeVendor())
    status, msg = _pipeline_step(env, "HDFCBANK")
    assert status == "ok"
    assert msg.startswith("P&L 12 yr, BS 12 yr, CF 12 yr (bank model")
    assert msg.endswith("Indian API: 5/500 calls this month")


def test_pipeline_step_without_key_is_a_warning_not_a_failure(env: Env) -> None:
    setup(env, FakeVendor(), key=None)
    status, msg = _pipeline_step(env, "HDFCBANK")
    assert status == "warning" and "add indianapi_key in .env" in msg.lower()


def test_events_and_corporate_action_cross_check(env: Env) -> None:
    from datetime import UTC, datetime

    from app.db.models import Event
    from app.jobs.events import board_calendar

    setup(env, FakeVendor())
    rec = run(env, "HDFCBANK")
    assert rec.outcome.details["board_meetings"] == 26
    with env.session() as s:
        ev = s.scalars(select(Event).where(Event.exchange == "indianapi")).all()
    assert len(ev) == 26 and {e.kind.value for e in ev} == {"board_meeting"}
    # no corporate actions stored: the vendor's 1:1 bonus is reported, never written
    assert any("bonus ex 2025-08-26" in g and "not in our corporate actions" in g
               for g in gaps(env, "HDFCBANK"))  # fmt: skip
    # the results watcher's calendar reads the vendor's meetings like NSE's
    env.ctx.clock = lambda: datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    assert board_calendar(env.ctx, ["HDFCBANK"]) == [{"symbol": "HDFCBANK", "date": "2026-10-17"}]


def _filed(s: Any, iid: int, end: date, code: str, value: float, ptype: PeriodType) -> None:
    stmt = LineStatement.BS if ptype is PeriodType.INSTANT else LineStatement.PL
    s.execute(insert(FinLineItem), [{
        "instrument_id": iid, "period_end": end, "period_type": ptype, "statement": stmt,
        "basis": StatementType.CONSOLIDATED, "item_code": code, "value_inr": value,
        "unit": "per_share" if code.startswith("eps") else "amount", "version": 1,
        "source": "nse", "derived": False,
    }])  # fmt: skip


def test_reconciliation_compares_the_vendor_with_the_filing(env: Env) -> None:
    from app.db.enums import CorporateActionType
    from app.db.models import CorporateAction, ReconciliationIssue
    from app.jobs.reconcile import reconcile_symbol

    setup(env, FakeVendor())
    fy26, fy25 = date(2026, 3, 31), date(2025, 3, 31)
    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "HDFCBANK"))
        assert iid is not None
        _filed(s, iid, fy26, "pat", 76025.97e7, PeriodType.YEAR)  # = vendor
        _filed(s, iid, fy26, "eps_diluted", 49.30, PeriodType.YEAR)  # vendor 49.28
        _filed(s, iid, fy26, "deposits", 2_900_000e7, PeriodType.INSTANT)  # vendor 3,099,638
        _filed(s, iid, fy25, "pat", 70792.25e7, PeriodType.YEAR)
        _filed(s, iid, fy25, "eps_diluted", 92.40, PeriodType.YEAR)  # as filed, pre-bonus
        s.add(CorporateAction(instrument_id=iid, ex_date=date(2025, 8, 26), source="nse",
                              action_type=CorporateActionType.BONUS, ratio_old=1, ratio_new=2,
                              fetched_at=NOW))  # fmt: skip
        s.commit()
    run(env, "HDFCBANK")  # the vendor fills around the filed figures; its answers are cached
    out = reconcile_symbol(env.ctx, "HDFCBANK")
    assert "Indian API" in out["sources"], out
    with env.session() as s:
        issues = s.scalars(select(ReconciliationIssue).where(
            ReconciliationIssue.instrument_id == iid,
            ReconciliationIssue.source == "indianapi")).all()  # fmt: skip
    assert [(i.item_code, i.period_end) for i in issues] == [("deposits", fy26)]
    assert issues[0].reasons[0].startswith("Deposits (year to 31 Mar 2026, consolidated): "
                                           "Indian API ₹3,099,638.29 cr vs NSE XBRL")  # fmt: skip
    # FY2025 EPS: the vendor's 46.20 is on today's (post-bonus) basis: not compared with 92.40.
    # Without the bonus on record the two would be compared (and differ by half).
    with env.session() as s:
        s.execute(delete(CorporateAction))
        s.commit()
    reconcile_symbol(env.ctx, "HDFCBANK")
    with env.session() as s:
        codes = s.execute(select(ReconciliationIssue.item_code, ReconciliationIssue.period_end)
                          .where(ReconciliationIssue.source == "indianapi")).all()  # fmt: skip
    assert sorted(codes) == [("deposits", fy26), ("eps_diluted", fy25)]


def test_key_metric_difference_is_an_open_issue(env: Env) -> None:
    from app.db.models import ReconciliationIssue

    COMPANIES["ITC"] = ("itc", "INE154A01025", "ITC Limited", "ITC")
    try:
        setup(env, FakeVendor())
        run(env, "ITC")
    finally:
        del COMPANIES["ITC"]
    with env.session() as s:
        issues = s.scalars(select(ReconciliationIssue).where(
            ReconciliationIssue.source == "indianapi_keymetrics")).all()  # fmt: skip
    assert [(i.item_code, i.cause) for i in issues] == [("km_book_value_per_share", "key_metric")]
    assert issues[0].value_inr == pytest.approx(54.46)


def test_analyst_consensus_is_loaded_for_the_report_only(env: Env) -> None:
    from app.reports.data import load_analyst_consensus

    setup(env, FakeVendor())
    run(env, "HDFCBANK")
    with env.session() as s:
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "HDFCBANK"))
        assert iid is not None
        got = load_analyst_consensus(s, iid)
    assert got is not None and got["recommendations"] == 40 and got["as_of"] == NOW.date()
    assert got["mean_rating"] == pytest.approx(1.525)
