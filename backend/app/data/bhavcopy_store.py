"""The NSE bhavcopy history (SPEC v0.2 §3.2: daily OHLCV built day by day from the
``sec_bhavdata_full`` archive): which trading days are stored, each day's rows for every
symbol, and a symbol's bars (including days it traded under a former symbol).

Only storage lives here; ``NseProvider`` downloads the files and decides what to fetch.
"""

from collections.abc import Callable, Iterable
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AliasKind
from app.db.models import BhavcopyDay, BhavcopyPrice, Symbol, SymbolAlias
from app.db.upsert import upsert

LOADED, HOLIDAY = "loaded", "holiday"
OHLCV = ["open", "high", "low", "close", "volume"]
_PRICE_COLS = ("series", "open", "high", "low", "close", "prev_close", "volume",
               "traded_value_cr", "deliverable_qty", "delivery_pct")  # fmt: skip


def weekdays(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


class BhavcopyStore:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def days(self, start: date, end: date) -> dict[date, str]:
        """Stored days in [start, end] → loaded | holiday."""
        with self._sessions() as s:
            return dict(s.execute(
                select(BhavcopyDay.trade_date, BhavcopyDay.status)
                .where(BhavcopyDay.trade_date.between(start, end))
            ).all())  # fmt: skip

    def missing(self, start: date, end: date) -> list[date]:
        """Weekdays in [start, end] with neither a stored file nor a known holiday."""
        known = self.days(start, end)
        return [d for d in weekdays(start, end) if d not in known]

    def save_day(self, day: date, df: pd.DataFrame, raw_path: str | None, now: datetime) -> int:
        """Store one file's rows (``BHAVCOPY_COLUMNS``) and mark the day loaded. A file dated
        otherwise than ``day`` (NSE served another day's file) is stored under its own date."""
        rows = [{"symbol": r["symbol"], "trade_date": r["date"] or day,
                 **{c: r[c] for c in _PRICE_COLS}} for r in df.to_dict("records")]  # fmt: skip
        with self._sessions() as s:
            upsert(s, BhavcopyPrice, rows)
            upsert(s, BhavcopyDay, [{"trade_date": day, "status": LOADED, "rows": len(rows),
                                     "raw_path": raw_path, "fetched_at": now}])  # fmt: skip
            s.commit()
        return len(rows)

    def mark_holiday(self, day: date, now: datetime) -> None:
        with self._sessions() as s:
            upsert(s, BhavcopyDay, [{"trade_date": day, "status": HOLIDAY, "rows": 0,
                                     "raw_path": None, "fetched_at": now}])  # fmt: skip
            s.commit()

    def _symbols(self, s: Session, symbol: str) -> list[str]:
        """The symbol and its former NSE symbols (symbol-change aliases)."""
        former = s.scalars(
            select(SymbolAlias.alias)
            .join(Symbol, Symbol.id == SymbolAlias.symbol_id)
            .where(Symbol.nse_symbol == symbol, SymbolAlias.kind == AliasKind.FORMER_SYMBOL)
        ).all()  # fmt: skip
        return [symbol, *(f.upper() for f in former if f.upper() != symbol)]

    def ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Raw daily bars (``open, high, low, close, volume``; DatetimeIndex ``date``)."""
        sym = symbol.strip().upper()
        with self._sessions() as s:
            rows = s.execute(
                select(BhavcopyPrice.trade_date, *(getattr(BhavcopyPrice, c) for c in OHLCV))
                .where(BhavcopyPrice.symbol.in_(self._symbols(s, sym)),
                       BhavcopyPrice.trade_date.between(start, end))
                .order_by(BhavcopyPrice.trade_date)
            ).all()  # fmt: skip
        df = pd.DataFrame(
            [r[1:] for r in rows],
            columns=OHLCV,
            index=pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows], name="date"),
        )
        df = df[~df.index.duplicated(keep="last")]  # the renamed symbol wins on the switch day
        return df.astype({"open": float, "high": float, "low": float, "close": float})

    def delivery(self, day: date, columns: Iterable[str]) -> pd.DataFrame:
        """The day's delivery rows in the shape of ``NseProvider.delivery``."""
        with self._sessions() as s:
            rows = s.scalars(select(BhavcopyPrice).where(BhavcopyPrice.trade_date == day)).all()
        recs = [{"symbol": r.symbol, "series": r.series, "date": r.trade_date,
                 "traded_qty": r.volume, "deliverable_qty": r.deliverable_qty,
                 "delivery_pct": r.delivery_pct, "traded_value_cr": r.traded_value_cr}
                for r in rows]  # fmt: skip
        df = pd.DataFrame(recs, columns=list(columns))
        return df.astype({"traded_qty": "Int64", "deliverable_qty": "Int64"})
