"""Point-in-time views of stored history (SPEC §11, AGENTS.md rule 4). Pure functions.

Everything a report sees at rebalance date ``d`` is sliced to what was *known* at ``d``:
- prices, delivery %, traded value and the benchmark: rows dated on or before ``d``;
- annual and quarterly statements: rows whose ``announcement_date`` is on or before ``d``.
  Rows with no announcement date are excluded (never assumed: the filing lag used by live
  reports does not apply here);
- shareholding: filings whose ``filing_date`` is on or before ``d`` (no filing date: excluded);
- surveillance lists: listings in force at ``d``;
- the universe: index members at ``d`` (delisted stocks included where data exists);
- user overrides are not applied (they are present-day judgements).
"""

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.core.config import TechnicalConfig
from app.reports.data import StockData
from app.reports.dto import PeerStats
from app.reports.overrides import Overrides
from app.technical.bars import to_weekly
from app.technical.rs import mansfield_rs, percentile_ranks


@dataclass
class PitStock:
    symbol: str
    name: str | None
    sector: str | None
    daily: pd.DataFrame  # adjusted OHLCV
    annual: pd.DataFrame  # incl. announcement_date
    quarterly: pd.DataFrame
    statement_type: str | None
    shareholding: pd.DataFrame  # incl. filing_date
    delivery_pct: pd.Series | None = None
    traded_value_cr: pd.Series | None = None
    # Weekly closes of the full history, cached for RS ranking. Each weekly bar is labelled
    # with its last session, so slicing ``<= d`` never uses data after ``d``.
    weekly_close: pd.Series | None = None


@dataclass
class PitWorld:
    stocks: dict[str, PitStock]
    benchmark_close: pd.Series | None
    membership: pd.DataFrame | None  # columns: symbol, effective_from, effective_to (NaT = open)
    surveillance: pd.DataFrame | None  # columns: symbol, effective_from, effective_to
    notes: list[str] = field(default_factory=list)


def _upto(series: pd.Series | None, d: pd.Timestamp) -> pd.Series | None:
    return None if series is None else series.loc[series.index <= d]


def announced_by(
    frame: pd.DataFrame, d: pd.Timestamp, column: str = "announcement_date"
) -> pd.DataFrame:
    """Rows known at ``d``: ``column`` on or before ``d``; rows without a date are dropped."""
    if frame.empty or column not in frame.columns:
        return frame.iloc[0:0]
    known = pd.to_datetime(frame[column], errors="coerce")
    return frame.loc[known.notna() & (known <= d)]


def universe_at(world: PitWorld, d: pd.Timestamp, symbols: list[str] | None = None) -> list[str]:
    """Index members at ``d`` that have stored data (or the given ``symbols``)."""
    if symbols:
        return [s for s in symbols if s in world.stocks]
    m = world.membership
    if m is None or m.empty:
        return []
    live = m[(m["effective_from"] <= d) & (m["effective_to"].isna() | (m["effective_to"] > d))]
    return sorted({s for s in live["symbol"] if s in world.stocks})


def on_asm_gsm_at(world: PitWorld, symbol: str, d: pd.Timestamp) -> bool | None:
    s = world.surveillance
    if s is None:
        return None  # lists never collected: unknown, not "clean"
    rows = s[(s["symbol"] == symbol) & (s["effective_from"] <= d)]
    rows = rows[rows["effective_to"].isna() | (rows["effective_to"] > d)]
    return not rows.empty


def rs_percentiles_at(
    world: PitWorld, symbols: list[str], d: pd.Timestamp, cfg: TechnicalConfig
) -> dict[str, float | None]:
    """Universe percentile of the latest Mansfield RS at ``d`` (as the technicals job does)."""
    bench = world.benchmark_close
    if bench is None:
        return dict.fromkeys(symbols)
    b = bench.loc[bench.index <= d]
    if b.empty:
        return dict.fromkeys(symbols)
    b_weekly = to_weekly(pd.DataFrame({"open": b, "high": b, "low": b, "close": b, "volume": 0.0}))[
        "close"
    ]
    latest: dict[str, float | None] = {}
    for sym in symbols:
        st = world.stocks[sym]
        if st.weekly_close is None:
            st.weekly_close = to_weekly(st.daily)["close"]
        w = st.weekly_close.loc[st.weekly_close.index <= d]
        rs = mansfield_rs(w, b_weekly, cfg.rs_sma_weeks) if len(w) else pd.Series(dtype=float)
        latest[sym] = float(rs.iloc[-1]) if len(rs) else None
    return percentile_ranks(latest)


def stock_data_at(
    world: PitWorld,
    symbol: str,
    d: pd.Timestamp,
    *,
    peers: list[PeerStats],
    rs_percentile: float | None,
) -> StockData | None:
    """``None`` when the stock has no prices on or before ``d``."""
    st = world.stocks[symbol]
    daily = st.daily.loc[st.daily.index <= d]
    if len(daily) < 2:
        return None
    annual = announced_by(st.annual, d)
    quarterly = announced_by(st.quarterly, d)
    shp = announced_by(st.shareholding, d, "filing_date")
    last_results = (
        pd.to_datetime(quarterly["announcement_date"]).max().date() if not quarterly.empty else None
    )
    return StockData(
        symbol=symbol,
        name=st.name,
        sector=st.sector,
        daily=daily,
        annual=annual,
        quarterly=quarterly,
        statement_type=st.statement_type,
        shareholding=shp.drop(columns=["filing_date"], errors="ignore"),
        delivery_pct=_upto(st.delivery_pct, d),
        traded_value_cr=_upto(st.traded_value_cr, d),
        benchmark_close=_upto(world.benchmark_close, d),
        last_results_date=last_results,
        on_asm_gsm=on_asm_gsm_at(world, symbol, d),
        rs_percentile=rs_percentile,
        overrides=Overrides(),
        peers=[p for p in peers if p.symbol != symbol],
        sources={"prices": "stored", "fundamentals": "stored (announced by rebalance date)"},
    )


def month_starts(sessions: pd.DatetimeIndex, start: date, end: date) -> list[pd.Timestamp]:
    """First session of each month within [start, end] (the monthly rebalance dates)."""
    s = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    if s.empty:
        return []
    firsts = pd.Series(s, index=s).groupby([s.year, s.month]).min()
    return [pd.Timestamp(x) for x in firsts]
