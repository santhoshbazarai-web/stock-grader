"""Run a backtest (SPEC §11) from stored history. Loads everything once, then per monthly
rebalance date builds every universe stock's report point-in-time (``pit.py`` +
``reports.build``, lite) and records its grade x zone. The simulator (``engine.py``) then runs
the selected rules and each of the 25 grade x zone cells against the Nifty 500.

Cost note: one report per stock per month. A 500-stock, 10-year run is ~60k report builds
(tens of minutes in the worker); relative valuation uses the previous rebalance's peer figures
(one month stale) so each report is built once.
"""

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtest.engine import (
    Costs,
    Metrics,
    SimResult,
    benchmark_equity,
    cagr,
    max_drawdown,
    metrics,
    sample,
    simulate,
)
from app.backtest.pit import (
    LINE_ITEM_COLUMNS,
    PitStock,
    PitWorld,
    assume_availability,
    month_starts,
    rs_percentiles_at,
    stock_data_at,
    universe_at,
    versioned_frame,
)
from app.core.config import AppConfig
from app.data import prices
from app.db.enums import StatementType, SurveillanceList
from app.db.models import (
    DeliveryDaily,
    FinAnnual,
    FinLineItem,
    FinQuarterly,
    IndexMembership,
    Instrument,
    Shareholding,
    SurveillanceFlag,
)
from app.fundamentals.xbrl_map import get_xbrl_map
from app.reports.build import build_report
from app.reports.data import SHP_COLUMNS, load_financials
from app.reports.dto import PeerStats

logger = logging.getLogger(__name__)

GRADES = ("A_plus", "A", "B", "C", "D")
ZONES = ("deep_discount", "discount", "fair", "premium", "extreme_premium")
ASM_GSM = (SurveillanceList.ASM_LT, SurveillanceList.ASM_ST, SurveillanceList.GSM)
Progress = Callable[[int, int], None]


# ───────────────────────── loading ─────────────────────────


def _index_symbol(config: AppConfig) -> str:
    return config.jobs.universe_index


def _benchmark(session: Session, config: AppConfig) -> tuple[pd.Series | None, str]:
    bt = config.jobs.backtest
    if bt.benchmark_tri:
        tri = prices.close_series(session, bt.benchmark_tri)
        if tri is not None and not tri.empty:
            return tri, f"{bt.benchmark_tri} (total return)"
    close = prices.close_series(session, bt.benchmark)
    return close, f"{bt.benchmark} (price index; TRI not stored)"


def _stock(session: Session, inst: Instrument, config: AppConfig) -> PitStock | None:
    try:
        daily = prices.adjusted_daily(session, inst.symbol)
    except (prices.NoPriceData, prices.UnadjustedPrices):
        return None
    annual, basis, _ = load_financials(session, FinAnnual, inst.id, "fin_annual")
    quarterly, _, _ = load_financials(session, FinQuarterly, inst.id, "fin_quarterly")
    if basis is not None:  # restatements: each period as known at each date (fin_line_items)
        items = _line_items(session, inst.id, StatementType(basis))
        xmap = get_xbrl_map()
        annual = versioned_frame(annual, items, "fin_annual", xmap)
        quarterly = versioned_frame(quarterly, items, "fin_quarterly", xmap)
    lag = config.jobs.backtest.fundamentals_availability_lag_days
    annual, assumed_a = assume_availability(annual, lag.annual)
    quarterly, assumed_q = assume_availability(quarterly, lag.quarterly)
    shp_rows = session.execute(
        select(
            Shareholding.period_end,
            Shareholding.filing_date,
            *[getattr(Shareholding, c) for c in SHP_COLUMNS],
        )
        .where(Shareholding.instrument_id == inst.id)
        .order_by(Shareholding.period_end)
    ).all()
    shp = pd.DataFrame(
        [r[1:] for r in shp_rows],
        index=pd.DatetimeIndex([pd.Timestamp(r[0]) for r in shp_rows], name="period_end"),
        columns=["filing_date", *SHP_COLUMNS],
    )
    dv = session.execute(
        select(DeliveryDaily.date, DeliveryDaily.delivery_pct, DeliveryDaily.traded_value_cr)
        .where(DeliveryDaily.instrument_id == inst.id)
        .order_by(DeliveryDaily.date)
    ).all()
    idx = pd.to_datetime([r[0] for r in dv])
    return PitStock(
        symbol=inst.symbol,
        name=inst.name,
        sector=inst.sector,
        daily=daily,
        annual=annual,
        quarterly=quarterly,
        statement_type=basis,
        shareholding=shp,
        assumed_annual=assumed_a,
        assumed_quarterly=assumed_q,
        delivery_pct=pd.Series([r[1] for r in dv], index=idx, dtype=float).dropna() if dv else None,
        traded_value_cr=pd.Series([r[2] for r in dv], index=idx, dtype=float).dropna()
        if dv
        else None,
    )


