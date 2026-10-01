"""Read stored market data for analysis (split/bonus-adjusted prices, delivery %)."""

from datetime import date

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DeliveryDaily, FinQuarterly, Instrument, PriceDaily


class NoPriceData(LookupError):
    pass


class UnadjustedPrices(LookupError):
    """Some stored bars have no adjusted values (e.g. a split/bonus with an unknown ratio)."""


def instrument_id(session: Session, symbol: str) -> int | None:
    return session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))


def adjusted_daily(session: Session, symbol: str, *, start: date | None = None) -> pd.DataFrame:
    """Adjusted OHLCV as ``open, high, low, close, volume`` indexed by date. Raises
    :class:`UnadjustedPrices` rather than silently mixing raw and adjusted bars (rule 6)."""
    iid = instrument_id(session, symbol)
    if iid is None:
        raise NoPriceData(f"{symbol}: unknown instrument")
    q = (
        select(
            PriceDaily.date,
            PriceDaily.adj_open,
            PriceDaily.adj_high,
            PriceDaily.adj_low,
            PriceDaily.adj_close,
            PriceDaily.adj_volume,
        )
        .where(PriceDaily.instrument_id == iid)
        .order_by(PriceDaily.date)
    )
    if start is not None:
        q = q.where(PriceDaily.date >= start)
    df = pd.DataFrame(
        session.execute(q).all(), columns=["date", "open", "high", "low", "close", "volume"]
    )
    if df.empty:
        raise NoPriceData(f"{symbol}: no stored prices (run eod_prices)")
    missing = int(df["close"].isna().sum())
    if missing:
        first = df.loc[df["close"].notna(), "date"].min()
        raise UnadjustedPrices(
            f"{symbol}: {missing} bars lack adjusted prices (adjusted history starts {first}); "
            "see data gaps for the corporate action"
        )
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").astype(float)
    return df


def close_series(session: Session, symbol: str, *, start: date | None = None) -> pd.Series | None:
    try:
        return adjusted_daily(session, symbol, start=start)["close"]
    except (NoPriceData, UnadjustedPrices):
        return None


def delivery_pct(session: Session, symbol: str) -> pd.Series | None:
    iid = instrument_id(session, symbol)
    if iid is None:
        return None
    rows = session.execute(
        select(DeliveryDaily.date, DeliveryDaily.delivery_pct)
        .where(DeliveryDaily.instrument_id == iid)
        .order_by(DeliveryDaily.date)
    ).all()
    if not rows:
        return None
    s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]), dtype=float)
    return s


def last_results_date(session: Session, symbol: str) -> date | None:
    iid = instrument_id(session, symbol)
    if iid is None:
        return None
    return session.scalar(
        select(func.max(FinQuarterly.announcement_date)).where(FinQuarterly.instrument_id == iid)
    )
