"""NSE bhavcopy history builder (SPEC v0.2 §3.2: the price fallback Fyers → NSE bhavcopy →
yfinance): parsing, on-demand recent days, holidays, the budget that makes the router move
on, delivery from the stored file, former symbols, the backfill job, and the chain end to end
through eod_prices with the broker failing."""

from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
import responses
from sqlalchemy import select

from app.core.config import JobName, Provider
from app.data.bhavcopy_store import BhavcopyStore
from app.data.gaps import DbGapRecorder
from app.data.providers.base import PriceProvider, ProviderUnavailable
from app.data.providers.nse import NseProvider, NseSession, parse_bhavcopy_ohlcv
from app.data.router import DataRouter, Outcome
from app.db.enums import AliasKind
from app.db.models import BhavcopyDay, PriceDaily, Symbol, SymbolAlias
from app.jobs.common import ensure_instruments
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobContext, JobOptions, run_job
from tests.jobs_support import Env, NoLimit

FIX = (Path(__file__).parent / "fixtures" / "nse" / "sec_bhavdata_full_28032024.csv").read_text()
TODAY = date(2024, 6, 14)  # Friday


def bhav(day: date, bump: float = 0.0) -> str:
    """The fixture file re-dated to ``day`` with SAMPLEIND's close moved by ``bump``."""
    text = FIX.replace("28-Mar-2024", day.strftime("%d-%b-%Y"))
    return text.replace("1450.00, 1449.80", f"1450.00, {1449.80 + bump:.2f}")


def url(env: Env, day: date) -> str:
    base = env.ctx.config.providers.nse.archives_url
    return f"{base}/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv"


def provider(env: Env) -> NseProvider:
    cfg = env.ctx.config.providers
    return NseProvider(cfg.nse, NseSession(cfg.nse, clock=lambda: 0.0), env.ctx.raw_store,
                       history=BhavcopyStore(env.Session), bhavcopy=cfg.bhavcopy,
                       today=lambda: TODAY)  # fmt: skip


def test_parse_ohlcv() -> None:
    df = parse_bhavcopy_ohlcv(FIX, ["EQ", "BE"])
    assert list(df["symbol"]) == ["20MICRONS", "SAMPLEIND", "M&M", "SMALLCO", "NEWLIST"]
    s = df.set_index("symbol").loc["SAMPLEIND"]
    assert (s["open"], s["high"], s["low"], s["close"]) == (1442.0, 1461.5, 1436.2, 1449.8)
    assert s["volume"] == 1_250_000 and s["traded_value_cr"] == pytest.approx(181.09)
    assert s["deliverable_qty"] == 687_500 and s["date"] == date(2024, 3, 28)
    assert df.set_index("symbol").loc["NEWLIST"]["delivery_pct"] is None  # "-", not 0


@responses.activate
def test_on_demand_days_holiday_and_cache(env: Env) -> None:
    p = provider(env)
    assert isinstance(p, PriceProvider)
    for d, bump in ((date(2024, 6, 10), 0), (date(2024, 6, 12), 5), (date(2024, 6, 13), 10)):
        responses.add(responses.GET, url(env, d), body=bhav(d, bump))
    responses.add(responses.GET, url(env, date(2024, 6, 11)), status=404)  # holiday
    responses.add(responses.GET, url(env, TODAY), status=404)  # not out yet this morning

    df = p.daily_ohlcv("sampleind", date(2024, 6, 8), date(2024, 6, 20))
    assert [d.date() for d in df.index] == [date(2024, 6, 10), date(2024, 6, 12),
                                            date(2024, 6, 13)]  # fmt: skip
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert list(df["close"]) == [1449.8, 1454.8, 1459.8]
    assert df.attrs["warnings"] == ["no bhavcopy yet for 2024-06-14"]
    with env.session() as s:
        days = dict(s.execute(select(BhavcopyDay.trade_date, BhavcopyDay.status)).all())
    assert days == {date(2024, 6, 10): "loaded", date(2024, 6, 11): "holiday",
                    date(2024, 6, 12): "loaded", date(2024, 6, 13): "loaded"}  # fmt: skip
    assert (env.ctx.raw_store.root / "nse/2024/06/14/sec_bhavdata_full_10062024.csv").is_file()

    calls = len(responses.calls)
    again = p.daily_ohlcv("20MICRONS", date(2024, 6, 10), date(2024, 6, 13))
    assert len(responses.calls) == calls  # every symbol came with the same files
    assert list(again["close"]) == [186.95] * 3


@responses.activate
def test_history_not_built_moves_the_router_on(env: Env) -> None:
    with pytest.raises(ProviderUnavailable, match=r"lacks .* trading days from 2024-01-01"):
        provider(env).daily_ohlcv("SAMPLEIND", date(2024, 1, 1), TODAY)
    assert len(responses.calls) == 0