def _line_items(session: Session, iid: int, basis: StatementType) -> pd.DataFrame:
    rows = session.execute(
        select(*[getattr(FinLineItem, c) for c in LINE_ITEM_COLUMNS]).where(
            FinLineItem.instrument_id == iid, FinLineItem.basis == basis
        )
    ).all()
    df = pd.DataFrame([tuple(r) for r in rows], columns=LINE_ITEM_COLUMNS)
    df["period_type"] = [str(getattr(v, "value", v)) for v in df["period_type"]]
    return df


def load_world(
    session: Session, config: AppConfig, *, symbols: list[str] | None
) -> tuple[PitWorld, str]:
    index = _index_symbol(config)
    membership = None
    if symbols:
        insts = session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols))).all()
    else:
        rows = session.execute(
            select(Instrument.symbol, IndexMembership.effective_from, IndexMembership.effective_to)
            .join(Instrument, Instrument.id == IndexMembership.instrument_id)
            .where(IndexMembership.index_name == index)
        ).all()
        membership = pd.DataFrame(rows, columns=["symbol", "effective_from", "effective_to"])
        membership["effective_from"] = pd.to_datetime(membership["effective_from"])
        membership["effective_to"] = pd.to_datetime(membership["effective_to"])
        # Delisted / inactive instruments stay in: they were members then.
        insts = session.scalars(
            select(Instrument).where(Instrument.symbol.in_(set(membership["symbol"])))
        ).all()
    stocks: dict[str, PitStock] = {}
    notes: list[str] = []
    for inst in insts:
        st = _stock(session, inst, config)
        if st is None:
            notes.append(f"{inst.symbol}: no adjusted prices stored; excluded")
            continue
        stocks[inst.symbol] = st
    surv_count = session.scalar(select(func.count()).select_from(SurveillanceFlag)) or 0
    surveillance = None
    if surv_count:
        srows = session.execute(
            select(
                Instrument.symbol, SurveillanceFlag.effective_from, SurveillanceFlag.effective_to
            )
            .join(Instrument, Instrument.id == SurveillanceFlag.instrument_id)
            .where(SurveillanceFlag.list_name.in_(ASM_GSM))
        ).all()
        surveillance = pd.DataFrame(srows, columns=["symbol", "effective_from", "effective_to"])
        for c in ("effective_from", "effective_to"):
            surveillance[c] = pd.to_datetime(surveillance[c])
    bench, label = _benchmark(session, config)
    return PitWorld(stocks, bench, membership, surveillance, notes), label


# ───────────────────────── running ─────────────────────────


def _metrics_dict(m: Metrics) -> dict[str, Any]:
    return {
        "cagr": m.cagr,
        "max_drawdown": m.max_drawdown,
        "hit_rate": m.hit_rate,
        "avg_holding_days": m.avg_holding_days,
        "avg_trade_return": m.avg_trade_return,
        "trades": m.trades,
        "exposure": m.exposure,
        "total_return": m.total_return,
    }


