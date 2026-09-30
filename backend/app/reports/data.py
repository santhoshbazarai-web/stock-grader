"""Everything a report needs about one stock, read from the database into plain frames.

:class:`StockData` is a pure container; :func:`load_stock_data` is the only DB access. The
report pipeline (``build.py``) never touches the database (AGENTS.md rule 2).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.data import prices
from app.data.canonical import fields_for
from app.db.enums import StatementType, SurveillanceList, Timeframe
from app.db.models import (
    DeliveryDaily,
    FinAnnual,
    FinQuarterly,
    Instrument,
    PriceDaily,
    Report,
    Shareholding,
    SurveillanceFlag,
    TechnicalSnapshot,
    UserOverride,
)
from app.reports.dto import PeerStats
from app.reports.overrides import Overrides

SHP_COLUMNS = ("promoter_pct", "promoter_pledge_pct", "fii_pct", "dii_pct", "mf_pct", "public_pct")
ASM_GSM = (SurveillanceList.ASM_LT, SurveillanceList.ASM_ST, SurveillanceList.GSM)


@dataclass
class StockData:
    symbol: str
    name: str | None
    sector: str | None  # instruments.sector (a sectors.yaml key, or None → default)
    daily: pd.DataFrame  # adjusted OHLCV
    annual: pd.DataFrame  # canonical fin_annual (index = period end)
    quarterly: pd.DataFrame  # canonical fin_quarterly
    statement_type: str | None  # consolidated | standalone (rule 5)
    shareholding: pd.DataFrame  # index = period end
    delivery_pct: pd.Series | None = None
    traded_value_cr: pd.Series | None = None
    benchmark_close: pd.Series | None = None
    last_results_date: date | None = None
    on_asm_gsm: bool | None = None  # None = surveillance lists never fetched
    rs_percentile: float | None = None
    overrides: Overrides = field(default_factory=Overrides)
    peers: list[PeerStats] = field(default_factory=list)
    sources: dict[str, str | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def load_financials(
    session: Session, model: type[FinAnnual] | type[FinQuarterly], iid: int, table: str
) -> tuple[pd.DataFrame, str | None, str | None]:
    """Consolidated first; standalone only if there is no consolidated statement (rule 5)."""
    cols = [*fields_for(table), "announcement_date", "extra"]  # type: ignore[arg-type]
    if table == "fin_annual":
        cols += ["fiscal_year", "is_derived"]
    for basis in (StatementType.CONSOLIDATED, StatementType.STANDALONE):
        rows: list[Any] = list(
            session.scalars(
                select(model)
                .where(model.instrument_id == iid, model.statement_type == basis)
                .order_by(model.period_end)
            )
        )
        if rows:
            df = pd.DataFrame(
                [{c: getattr(r, c, None) for c in cols} for r in rows],
                index=pd.DatetimeIndex(
                    [pd.Timestamp(r.period_end) for r in rows], name="period_end"
                ),
            )
            return df, basis.value, rows[-1].source
    empty = pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="period_end"))
    return empty, None, None


def load_shareholding(session: Session, iid: int) -> tuple[pd.DataFrame, str | None]:
    rows = list(
        session.scalars(
            select(Shareholding)
            .where(Shareholding.instrument_id == iid)
            .order_by(Shareholding.period_end)
        )
    )
    df = pd.DataFrame(
        [{c: getattr(r, c) for c in SHP_COLUMNS} for r in rows],
        index=pd.DatetimeIndex([pd.Timestamp(r.period_end) for r in rows], name="period_end"),
        columns=list(SHP_COLUMNS),
    )
    return df, (rows[-1].source if rows else None)


def load_overrides(session: Session, iid: int) -> Overrides:
    rows = session.execute(
        select(UserOverride.key, UserOverride.value).where(UserOverride.instrument_id == iid)
    ).all()
    return Overrides.model_validate({k: v.get("value") for k, v in rows})


def load_peers(session: Session, sector: str, exclude: str) -> list[PeerStats]:
    """Latest stored ``peer_stats`` of every other stock whose report used ``sector``."""
    latest = (
        select(Report.instrument_id, func.max(Report.as_of).label("as_of"))
        .group_by(Report.instrument_id)
        .subquery()
    )
    payloads: list[Any] = list(
        session.scalars(
            select(Report.payload).join(
                latest,
                (latest.c.instrument_id == Report.instrument_id) & (latest.c.as_of == Report.as_of),
            )
        )
    )
    out = []
    for p in payloads:
        stats = p.get("peer_stats") if isinstance(p, dict) else None
        if stats and stats.get("sector") == sector and stats.get("symbol") != exclude:
            out.append(PeerStats.model_validate(stats))
    return out


def _series(rows: list[Any]) -> pd.Series | None:
    if not rows:
        return None
    return pd.Series(
        [r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]), dtype=float
    ).dropna()


def load_stock_data(
    session: Session,
    symbol: str,
    config: AppConfig,
    *,
    peers: list[PeerStats] | None = None,
    benchmark_close: pd.Series | None = None,
) -> StockData:
    """Raises ``prices.NoPriceData`` / ``prices.UnadjustedPrices`` (no report without prices).
    ``peers`` / ``benchmark_close`` may be passed in by batch jobs to avoid re-reading them."""
    sym = symbol.upper()
    inst = session.scalar(select(Instrument).where(Instrument.symbol == sym))
    if inst is None:
        raise prices.NoPriceData(f"{sym}: unknown instrument")
    daily = prices.adjusted_daily(session, sym)
    annual, basis, fin_source = load_financials(session, FinAnnual, inst.id, "fin_annual")
    quarterly, _, _ = load_financials(session, FinQuarterly, inst.id, "fin_quarterly")
    shp, shp_source = load_shareholding(session, inst.id)
    price_source = session.scalar(
        select(PriceDaily.source)
        .where(PriceDaily.instrument_id == inst.id)
        .order_by(PriceDaily.date.desc())
        .limit(1)
    )
    delivery_rows = session.execute(
        select(DeliveryDaily.date, DeliveryDaily.traded_value_cr)
        .where(DeliveryDaily.instrument_id == inst.id)
        .order_by(DeliveryDaily.date)
    ).all()

    surveillance_known = (
        session.scalar(select(func.count()).select_from(SurveillanceFlag)) or 0
    ) > 0
    on_list = None
    if surveillance_known:
        on_list = (
            session.scalar(
                select(func.count())
                .select_from(SurveillanceFlag)
                .where(
                    SurveillanceFlag.instrument_id == inst.id,
                    SurveillanceFlag.list_name.in_(ASM_GSM),
                    SurveillanceFlag.effective_to.is_(None),
                )
            )
            or 0
        ) > 0

    notes: list[str] = []
    snap = session.execute(
        select(TechnicalSnapshot.as_of, TechnicalSnapshot.rs_percentile)
        .where(
            TechnicalSnapshot.instrument_id == inst.id,
            TechnicalSnapshot.timeframe == Timeframe.WEEKLY,
        )
        .order_by(TechnicalSnapshot.as_of.desc())
        .limit(1)
    ).first()
    rs_pct = None
    last_day = daily.index.max().date()
    max_age = timedelta(days=config.technical.rs_percentile_max_age_days)
    if snap is not None and snap[1] is not None:
        if last_day - snap[0] <= max_age:
            rs_pct = float(snap[1])
        else:
            notes.append(f"RS percentile from {snap[0]} is stale (prices to {last_day})")

    overrides = load_overrides(session, inst.id)
    sector_key = overrides.sector or inst.sector or "default"
    if benchmark_close is None:
        benchmark_close = prices.close_series(session, config.jobs.universe_index)
    if peers is None:
        peers = load_peers(session, sector_key, sym)
    return StockData(
        symbol=sym,
        name=inst.name,
        sector=overrides.sector or inst.sector,
        daily=daily,
        annual=annual,
        quarterly=quarterly,
        statement_type=basis,
        shareholding=shp,
        delivery_pct=prices.delivery_pct(session, sym),
        traded_value_cr=_series(list(delivery_rows)),
        benchmark_close=benchmark_close,
        last_results_date=prices.last_results_date(session, sym),
        on_asm_gsm=on_list,
        rs_percentile=rs_pct,
        overrides=overrides,
        peers=[p for p in peers if p.symbol != sym],
        sources={
            "prices": price_source,
            "fundamentals": fin_source,
            "shareholding": shp_source,
        },
        notes=notes,
    )
