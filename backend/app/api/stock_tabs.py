"""Stock page tabs: statements, key-metric history, dividends; and the Compare tab's peers
and normalised prices (SPEC §9)."""

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import Symbol
from app.db.models import Instrument, PriceDaily, Report
from app.reports import tabs
from app.reports.service import latest_payloads

router = APIRouter(tags=["stock tabs"])
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Unknown symbol"}}


def _iid(session: SessionDep, symbol: str) -> int:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return iid


@router.get("/stocks/{symbol}/statements", responses=_NOT_FOUND)
def statements(
    symbol: Symbol,
    session: SessionDep,
    config: ConfigDep,
    period: Literal["annual", "quarterly"] = "annual",
) -> tabs.Statements:
    """Up to 12 columns, oldest first: income statement, balance sheet and cash flow (banks:
    NII, non-interest income / expense, provisions, loans, deposits, equity, BVPS)."""
    out = tabs.statements(session, symbol, config, period)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return out


@router.get("/stocks/{symbol}/key-metrics", responses=_NOT_FOUND)
def key_metrics(symbol: Symbol, session: SessionDep, config: ConfigDep) -> tabs.KeyMetricsOut:
    """Grouped metrics, each with its 5-year median and percentile in its own history."""
    iid = _iid(session, symbol)
    payload = latest_payloads(session, [iid]).get(iid) or {}
    out = tabs.key_metrics(session, symbol, config, payload.get("peer_stats"))
    assert out is not None
    return out


@router.get("/stocks/{symbol}/dividends", responses=_NOT_FOUND)
def dividends(symbol: Symbol, session: SessionDep) -> tabs.DividendsOut:
    iid = _iid(session, symbol)
    payload = latest_payloads(session, [iid]).get(iid) or {}
    out = tabs.dividends(session, symbol, payload.get("cmp"), date.today())
    assert out is not None
    return out


class Peer(BaseModel):
    symbol: str
    name: str | None


@router.get("/compare/peers/{symbol}", responses=_NOT_FOUND)
def peers(
    symbol: Symbol, session: SessionDep, limit: Annotated[int, Query(ge=1, le=12)] = 6
) -> list[Peer]:
    """Stocks in the same sector that have a stored report."""
    iid = _iid(session, symbol)
    sector = session.scalar(select(Instrument.sector).where(Instrument.id == iid))
    if not sector:
        return []
    rows = session.execute(
        select(Instrument.symbol, Instrument.name)
        .where(Instrument.sector == sector, Instrument.id != iid, Instrument.is_active.is_(True),
               Instrument.id.in_(select(Report.instrument_id)))
        .order_by(Instrument.symbol).limit(limit)
    ).all()  # fmt: skip
    return [Peer(symbol=s, name=n) for s, n in rows]


class PricePoint(BaseModel):
    date: date
    value: float


class PriceSeries(BaseModel):
    symbol: str
    points: list[PricePoint]


class ComparePrices(BaseModel):
    base_date: date | None
    series: list[PriceSeries]


@router.get("/compare/prices")
def compare_prices(
    session: SessionDep,
    symbols: Annotated[str, Query(description="Comma-separated, up to 4")],
    years: Annotated[int, Query(ge=1, le=10)] = 3,
) -> ComparePrices:
    """Adjusted closes rebased to 100 on the first date every stock has a price."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:4]
    since = date.today() - timedelta(days=365 * years)
    raw: dict[str, dict[date, float]] = {}
    for s in syms:
        iid = session.scalar(select(Instrument.id).where(Instrument.symbol == s))
        if iid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {s}")
        rows = session.execute(
            select(PriceDaily.date, PriceDaily.adj_close)
            .where(PriceDaily.instrument_id == iid, PriceDaily.date >= since,
                   PriceDaily.adj_close.is_not(None))
            .order_by(PriceDaily.date)
        ).all()  # fmt: skip
        raw[s] = {d: float(c) for d, c in rows if c is not None}
    common = set.intersection(*(set(v) for v in raw.values())) if raw else set()
    base = min(common) if common else None
    series = [
        PriceSeries(
            symbol=s,
            points=[
                PricePoint(date=d, value=c / v[base] * 100)
                for d, c in sorted(v.items())
                if d >= base
            ],
        )
        for s, v in raw.items()
        if base
    ]
    return ComparePrices(base_date=base, series=series)