def _excluded_rows(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int(pd.to_datetime(frame[column], errors="coerce").isna().sum())


def run_backtest(
    world: PitWorld,
    config: AppConfig,
    *,
    grades: list[str],
    zones: list[str],
    holding_days: int,
    start: date,
    end: date,
    symbols: list[str] | None,
    benchmark_label: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Pure given ``world``: everything is computed from the loaded history."""
    bt = config.jobs.backtest
    if world.benchmark_close is not None and not world.benchmark_close.empty:
        sessions = pd.DatetimeIndex(world.benchmark_close.index)
    else:
        sessions = pd.DatetimeIndex(
            sorted({d for s in world.stocks.values() for d in s.daily.index})
        )
    rebalances = month_starts(sessions, start, end)
    if not rebalances:
        raise ValueError("no trading sessions between start and end (are prices stored?)")

    cells: dict[tuple[str, str], dict[pd.Timestamp, list[str]]] = defaultdict(dict)
    peers: dict[str, list[PeerStats]] = {}
    universe_sizes: list[int] = []
    failures: dict[str, str] = {}
    for k, d in enumerate(rebalances):
        uni = universe_at(world, d, symbols)
        universe_sizes.append(len(uni))
        rs = rs_percentiles_at(world, uni, d, config.technical)
        next_peers: dict[str, list[PeerStats]] = defaultdict(list)
        for sym in uni:
            stock = world.stocks[sym]
            key = stock.sector if stock.sector in config.sectors.root else "default"
            data = stock_data_at(
                world, sym, d, peers=peers.get(key or "default", []), rs_percentile=rs.get(sym)
            )
            if data is None:
                continue
            try:
                built = build_report(data, config, lite=True)
            except Exception as exc:  # one stock-month must not sink the run
                failures[f"{sym}@{d.date()}"] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            next_peers[built.run.peer.sector].append(built.run.peer)
            r = built.report
            if r.grade and r.zone:
                cells[(r.grade, r.zone)].setdefault(d, []).append(sym)
        peers = next_peers
        if progress:
            progress(k + 1, len(rebalances))

    window = sessions[(sessions >= rebalances[0]) & (sessions <= pd.Timestamp(end))]
    closes = pd.DataFrame(
        {s: st.daily["close"].reindex(window) for s, st in world.stocks.items()}, index=window
    )
    costs = Costs(buy=bt.cost_per_side + bt.stt_buy, sell=bt.cost_per_side + bt.stt_sell)

    def run(signals: dict[pd.Timestamp, list[str]]) -> SimResult:
        return simulate(
            closes, signals, holding_days=holding_days, costs=costs, lag=bt.execution_lag_days
        )

    selected: dict[pd.Timestamp, list[str]] = defaultdict(list)
    for (g, z), by_date in cells.items():
        if g in grades and z in zones:
            for d, syms in by_date.items():
                selected[d] += syms
    port = run(dict(selected))
    bench = (
        benchmark_equity(world.benchmark_close, window)
        if world.benchmark_close is not None
        else pd.Series(dtype=float)
    )
    port_curve = sample(port.equity, bt.equity_curve_points)
    bench_vals = (
        bench.reindex(port_curve.index).ffill().to_numpy(dtype=float).tolist()
        if not bench.empty
        else [float("nan")] * len(port_curve)
    )
    curve = [
        {
            "time": t.date().isoformat(),
            "portfolio": round(v, 4),
            "benchmark": None if b != b else round(b, 4),  # NaN → null
        }
        for t, v, b in zip(
            pd.DatetimeIndex(port_curve.index),
            port_curve.to_numpy(dtype=float).tolist(),
            bench_vals,
            strict=True,
        )
    ]
    table = []
    for g in GRADES:
        for z in ZONES:
            sig = cells.get((g, z), {})
            m = metrics(run(sig)) if sig else None
            table.append(
                {
                    "grade": g,
                    "zone": z,
                    "signals": sum(len(v) for v in sig.values()),
                    "selected": g in grades and z in zones,
                    **(_metrics_dict(m) if m else {k: None for k in _metrics_dict(metrics(port))}),
                }
            )
            if not sig:
                table[-1]["trades"] = 0

    excluded_annual = sum(
        _excluded_rows(s.annual, "announcement_date") for s in world.stocks.values()
    )
    excluded_q = sum(
        _excluded_rows(s.quarterly, "announcement_date") for s in world.stocks.values()
    )
    excluded_shp = sum(_excluded_rows(s.shareholding, "filing_date") for s in world.stocks.values())
    caveats = [
        "Monthly rebalance; entries execute "
        f"{bt.execution_lag_days} session(s) after the rebalance close; costs "
        f"{costs.buy:.2%} per buy and {costs.sell:.2%} per sell (brokerage + STT).",
        f"Benchmark: {benchmark_label}.",
        "Sector classification is today's (not point-in-time); user overrides are not applied.",
        "Relative valuation uses peer figures from the previous rebalance.",
    ]
    first_session, last_session = rebalances[0].date(), window[-1].date()
    if (first_session - start).days > 31 or (end - last_session).days > 7:
        caveats.append(
            f"Stored prices cover {first_session} to {last_session} only; the test ran over that "
            f"window, not the requested {start} to {end}."
        )
    if symbols:
        caveats.append(
            "Fixed symbol list, not the point-in-time Nifty 500: results carry survivorship bias."
        )
    elif world.membership is None or world.membership.empty:
        caveats.append(
            f"No {config.jobs.universe_index} membership is stored, so the universe is empty and "
            "nothing was traded. Run the index_constituents job, or list symbols."
        )
    else:
        first = world.membership["effective_from"].min().date()
        if first > start:
            caveats.append(
                f"Index membership is stored only from {first}: rebalances before that have no "
                "universe. Load historical constituents for a longer point-in-time test."
            )
    assumed_a = sum(s.assumed_annual for s in world.stocks.values())
    assumed_q = sum(s.assumed_quarterly for s in world.stocks.values())
    if assumed_a or assumed_q:
        lag = bt.fundamentals_availability_lag_days
        caveats.append(
            f"{assumed_a} annual and {assumed_q} quarterly statements have no announcement date "
            f"(Indian API, yfinance, Screener): taken as public {lag.annual} / {lag.quarterly} "
            "days after their period end."
        )
    if excluded_annual or excluded_q or excluded_shp:
        caveats.append(
            f"Excluded for lack of announcement/filing dates (rule 4): {excluded_annual} annual, "
            f"{excluded_q} quarterly statements and {excluded_shp} shareholding filings."
        )
    return {
        "period": {
            "start": rebalances[0].date().isoformat(),
            "end": window[-1].date().isoformat(),
            "rebalances": len(rebalances),
        },
        "universe": {
            "source": "symbols" if symbols else "index_membership",
            "avg_size": sum(universe_sizes) / len(universe_sizes),
            "stocks_with_data": len(world.stocks),
        },
        "portfolio": _metrics_dict(metrics(port)),
        "benchmark": {
            "label": benchmark_label,
            "cagr": cagr(bench) if not bench.empty else None,
            "max_drawdown": max_drawdown(bench) if not bench.empty else None,
            "total_return": float(bench.iloc[-1] / bench.iloc[0] - 1) if len(bench) > 1 else None,
        },
        "equity": curve,
        "cells": table,
        "trades_sample": [
            {
                "symbol": t.symbol,
                "entry": t.entry_date.date().isoformat(),
                "exit": t.exit_date.date().isoformat(),
                "days": t.days_held,
                "return": round(t.net_return, 6),
                "closed_by": t.closed_by,
            }
            for t in port.trades[-200:]
        ],
        "caveats": caveats,
        "notes": world.notes[:50],
        "failures": dict(list(failures.items())[:50]),
    }
