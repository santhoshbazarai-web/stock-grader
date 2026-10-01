"""The offline exchange (P25): deterministic synthetic filings and prices that the real
pipeline turns into a full report with 10+ years of exchange-XBRL fundamentals, offline."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Dataset, Provider, load_config
from app.data.coverage import coverage_grid
from app.data.gaps import DbGapRecorder
from app.data.providers.base import ProviderUnavailable
from app.data.router import DataRouter
from app.data.xbrl import parse_results
from app.db.enums import PipelineStatus, StatementType
from app.db.models import FinLineItem, Instrument, ResultFiling
from app.devtools.offline_exchange import (
    BY_SYMBOL,
    COMPANIES,
    HOST,
    OfflineExchange,
    filings,
    offline_providers,
    offline_router_config,
    seed,
    xbrl_document,
)
from app.jobs.runner import JobContext
from app.pipeline.runner import run_pending, start_run
from app.reports.service import latest_report
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, NoLimit

CFG = load_config(REPO_CONFIG_DIR)
NOW = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)


def test_filings_are_deterministic_and_parse() -> None:
    c = BY_SYMBOL["OFFIT"]
    listing = filings(c, NOW)
    # Q1 FY2014 .. Q1 FY2027 (Jun 2026 results are out by Aug); Sep 2026 is not yet filed
    assert listing[0]["period_end"] == date(2013, 6, 30)
    assert listing[-1]["period_end"] == date(2026, 6, 30)
    assert all(r["disseminated_at"] <= NOW for r in listing)
    assert xbrl_document(c, date(2020, 3, 31)) == xbrl_document(c, date(2020, 3, 31))
    q4 = parse_results(xbrl_document(c, date(2020, 3, 31)), CFG.providers.nse.results)
    assert q4.annual is not None and q4.annual["revenue"] and q4.annual["total_equity"]
    assert q4.annual["cfo"] and q4.quarter is not None


def test_bank_equity_from_capital_and_reserves() -> None:
    bank = parse_results(xbrl_document(BY_SYMBOL["OFFBANK"], date(2024, 3, 31)),
                         CFG.providers.nse.results)  # fmt: skip
    assert bank.is_bank and bank.annual is not None
    # xbrl_map v3: banking results give Capital + Reserves, not a total
    assert bank.annual["total_equity"] == pytest.approx((10e9 + 590e9) * 0.5 / 1e7, rel=0.04)


def test_provider_refuses_what_it_does_not_have() -> None:
    ex = OfflineExchange(Provider.OFFLINE, clock=lambda: NOW)
    with pytest.raises(ProviderUnavailable):
        ex.results_filings("HDFCBANK")
    with pytest.raises(ProviderUnavailable):
        ex.results_document("https://nsearchives.nseindia.com/some/real.xml")
    with pytest.raises(ProviderUnavailable, match="not filed yet"):
        ex.results_document(f"{HOST}/OFFIT/OFFIT_20260930_consolidated.xml")
    bars = ex.daily_ohlcv("OFFIT", date(2026, 9, 1), date(2026, 12, 31))
    assert bars.index.max().date() <= NOW.date() and len(bars) > 15


def test_offline_routing_uses_only_the_offline_exchange() -> None:
    routed = offline_router_config(CFG.providers)
    assert set(routed.priority) == set(CFG.providers.priority)
    assert all(p == [Provider.OFFLINE] for p in routed.priority.values())
    assert CFG.providers.priority[Dataset.DAILY_OHLCV][0] is Provider.FYERS  # original untouched
    assert set(offline_providers()) == {Provider.OFFLINE}
    assert HOST.endswith(".invalid")  # synthetic documents never point at a real host


@pytest.mark.parametrize("symbol", ["OFFIT", "OFFBANK"])
def test_pipeline_builds_a_full_report_offline(env: Env, symbol: str) -> None:
    providers = offline_providers(clock=lambda: NOW)
    router = DataRouter(providers, offline_router_config(env.ctx.config.providers),
                        limiter=NoLimit(),
                        gaps=DbGapRecorder(env.Session), clock=lambda: NOW,
                        sleep=lambda _: None)  # fmt: skip
    ctx = JobContext(env.ctx.config, env.Session, router, env.ctx.redis, env.ctx.gaps,
                     clock=lambda: NOW, raw_store=env.ctx.raw_store)  # fmt: skip
    with env.session() as s:
        assert seed(s, env.ctx.config.jobs.universe_index) == [c.symbol for c in COMPANIES]
        run, _ = start_run(s, symbol, trigger="user", force=True, cfg=ctx.config.jobs.pipeline,
                           now=NOW)  # fmt: skip
        s.commit()
    assert run is not None
    cfg = ctx.config.jobs.pipeline.model_copy(update={"max_xbrl_downloads": 100})
    ctx.config = ctx.config.model_copy(update={"jobs": ctx.config.jobs.model_copy(
        update={"pipeline": cfg})})  # fmt: skip
    assert run_pending(ctx) == [(run.id, PipelineStatus.DONE)]
    with env.session() as s:
        report = latest_report(s, symbol)
        assert report is not None
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == symbol))
        grid = coverage_grid(s, iid, list(range(2017, 2027)), 3)  # as the stock page
    lv = report.levels
    assert lv.baseline and lv.fair_value and lv.top_band
    assert report.zone and report.grade and report.action
    assert report.sources["fundamentals"] == "offline"
    assert report.sources["prices"] == "offline"
    cons = next(g for g in grid if g.basis is StatementType.CONSOLIDATED)
    pl = [c for c in cons.cells if c.statement == "P&L"]
    assert len(pl) == 10 and all("xbrl" in c.sources for c in pl)
    with env.session() as s:
        exchanges = set(s.scalars(select(ResultFiling.exchange).where(
            ResultFiling.instrument_id == iid)))  # fmt: skip
        line_sources = set(s.scalars(select(FinLineItem.source).where(
            FinLineItem.instrument_id == iid)))  # fmt: skip
    assert exchanges == {"offline"}  # never ledgered as NSE filings
    assert "nse_xbrl" not in line_sources and "offline_xbrl" in line_sources
    assert Path(env.ctx.raw_store.root).exists()  # every XBRL document was cached first
