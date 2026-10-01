"""Backtests (SPEC §11): simulation arithmetic (hand-computed), point-in-time slicing (no
look-ahead), the runner end to end on synthetic companies, the worker job and the API."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest import runner as runner_module
from app.backtest.engine import (
    Costs,
    benchmark_equity,
    cagr,
    max_drawdown,
    metrics,
    sample,
    simulate,
)
from app.backtest.pit import (
    PitStock,
    PitWorld,
    announced_by,
    month_starts,
    on_asm_gsm_at,
    stock_data_at,
    universe_at,
)
from app.backtest.runner import load_world, run_backtest
from app.core.config import JobName, load_config
from app.db.enums import BacktestStatus
from app.db.models import Backtest
from app.jobs.registry import REGISTRY
from app.jobs.runner import run_job
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env
from tests.report_support import seed_company, seed_index

CFG = load_config(REPO_CONFIG_DIR)
DAYS = pd.bdate_range("2024-01-01", periods=6)


def frame(**cols: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {k: [np.nan if v is None else v for v in vals] for k, vals in cols.items()}, index=DAYS
    )


# ───────────────────────── engine ─────────────────────────


def test_single_trade_hand_computed() -> None:
    # Buy A one session after the d0 signal at 100 with 0.2% cost: 0.998 shares (NAV 99.8).
    # Held 2 sessions, sold at 121 with 0.2% cost: 0.998 x 121 x 0.998 = 120.516484.
    closes = frame(A=[100, 100, 110, 121, 121, 121])
    r = simulate(closes, {DAYS[0]: ["A"]}, holding_days=2, costs=Costs(0.002, 0.002), lag=1)
    assert r.equity.round(6).tolist() == [100, 99.8, 109.78, 120.516484, 120.516484, 120.516484]
    [t] = r.trades
    assert (t.entry_date, t.exit_date, t.days_held, t.closed_by) == (
        DAYS[1],
        DAYS[3],
        2,
        "holding_period",
    )
    assert t.net_return == pytest.approx(0.20516484)
    m = metrics(r)
    assert m.hit_rate == 1.0 and m.avg_holding_days == 2 and m.trades == 1
    assert m.max_drawdown == pytest.approx(-0.002)  # 100 → 99.8
    assert m.total_return == pytest.approx(0.20516484)
    assert m.exposure == pytest.approx(2 / 6)  # invested on d1, d2 only


def test_no_reweighting_and_cash_gates_new_entries() -> None:
    # d0: A and B get 50 each (no costs). A doubles on d1 → NAV 150 with zero cash, so the C
    # signal on d2 cannot be funded (existing positions are never trimmed). Both exit on d3
    # (held 3 sessions); C signalled again on d4 then gets all the cash.
    closes = frame(A=[100, 200, 200, 200, 200, 200], B=[10] * 6, C=[5] * 6)
    r = simulate(
        closes,
        {DAYS[0]: ["A", "B"], DAYS[2]: ["C"], DAYS[4]: ["C"]},
        holding_days=3,
        costs=Costs(0, 0),
        lag=0,
    )
    assert [t.symbol for t in r.trades] == ["A", "B", "C"]
    assert r.equity.tolist() == [100, 150, 150, 150, 150, 150]
    c = r.trades[-1]
    assert c.entry_date == DAYS[4] and c.closed_by == "backtest_end"


def test_delisting_and_backtest_end_exits() -> None:
    # D stops trading after d1: sold at its last close (10) once the data has ended.
    closes = frame(D=[10, 10, None, None, None, None], E=[20, 20, 20, 20, 20, 22])
    r = simulate(closes, {DAYS[0]: ["D", "E"]}, holding_days=50, costs=Costs(0, 0), lag=0)
    by = {t.symbol: t for t in r.trades}
    assert (
        by["D"].closed_by == "data_end"
        and by["D"].exit_date == DAYS[2]
        and by["D"].exit_price == 10
    )
    assert by["E"].closed_by == "backtest_end" and by["E"].net_return == pytest.approx(0.1)
    assert r.equity.iloc[-1] == pytest.approx(50 + 55)  # 50 back from D, E 2.5 shares x 22


def test_signal_on_a_holiday_executes_next_session() -> None:
    closes = frame(A=[100] * 6)
    r = simulate(
        closes, {pd.Timestamp("2023-12-31"): ["A"]}, holding_days=2, costs=Costs(0, 0), lag=0
    )
    assert r.trades[0].entry_date == DAYS[0]


def test_cagr_drawdown_benchmark_and_sampling() -> None:
    eq = pd.Series([100.0, 121.0], index=pd.DatetimeIndex(["2020-01-01", "2022-01-01"]))
    assert cagr(eq) == pytest.approx(1.21 ** (365.25 / 731) - 1)  # ≈ 10% a year
    assert max_drawdown(pd.Series([100.0, 120, 90, 130])) == pytest.approx(90 / 120 - 1)
    bench = benchmark_equity(pd.Series([50.0, 55.0], index=DAYS[:2]), DAYS[:3])
    assert bench.tolist() == pytest.approx([100.0, 110.0, 110.0])  # rebased, forward-filled
    daily = pd.Series(range(10), index=pd.bdate_range("2024-01-01", periods=10), dtype=float)
    w = sample(daily, "weekly")
    # Mon 1 Jan (first point), Fri 5 Jan, Fri 12 Jan (last); all real sessions
    assert w.index[0] == daily.index[0] and w.index[-1] == daily.index[-1] and len(w) == 3


# ───────────────────────── point in time ─────────────────────────


def test_announced_by_excludes_unknown_and_future_dates() -> None:
    f = pd.DataFrame(
        {"announcement_date": [date(2023, 5, 10), None, date(2024, 5, 15)], "x": [1, 2, 3]}
    )
    assert announced_by(f, pd.Timestamp("2024-05-14"))["x"].tolist() == [1]
    assert announced_by(f, pd.Timestamp("2024-05-15"))["x"].tolist() == [1, 3]


def test_universe_and_surveillance_at_a_date() -> None:
    world = PitWorld(
        stocks={s: None for s in ("A", "B", "C")},  # type: ignore[misc]
        benchmark_close=None,
        membership=pd.DataFrame(
            {
                "symbol": ["A", "B", "C"],
                "effective_from": pd.to_datetime(["2020-01-01", "2022-01-01", "2020-01-01"]),
                "effective_to": pd.to_datetime([None, None, "2021-06-30"]),
            }
        ),
        surveillance=pd.DataFrame(
            {
                "symbol": ["A"],
                "effective_from": pd.to_datetime(["2021-01-01"]),
                "effective_to": pd.to_datetime(["2021-03-31"]),
            }
        ),
    )
    assert universe_at(world, pd.Timestamp("2021-01-01")) == ["A", "C"]  # C left mid-2021
    assert universe_at(world, pd.Timestamp("2023-01-01")) == ["A", "B"]
    assert on_asm_gsm_at(world, "A", pd.Timestamp("2021-02-01")) is True
    assert on_asm_gsm_at(world, "A", pd.Timestamp("2021-04-01")) is False
    world.surveillance = None
    assert on_asm_gsm_at(world, "A", pd.Timestamp("2021-02-01")) is None  # unknown, not clean


def test_month_starts() -> None:
    sessions = pd.bdate_range("2024-01-01", "2024-03-31")
    assert [d.date() for d in month_starts(sessions, date(2024, 1, 15), date(2024, 3, 31))] == [
        date(2024, 1, 15),
        date(2024, 2, 1),
        date(2024, 3, 1),
    ]


# ───────────────────────── runner ─────────────────────────


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    for i, (sym, pe) in enumerate((("AAA", 12.0), ("BBB", 25.0), ("CCC", 40.0))):
        seed_company(db, sym, pe=pe, seed=30 + i)
    return db


def test_runner_is_point_in_time(seeded: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    world, label = load_world(seeded, CFG, symbols=["AAA", "BBB", "CCC"])
    assert label == "NIFTY500 (price index; TRI not stored)"
    seen: list[tuple[pd.Timestamp, Any]] = []
    real = runner_module.build_report

    def spy(data, config, *, lite=False):  # type: ignore[no-untyped-def]
        seen.append((data.daily.index.max(), data))
        return real(data, config, lite=lite)

    monkeypatch.setattr(runner_module, "build_report", spy)
    res = run_backtest(
        world,
        CFG,
        grades=["A_plus", "A", "B", "C", "D"],
        zones=["deep_discount", "discount", "fair", "premium", "extreme_premium"],
        holding_days=20,
        start=date(2024, 1, 1),
        end=date(2024, 6, 14),
        symbols=["AAA", "BBB", "CCC"],
        benchmark_label=label,
    )
    # Every report saw only what was known at its rebalance date.
    for last_price, data in seen:
        cutoff = last_price
        assert (pd.to_datetime(data.annual["announcement_date"]) <= cutoff).all()
        assert (pd.to_datetime(data.quarterly["announcement_date"]) <= cutoff).all()
        assert data.benchmark_close.index.max() <= cutoff
        assert data.overrides.as_dict() == {}
    feb = next(d for p, d in seen if p.month == 2 and d.symbol == "AAA")
    assert feb.annual["fiscal_year"].max() == 2023  # FY2024 is announced 15 May 2024
    may = next(d for p, d in seen if p.month == 6 and d.symbol == "AAA")
    assert may.annual["fiscal_year"].max() == 2024

    assert res["period"]["rebalances"] == 6 and res["universe"]["source"] == "symbols"
    assert len(res["cells"]) == 25
    assert sum(c["signals"] for c in res["cells"]) == 18  # 3 stocks x 6 months, all graded
    assert res["portfolio"]["trades"] > 0 and res["equity"][0]["portfolio"] == 100.0
    assert res["equity"][0]["benchmark"] == 100.0
    assert any("survivorship" in c for c in res["caveats"])


def test_runner_rejects_an_empty_period(seeded: Session) -> None:
    world, label = load_world(seeded, CFG, symbols=["AAA"])
    with pytest.raises(ValueError, match="no trading sessions"):
        run_backtest(
            world,
            CFG,
            grades=["A"],
            zones=["fair"],
            holding_days=20,
            start=date(2030, 1, 1),
            end=date(2030, 6, 1),
            symbols=["AAA"],
            benchmark_label=label,
        )


def test_empty_universe_and_short_history_are_caveated(seeded: Session) -> None:
    world, label = load_world(seeded, CFG, symbols=None)  # no membership rows stored
    assert world.stocks == {}
    res = run_backtest(
        world,
        CFG,
        grades=["A"],
        zones=["fair"],
        holding_days=20,
        start=date(2000, 1, 1),
        end=date(2030, 1, 1),
        symbols=None,
        benchmark_label=label,
    )
    assert res["universe"] == {"source": "index_membership", "avg_size": 0.0, "stocks_with_data": 0}
    assert res["portfolio"]["trades"] == 0
    caveats = " ".join(res["caveats"])
    assert "membership is stored, so the universe is empty" in caveats
    assert "not the requested 2000-01-01 to 2030-01-01" in caveats


def test_stock_without_prices_before_the_date_is_skipped(seeded: Session) -> None:
    world, _ = load_world(seeded, CFG, symbols=["AAA"])
    assert (
        stock_data_at(world, "AAA", pd.Timestamp("2019-01-01"), peers=[], rs_percentile=None)
        is None
    )
    assert isinstance(world.stocks["AAA"], PitStock)


# ───────────────────────── job + API ─────────────────────────


def test_backtests_job_runs_queued_requests(env: Env) -> None:
    with env.session() as s:
        seed_index(s)
        for i, sym in enumerate(("AAA", "BBB")):
            seed_company(s, sym, seed=40 + i)
        params = {
            "grades": ["A_plus", "A", "B"],
            "zones": ["discount", "fair", "premium"],
            "holding_days": 20,
            "start": "2024-03-01",
            "end": "2024-06-14",
            "symbols": ["AAA", "BBB"],
        }
        good = Backtest(status=BacktestStatus.QUEUED, params=params)
        future = Backtest(
            status=BacktestStatus.QUEUED,
            params={**params, "start": "2031-01-01", "end": "2031-06-01"},
        )
        s.add_all([good, future])
        s.commit()
        ids = good.id, future.id
    rec = run_job(REGISTRY[JobName.BACKTESTS], env.ctx)
    assert rec.outcome is not None and rec.outcome.details["done"] == [ids[0]]
    assert "no trading sessions" in rec.outcome.details["failed"][ids[1]]
    with env.session() as s:
        ok, bad = s.scalars(select(Backtest).order_by(Backtest.id)).all()
        assert ok.status == BacktestStatus.DONE and ok.results is not None
        assert ok.results["period"]["rebalances"] == 4 and len(ok.results["cells"]) == 25
        assert any("survivorship" in c for c in ok.results["caveats"])
        assert bad.status == BacktestStatus.FAILED and "no trading sessions" in (bad.error or "")
    again = run_job(REGISTRY[JobName.BACKTESTS], env.ctx)
    assert again.outcome is not None and again.outcome.skipped_reason == "no queued backtests"


def test_weekly_sample_uses_last_real_session_of_holiday_weeks() -> None:
    days = pd.DatetimeIndex(["2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28", "2024-04-01"])
    s = pd.Series([1.0, 2, 3, 4, 5], index=days)  # Good Friday 29 Mar 2024 is a holiday
    w = sample(s, "weekly")
    assert list(w.index) == [days[0], days[3], days[4]] and w.tolist() == [1.0, 4.0, 5.0]
    assert sample(s, "daily") is s
