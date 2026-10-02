"""Everything a report needs about one stock, read from the database into plain frames.

:class:`StockData` is a pure container; :func:`load_stock_data` is the only DB access. The
report pipeline (``build.py``) never touches the database (AGENTS.md rule 2).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, SectorModel, StructuralEvent
from app.data import prices
from app.data.adjust import restate_per_share
from app.data.canonical import fields_for
from app.data.indianapi_checks import analyst_consensus, company_key, vendor_peers
from app.data.indianapi_parse import SHARES_PER_CRORE
from app.data.indianapi_store import cached_answers
from app.data.price_anomalies import open_anomalies
from app.data.reconcile_store import issue_text, open_issues
from app.db.enums import EventKind, PeriodType, StatementType, SurveillanceList, Timeframe
from app.db.models import (
    CorporateAction,
    DeliveryDaily,
    Event,
    FinAnnual,
    FinLineItem,
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
    # SPEC v0.2 §3.8-3.9: open cross-source differences (lower the valuation confidence),
    # recent red-flag events, and auditor resignations on file (None: feed coverage too short).
    reconciliation_issues: list[str] = field(default_factory=list)
    event_red_flags: list[str] = field(default_factory=list)
    auditor_resignations: list[date] | None = None
    industry_label: str | None = None  # the classification label the sector came from
    industry_source: str | None = None  # "nse" | "yfinance" | None (not classified yet)
    # Indian API recosBar (informational only, never scored): analyst_consensus() + as_of
    analyst_consensus: dict[str, Any] | None = None
    # SPEC §4 structural breaks: the stock's events (structural_events.yaml) and its reported
    # year-end share count (crore) by fiscal year, for per-share growth across them
    structural_events: list[StructuralEvent] = field(default_factory=list)
    shares_year_end: dict[int, float] = field(default_factory=dict)


def load_analyst_consensus(session: Session, iid: int) -> dict[str, Any] | None:
    """From the latest verified Indian API /stock answer on file (no call)."""
    row = cached_answers(session, iid).get("/stock")
    got = analyst_consensus(row.payload) if row is not None else None
    if row is None or got is None:
        return None
    return {**got, "as_of": row.fetched_at.date()}


def load_shares_year_end(session: Session, iid: int, annual: pd.DataFrame) -> dict[int, float]:
    """Reported shares outstanding at each fiscal-year end (crore), keyed like ``annual``'s
    fiscal years. Only the Indian API reports them today; on today's share basis."""
    if annual.empty or "fiscal_year" not in annual.columns:
        return {}
    fy_by_end = {pd.Timestamp(e).date(): int(fy) for e, fy in zip(annual.index,
                                                                  annual["fiscal_year"],
                                                                  strict=True)
                 if pd.notna(fy)}  # fmt: skip
    rows = session.execute(select(FinLineItem.period_end, FinLineItem.value_inr).where(
        FinLineItem.instrument_id == iid, FinLineItem.item_code == "shares_outstanding",
        FinLineItem.period_type == PeriodType.INSTANT,
        FinLineItem.period_end.in_(list(fy_by_end)))).all()  # fmt: skip
    return {fy_by_end[end]: float(v) / SHARES_PER_CRORE for end, v in rows if v and v > 0}


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


def vendor_rows(session: Session, model: type[FinAnnual] | type[FinQuarterly],
                iid: int) -> set[date]:  # fmt: skip
    """Period ends whose wide row comes from the Indian API (per-share figures current)."""
    return set(session.scalars(select(model.period_end).where(
        model.instrument_id == iid, model.source == "indianapi")))  # fmt: skip


def load_share_actions(session: Session, iid: int) -> pd.DataFrame:
    rows = session.execute(
        select(CorporateAction.ex_date, CorporateAction.action_type, CorporateAction.ratio_old,
               CorporateAction.ratio_new).where(CorporateAction.instrument_id == iid)
    ).all()  # fmt: skip
    return pd.DataFrame(rows, columns=["ex_date", "action_type", "ratio_old", "ratio_new"])


def load_shareholding(session: Session, iid: int) -> tuple[pd.DataFrame, str | None]:
    rows = list(
        session.scalars(
            select(Shareholding)
            .where(Shareholding.instrument_id == iid)
            .order_by(Shareholding.period_end)
        )
    )
    cols = [*SHP_COLUMNS, "filing_date", "source"]
    df = pd.DataFrame(
        [{c: getattr(r, c) for c in cols} for r in rows],
        index=pd.DatetimeIndex([pd.Timestamp(r.period_end) for r in rows], name="period_end"),
        columns=cols,
    )
    return df, (rows[-1].source if rows else None)


def load_overrides(session: Session, iid: int) -> Overrides:
    rows = session.execute(
        select(UserOverride.key, UserOverride.value).where(UserOverride.instrument_id == iid)
    ).all()
    return Overrides.model_validate({k: v.get("value") for k, v in rows})


def load_peers(
    session: Session, sector: str, exclude: str, symbols: Iterable[str] = ()
) -> list[PeerStats]:
    """Latest stored ``peer_stats`` of every other stock whose report used ``sector``, plus
    those of ``symbols`` (the sector's configured peers) whatever sector their report used."""
    wanted = {s.upper() for s in symbols}
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
        if not stats or stats.get("symbol") == exclude:
            continue
        if stats.get("sector") == sector or str(stats.get("symbol")).upper() in wanted:
            out.append(PeerStats.model_validate(stats))
    return out


