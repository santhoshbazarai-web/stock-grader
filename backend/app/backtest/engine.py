"""Portfolio simulation and metrics (SPEC §11). Pure functions over a close-price matrix.

Model (documented in SPEC §11 implementation notes):
- Signals are decided at a rebalance date's close and executed ``lag`` sessions later at that
  session's close (no same-bar look-ahead).
- A new position gets ``min(NAV / (held + new), cash / new)``: existing positions are never
  re-weighted (no churn), and capital freed by exits waits in cash (0% return) until the next
  rebalance.
- A position is sold at the close of its ``holding_days``-th session after entry. If the stock
  stops trading (suspension beyond the data, delisting), it is sold at its last available close.
  Open positions are closed at the last date of the backtest.
- Costs: ``buy`` on the value bought, ``sell`` on the value sold (brokerage + STT).
"""

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

ClosedBy = Literal["holding_period", "data_end", "backtest_end"]


@dataclass(frozen=True)
class Costs:
    buy: float
    sell: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    days_held: int  # trading sessions
    net_return: float
    closed_by: ClosedBy


@dataclass
class SimResult:
    equity: pd.Series  # NAV per session, starting at ``start_value``
    invested: pd.Series  # fraction of NAV in positions
    trades: list[Trade] = field(default_factory=list)


@dataclass(frozen=True)
class Metrics:
    cagr: float | None
    max_drawdown: float | None
    hit_rate: float | None
    avg_holding_days: float | None
    avg_trade_return: float | None
    trades: int
    exposure: float | None  # average fraction invested
    total_return: float | None


@dataclass
class _Pos:
    shares: float
    entry_date: pd.Timestamp
    entry_price: float
    days: int = 0
    last_price: float = 0.0


def simulate(
    closes: pd.DataFrame,
    signals: dict[pd.Timestamp, list[str]],
    *,
    holding_days: int,
    costs: Costs,
    lag: int,
    start_value: float = 100.0,
) -> SimResult:
    """``closes``: sessions x symbols (NaN = no trade that day). ``signals``: rebalance date →
    symbols to hold. Signals dated on non-sessions execute from the next session on."""
    dates = pd.DatetimeIndex(closes.index)
    if len(dates) == 0:
        empty = pd.Series(dtype=float)
        return SimResult(empty, empty)
    px = closes.to_numpy(dtype=float)
    cols = {s: j for j, s in enumerate(closes.columns)}
    last_valid = {
        s: (int(np.flatnonzero(~np.isnan(px[:, j]))[-1]) if (~np.isnan(px[:, j])).any() else -1)
        for s, j in cols.items()
    }
    execute: dict[int, list[str]] = {}
    for when, syms in signals.items():
        i = int(dates.searchsorted(pd.Timestamp(when))) + lag
        if i < len(dates):
            execute.setdefault(i, [])
            execute[i] += [s for s in syms if s not in execute[i]]

    cash = start_value
    held: dict[str, _Pos] = {}
    trades: list[Trade] = []
    nav = np.empty(len(dates))
    inv = np.empty(len(dates))

    def sell(sym: str, i: int, price: float, how: ClosedBy) -> None:
        nonlocal cash
        p = held.pop(sym)
        cash += p.shares * price * (1 - costs.sell)
        ret = price * (1 - costs.sell) * (1 - costs.buy) / p.entry_price - 1
        trades.append(Trade(sym, p.entry_date, p.entry_price, dates[i], price, p.days, ret, how))

    for i, day in enumerate(dates):
        # 1. mark, age and exit
        for sym in list(held):
            p = held[sym]
            j = cols[sym]
            price = px[i, j]
            if not math.isnan(price):
                p.last_price = float(price)
            p.days += 1
            if p.days >= holding_days and not math.isnan(price):
                sell(sym, i, float(price), "holding_period")
            elif last_valid[sym] < i:  # no more data for this stock
                sell(sym, i, p.last_price, "data_end")
        # 2. enter
        new = [
            s
            for s in execute.get(i, [])
            if s in cols and s not in held and not math.isnan(px[i, cols[s]])
        ]
        if new:
            value = cash + sum(p.shares * p.last_price for p in held.values())
            target = value / (len(held) + len(new))
            alloc = min(target, cash / len(new))
            for sym in new:
                price = float(px[i, cols[sym]])
                if alloc <= 0:
                    break
                held[sym] = _Pos(alloc * (1 - costs.buy) / price, day, price, 0, price)
                cash -= alloc
        pos_value = sum(p.shares * p.last_price for p in held.values())
        nav[i] = cash + pos_value
        inv[i] = pos_value / nav[i] if nav[i] > 0 else 0.0

    last = len(dates) - 1
    for sym in list(held):
        # Closed at the final mark; the final NAV already reflects the position at that price.
        p = held[sym]
        sell(sym, last, p.last_price, "backtest_end")
    nav[last] = cash
    return SimResult(pd.Series(nav, index=dates), pd.Series(inv, index=dates), trades)


def cagr(equity: pd.Series) -> float | None:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return None
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return None
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def max_drawdown(equity: pd.Series) -> float | None:
    if len(equity) == 0:
        return None
    return float((equity / equity.cummax() - 1).min())


def metrics(result: SimResult) -> Metrics:
    eq, trades = result.equity, result.trades
    rets = [t.net_return for t in trades]
    return Metrics(
        cagr=cagr(eq),
        max_drawdown=max_drawdown(eq),
        hit_rate=sum(r > 0 for r in rets) / len(rets) if rets else None,
        avg_holding_days=float(np.mean([t.days_held for t in trades])) if trades else None,
        avg_trade_return=float(np.mean(rets)) if rets else None,
        trades=len(trades),
        exposure=float(result.invested.mean()) if len(result.invested) else None,
        total_return=float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) > 1 else None,
    )


def benchmark_equity(
    close: pd.Series, dates: pd.DatetimeIndex, start_value: float = 100.0
) -> pd.Series:
    """Buy-and-hold of the benchmark on the backtest's sessions, rebased to ``start_value``."""
    s = close.reindex(dates).ffill().dropna()
    if s.empty:
        return s
    return pd.Series(s / float(s.iloc[0]) * start_value, index=s.index)


def sample(series: pd.Series, how: Literal["daily", "weekly"]) -> pd.Series:
    """Thin a daily curve for charting. Weekly keeps each week's last *actual* session (so the
    benchmark lines up on holidays), plus the true first point so curves start at 100."""
    if how == "daily" or series.empty:
        return series
    idx = pd.DatetimeIndex(series.index)
    weekly = series.groupby(idx.to_period("W-FRI")).tail(1)
    if weekly.index[0] != series.index[0]:
        weekly = pd.concat([series.iloc[:1], weekly])
    return weekly