@responses.activate
def test_delivery_from_the_stored_file(env: Env) -> None:
    d = date(2024, 6, 13)
    responses.add(responses.GET, url(env, d), body=bhav(d))
    p = provider(env)
    df = p.delivery(d)
    row = df.set_index("symbol").loc["SAMPLEIND"]
    assert row["delivery_pct"] == 55.0 and row["traded_qty"] == 1_250_000
    assert len(df) == 5
    assert len(p.delivery(d)) == 5 and len(responses.calls) == 1  # served from the store


@responses.activate
def test_former_symbol_history(env: Env) -> None:
    old, new = date(2024, 6, 12), date(2024, 6, 13)
    responses.add(responses.GET, url(env, old), body=bhav(old).replace("SAMPLEIND", "OLDSAMPLE"))
    responses.add(responses.GET, url(env, new), body=bhav(new))
    with env.session() as s:
        iid = ensure_instruments(s, ["SAMPLEIND"])["SAMPLEIND"]
        sym = Symbol(isin="INE000SAMP01", name="Sample", nse_symbol="SAMPLEIND",
                     instrument_id=iid, status="active", sources=["nse"])  # fmt: skip
        s.add(sym)
        s.flush()
        s.add(SymbolAlias(symbol_id=sym.id, alias="OLDSAMPLE", kind=AliasKind.FORMER_SYMBOL,
                          source="nse"))  # fmt: skip
        s.commit()
    df = provider(env).daily_ohlcv("SAMPLEIND", old, new)
    assert [d.date() for d in df.index] == [old, new]


@responses.activate
def test_backfill_job_and_chain_through_eod_prices(env: Env) -> None:
    cfg = env.ctx.config
    nse = provider(env)
    router = DataRouter({Provider.FYERS: env.prices, Provider.NSE: nse}, cfg.providers,
                        limiter=NoLimit(), gaps=DbGapRecorder(env.Session),
                        clock=lambda: datetime(2024, 6, 14, 13, 0, tzinfo=UTC),
                        sleep=lambda _: None)  # fmt: skip
    ctx = JobContext(cfg, env.Session, router, env.ctx.redis, env.ctx.gaps,
                     clock=env.ctx.clock, raw_store=env.ctx.raw_store)  # fmt: skip
    small = cfg.providers.bhavcopy.model_copy(update={"backfill_days_per_run": 3})
    nse._bhav = small
    ctx.config = cfg.model_copy(update={"providers": cfg.providers.model_copy(
        update={"bhavcopy": small})})  # fmt: skip
    for d in pd.bdate_range("2024-05-27", "2024-06-13"):
        responses.add(responses.GET, url(env, d.date()), body=bhav(d.date()))

    rec = run_job(REGISTRY[JobName.BHAVCOPY_HISTORY], ctx)
    assert rec.outcome is not None
    d = rec.outcome.details
    assert d["loaded"] == 3 and d["oldest_loaded"] == "2024-06-11"  # newest first, budgeted
    assert d["remaining"] == d["missing_before"] - 3

    # Fyers has no bars for SAMPLEIND (EMPTY), Kite is disabled → NSE bhavcopy serves them
    res = router.daily_ohlcv("SAMPLEIND", date(2024, 6, 11), date(2024, 6, 13))
    assert res.source is Provider.NSE
    kinds = [(a.provider, a.outcome) for a in res.attempts]
    assert kinds[:2] == [(Provider.FYERS, Outcome.EMPTY), (Provider.KITE, Outcome.NOT_CONFIGURED)]
    # a new stock's first fetch covers history_years: NSE declines until the history is built
    responses.add(responses.GET, url(env, TODAY), status=404)
    first = run_job(REGISTRY[JobName.EOD_PRICES], ctx, JobOptions(symbols=("SAMPLEIND",)))
    assert first.outcome is not None and "SAMPLEIND" in first.outcome.details["failed"]
    # with a stored bar the incremental window is short: the missing days are fetched now
    with env.session() as s:
        iid = ensure_instruments(s, ["SAMPLEIND"])["SAMPLEIND"]
        s.add(PriceDaily(instrument_id=iid, date=date(2024, 6, 7), open=1, high=1, low=1,
                         close=1449.8, volume=1, source="fyers"))  # fmt: skip
        s.commit()
    rec = run_job(REGISTRY[JobName.EOD_PRICES], ctx, JobOptions(symbols=("SAMPLEIND",)))
    assert rec.outcome is not None and rec.outcome.details["failed"] == []
    with env.session() as s:
        by_source = dict(s.execute(select(PriceDaily.date, PriceDaily.source)).all())
    assert by_source[date(2024, 6, 13)] == "nse" and by_source[date(2024, 6, 3)] == "nse"