def load_vendor_peers(
    session: Session, iid: int, own_name: str | None, sector: str, stored: list[PeerStats]
) -> tuple[list[PeerStats], list[str]]:
    """The Indian API peer list from the latest verified /stock answer (no call), as peer
    stats. A vendor peer that is also a stored peer (same company name) replaces it: one
    company is counted once. Returns (peers, notes)."""
    row = cached_answers(session, iid).get("/stock")
    if row is None:
        return stored, ["vendor peer list: no Indian API /stock answer on file"]
    rows = vendor_peers(row.payload)
    if not rows:
        return stored, ["vendor peer list: empty in the /stock answer"]
    names = dict(
        session.execute(
            select(Instrument.symbol, Instrument.name).where(
                Instrument.symbol.in_([p.symbol for p in stored])
            )
        ).all()
    )
    own = company_key(own_name) if own_name else None
    vendor = [
        PeerStats(symbol=r["name"], name=r["name"], sector=sector, pe=r["pe"], pb=r["pb"],
                  ev_ebitda=None, roce=None, roe=r["roe"], eps_growth=None, source="vendor")
        for r in rows
        if company_key(r["name"]) != own
    ]  # fmt: skip
    keys = {company_key(p.symbol) for p in vendor}
    kept = [p for p in stored if company_key(names.get(p.symbol) or p.symbol) not in keys]
    return [*vendor, *kept], []


def load_events(
    session: Session, iid: int, as_of: date, config: AppConfig
) -> tuple[list[date] | None, list[str]]:
    """(auditor resignation dates, recent red-flag events). Resignations are known ([] when
    none) only when the stored announcement feeds reach back over the whole knock-out window
    (``knockouts.auditor_resignation_years``); otherwise None, so the knock-out stays unknown."""
    ec = config.jobs.event_classification
    rows = session.execute(
        select(Event.event_date, Event.title, Event.category, Event.red_flag)
        .where(Event.instrument_id == iid,
               (Event.category == ec.auditor_resignation) | Event.red_flag.is_(True))
        .order_by(Event.event_date.desc())
    ).all()  # fmt: skip
    since = as_of - timedelta(days=ec.red_flag_days)
    flags = [f"{title} ({day:%d %b %Y})" for day, title, _, red in rows
             if red and day is not None and since <= day <= as_of]  # fmt: skip
    found = sorted({day for day, _, cat, _ in rows
                    if cat == ec.auditor_resignation and day is not None})  # fmt: skip
    if found:
        return found, flags
    first = session.scalar(select(func.min(Event.event_date)).where(
        Event.kind == EventKind.ANNOUNCEMENT))  # fmt: skip
    years = config.scoring.knockouts.auditor_resignation_years
    window_start = date(as_of.year - years, as_of.month, min(as_of.day, 28))
    return ([] if first is not None and first <= window_start else None), flags


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
    # per-share figures on today's share basis, like the adjusted prices (rule 6)
    actions = load_share_actions(session, inst.id)
    # the Indian API's per-share figures are already on today's share basis (its FY2015 EPS
    # for HDFCBANK implies today's share count): never restated again
    adj = config.providers.adjustment
    current_a = vendor_rows(session, FinAnnual, inst.id)
    current_q = vendor_rows(session, FinQuarterly, inst.id)
    annual, restated = restate_per_share(annual, actions, adj, current=current_a)
    quarterly, _ = restate_per_share(quarterly, actions, adj, current=current_q)
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

    notes: list[str] = list(restated)
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
    consensus = load_analyst_consensus(session, inst.id)
    shares_ye = load_shares_year_end(session, inst.id, annual)
    resignations, flags = load_events(session, inst.id, last_day, config)
    issues = [issue_text(i) for i in open_issues(session, inst.id)]
    # SPEC §3.2: price moves that look like a missing / doubled split or bonus
    notes += [f"price data: {a.text}" for a in open_anomalies(session, inst.id)]
    sector_key = overrides.sector or inst.sector or "default"
    if benchmark_close is None:
        benchmark_close = prices.close_series(session, config.jobs.universe_index)
    sector_cfg = config.sectors.for_sector(sector_key)
    cfg_peers = sector_cfg.peers or []
    if peers is None:
        peers = load_peers(session, sector_key, sym, cfg_peers)
    else:  # batch jobs pass the sector's peers: add the configured ones they lack
        have = {p.symbol for p in peers}
        peers = [*peers, *(p for p in load_peers(session, "", sym, cfg_peers)
                           if p.symbol not in have)]  # fmt: skip
    peers = [p for p in peers if p.symbol != sym]
    if sector_cfg.model is SectorModel.BANK:
        peers, peer_notes = load_vendor_peers(session, inst.id, inst.name, sector_key, peers)
        notes += peer_notes
    return StockData(
        symbol=sym,
        name=inst.name,
        sector=overrides.sector or inst.sector,
        industry_label=inst.basic_industry,
        industry_source=inst.industry_source,
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
        peers=peers,
        sources={
            "prices": price_source,
            "fundamentals": fin_source,
            "shareholding": shp_source,
        },
        notes=notes,
        reconciliation_issues=issues,
        event_red_flags=flags,
        auditor_resignations=resignations,
        analyst_consensus=consensus,
        structural_events=config.structural_events.for_symbol(sym),
        shares_year_end=shares_ye,
    )
