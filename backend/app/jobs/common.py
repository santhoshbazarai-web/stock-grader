"""Helpers shared by the job pipelines: universe, instrument ids, row conversion, adjustment."""

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import AdjustmentConfig
from app.data.adjust import AdjustmentResult, adjust_prices, suspicious_moves
from app.data.price_anomalies import save_anomalies
from app.db.models import CorporateAction, IndexMembership, Instrument, PriceDaily, WatchlistItem
from app.jobs.runner import JobContext, JobOptions

logger = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"


def py(value: Any) -> Any:
    """numpy/pandas scalar → plain Python; NaN/NA → None (never 0)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, np.generic):
        v = value.item()
        return None if isinstance(v, float) and np.isnan(v) else v
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def normalise_symbols(symbols: Iterable[str]) -> list[str]:
    out: list[str] = []
    for s in symbols:
        for part in s.split(","):
            p = part.strip().upper()
            if p and p not in out:
                out.append(p)
    return out


def ensure_instruments(
    session: Session, symbols: Iterable[str], *, is_index: bool = False
) -> dict[str, int]:
    """Instrument ids for ``symbols``, creating bare rows for unknown ones (never overwriting
    existing metadata)."""
    symbols = list(symbols)
    if symbols:
        stmt = insert(Instrument).values(
            [{"symbol": s, "source": MANUAL_SOURCE, "is_index": is_index} for s in symbols]
        )
        session.execute(stmt.on_conflict_do_nothing(index_elements=["symbol"]))
    return instrument_ids(session, symbols)


def instrument_ids(session: Session, symbols: Iterable[str]) -> dict[str, int]:
    symbols = list(symbols)
    if not symbols:
        return {}
    rows = session.execute(
        select(Instrument.symbol, Instrument.id).where(Instrument.symbol.in_(symbols))
    )
    return {s: i for s, i in rows}


def universe(ctx: JobContext, options: JobOptions) -> list[str]:
    """CLI ``--symbols`` if given, else current members of ``jobs.universe_index`` plus the
    watchlist (non-index, active instruments), sorted."""
    session = ctx.session_factory()
    try:
        if options.symbols:
            symbols = normalise_symbols(options.symbols)
            ensure_instruments(session, symbols)
            session.commit()
            return symbols
        index = ctx.config.jobs.universe_index
        members = (
            select(Instrument.symbol)
            .join(IndexMembership, IndexMembership.instrument_id == Instrument.id)
            .where(
                IndexMembership.index_name == index,
                IndexMembership.effective_to.is_(None),
                Instrument.is_active.is_(True),
                Instrument.is_index.is_(False),
            )
        )
        watch = select(Instrument.symbol).join(
            WatchlistItem, WatchlistItem.instrument_id == Instrument.id
        )
        found = set(session.scalars(members)) | set(session.scalars(watch))
        return sorted(found)
    finally:
        session.close()


def frame_records(df: pd.DataFrame, columns: Iterable[str]) -> list[dict[str, Any]]:
    cols = list(columns)
    return [{c: py(rec[c]) for c in cols} for rec in df[cols].to_dict("records")]


def non_empty_columns(df: pd.DataFrame, candidates: Iterable[str]) -> list[str]:
    """Columns with at least one value. A partial source must not overwrite fields another
    source filled with NULLs (e.g. NSE shareholding vs a Screener upload)."""
    return [c for c in candidates if c in df.columns and df[c].notna().any()]


def readjust(
    session: Session, instrument_id: int, config: AdjustmentConfig | None = None
) -> AdjustmentResult:
    """Recompute ``adj_*`` for one instrument from its raw prices and corporate actions;
    writes only rows whose adjustment changed. Does not commit."""
    raw = pd.DataFrame(
        session.execute(
            select(
                PriceDaily.date,
                PriceDaily.open,
                PriceDaily.high,
                PriceDaily.low,
                PriceDaily.close,
                PriceDaily.volume,
                PriceDaily.adj_factor,
                PriceDaily.adj_close,
                PriceDaily.source,
            )
            .where(PriceDaily.instrument_id == instrument_id)
            .order_by(PriceDaily.date)
        ).all(),
        columns=["date", "open", "high", "low", "close", "volume", "old_factor", "old_close",
                 "source"],
    )  # fmt: skip
    actions = pd.DataFrame(
        session.execute(
            select(
                CorporateAction.ex_date,
                CorporateAction.action_type,
                CorporateAction.ratio_old,
                CorporateAction.ratio_new,
                CorporateAction.price_adjusted_by_source,
            ).where(CorporateAction.instrument_id == instrument_id)
        ).all(),
        columns=["ex_date", "action_type", "ratio_old", "ratio_new", "price_adjusted_by_source"],
    )
    if raw.empty:
        return AdjustmentResult(raw, True)
    raw["date"] = pd.to_datetime(raw["date"])
    raw = raw.set_index("date")
    result = adjust_prices(raw[["open", "high", "low", "close", "volume"]], actions, config,
                           sources=raw["source"])  # fmt: skip
    adj = result.prices
    if config is not None:  # SPEC §3.2: moves that look like a missing / doubled action
        result.suspicious = suspicious_moves(adj, actions, config.suspicious)
        save_anomalies(session, instrument_id, result.suspicious,
                       datetime.now(UTC))  # fmt: skip
    new_factor = adj["adj_factor"].astype(float)
    old_factor = raw["old_factor"].astype(float)
    same_factor = np.isclose(new_factor.to_numpy(), old_factor.to_numpy(), equal_nan=True)
    same_nullness = (raw["old_close"].notna() == adj["adj_close"].notna()).to_numpy()
    changed = ~(same_factor & same_nullness)
    rows = [
        {
            "instrument_id": instrument_id,
            "date": d.date(),
            **{c: py(adj.at[d, c]) for c in _ADJ_COLUMNS},
        }
        for d in adj.index[changed]
    ]
    if rows:
        session.execute(update(PriceDaily), rows)
    result.changed = len(rows)
    return result


_ADJ_COLUMNS = ("adj_factor", "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume")


def last_price_date(session: Session, instrument_id: int) -> date | None:
    return session.scalar(
        select(PriceDaily.date)
        .where(PriceDaily.instrument_id == instrument_id)
        .order_by(PriceDaily.date.desc())
        .limit(1)
    )


def count_by(values: Iterable[Any]) -> Mapping[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out
