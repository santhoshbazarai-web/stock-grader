"""Seed synthetic DEMO stocks so the UI can be explored before any real data is fetched.

    python -m app.devtools.demo            # seed DEMO* stocks and build their reports
    python -m app.devtools.demo --purge    # delete them again

Everything written is synthetic: symbols start with ``DEMO``, names end with "(synthetic
demo)" and prices carry ``source='demo'``. A synthetic NIFTY500 benchmark is written only
when no NIFTY500 prices exist at all (never over real data); ``--purge`` removes it only if it
is still the demo series. Intended for a local development database.
"""

import argparse
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, get_config
from app.data import prices
from app.db.enums import AliasKind, EventKind, IssueStatus, PeriodType, StatementType, SymbolStatus
from app.db.models import (
    Event,
    Instrument,
    PriceDaily,
    ReconciliationIssue,
    Symbol,
    SymbolAlias,
)
from app.db.session import get_session_factory
from app.db.upsert import upsert
from app.devtools.synthetic import seed_company, seed_index
from app.reports.build import build_report
from app.reports.data import load_stock_data
from app.reports.dto import PeerStats
from app.reports.service import persist

# symbol, sector, revenue growth, PE the price is scaled to, price seed
DEMO_STOCKS = (
    ("DEMOIT", "it_services", 0.12, 25.0, 11),
    ("DEMOSOFT", "it_services", 0.15, 32.0, 21),
    ("DEMOCODE", "it_services", 0.10, 18.0, 31),
    ("DEMOTECH", "it_services", 0.08, 14.0, 41),
    ("DEMOBANK", "banks", 0.14, 16.0, 51),
    ("DEMOFMCG", "fmcg", 0.09, 45.0, 61),
)
DEMO_SOURCE = "demo"
# Synthetic symbol-master entries (SPEC §3.5): made-up ISINs / BSE codes, so search can be tried
# by BSE code, former name and former symbol, plus one BSE-only company.
DEMO_ISIN = "INE9DEMO{:02d}01"
DEMO_BSE = "99{:04d}"
DEMO_FORMER = {  # symbol → (former name, former symbol)
    "DEMOIT": ("Demo Infotech Systems Ltd (synthetic demo)", "DEMOINFO"),
    "DEMOBANK": ("Demo Co-operative Finance Ltd (synthetic demo)", None),
}


def seed(session: Session, config: AppConfig) -> list[str]:
    bench = config.jobs.universe_index
    bench_bars = session.scalar(
        select(func.count())
        .select_from(PriceDaily)
        .join(Instrument, Instrument.id == PriceDaily.instrument_id)
        .where(Instrument.symbol == bench)
    )
    if not bench_bars:
        seed_index(session, bench, source=DEMO_SOURCE)
    for symbol, sector, growth, pe, s in DEMO_STOCKS:
        seed_company(
            session,
            symbol,
            sector=sector,
            growth=growth,
            pe=pe,
            seed=s,
            shp_quarters=8,
            name=f"{symbol.title()} Ltd (synthetic demo)",
            price_source=DEMO_SOURCE,
        )
    session.commit()
    _seed_symbol_master(session)
    _seed_events_and_issues(session)
    session.commit()

    # Two passes, as in the valuation_scores job, so relative valuation sees the peers.
    loaded = {}
    by_sector: dict[str, list[PeerStats]] = defaultdict(list)
    for symbol, *_ in DEMO_STOCKS:
        data = load_stock_data(session, symbol, config, peers=[])
        loaded[symbol] = data
        stats = build_report(data, config).run.peer
        by_sector[stats.sector].append(stats)
    for symbol, data in loaded.items():
        key = data.sector if data.sector in config.sectors.root else "default"
        data.peers = [p for p in by_sector.get(key or "default", []) if p.symbol != symbol]
        persist(session, build_report(data, config))
    session.commit()
    return [s for s, *_ in DEMO_STOCKS]


def _seed_symbol_master(session: Session) -> None:
    ids = dict(session.execute(select(Instrument.symbol, Instrument.id)
                               .where(Instrument.symbol.like("DEMO%"))).all())  # fmt: skip
    rows = [
        {
            "isin": DEMO_ISIN.format(i),
            "name": f"{sym.title()} Ltd (synthetic demo)",
            "nse_symbol": sym,
            "nse_series": "EQ",
            "bse_code": DEMO_BSE.format(i),
            "instrument_id": ids.get(sym),
            "status": SymbolStatus.ACTIVE,
            "sources": ["demo"],
        }
        for i, (sym, *_) in enumerate(DEMO_STOCKS, start=1)
    ]
    rows.append({**rows[0], "isin": DEMO_ISIN.format(99), "bse_code": DEMO_BSE.format(99),
                 "name": "Demo Rural Traders Ltd (synthetic demo)", "nse_symbol": None,
                 "nse_series": None, "instrument_id": None})  # BSE only  # fmt: skip
    upsert(session, Symbol, rows)
    sym_ids = dict(session.execute(select(Symbol.nse_symbol, Symbol.id)
                                   .where(Symbol.isin.like("INE9DEMO%"))).all())  # fmt: skip
    aliases = []
    for sym, (name, old_symbol) in DEMO_FORMER.items():
        aliases.append({"symbol_id": sym_ids[sym], "alias": name, "kind": AliasKind.FORMER_NAME,
                        "source": DEMO_SOURCE, "valid_until": None})  # fmt: skip
        if old_symbol:
            aliases.append({"symbol_id": sym_ids[sym], "alias": old_symbol,
                            "kind": AliasKind.FORMER_SYMBOL, "source": DEMO_SOURCE,
                            "valid_until": None})  # fmt: skip
    upsert(session, SymbolAlias, aliases, update=[])


def _seed_events_and_issues(session: Session) -> None:
    """Synthetic corporate events (SPEC §3.8) for DEMOIT / DEMOCODE and one open
    reconciliation issue (§3.9) for DEMOSOFT, so the events card and the banner can be seen."""
    ids = dict(session.execute(select(Instrument.symbol, Instrument.id)
                               .where(Instrument.symbol.like("DEMO%"))).all())  # fmt: skip
    now = datetime.now(UTC)
    today = now.date()
    events = [  # (symbol, kind, days from today, title, category, red flag)
        ("DEMOIT", EventKind.BOARD_MEETING, 6, "Board meeting: Financial Results", "board_meeting",
         False),
        ("DEMOIT", EventKind.BULK_DEAL, -2, "Bulk deal: DEMO FUND LLP bought 650,000 shares at "
         "₹1,412.35", None, False),
        ("DEMOIT", EventKind.ANNOUNCEMENT, -9, "Record date for final dividend", "dividend", False),
        ("DEMOCODE", EventKind.ANNOUNCEMENT, -20, "Credit rating: long-term rating downgraded to "
         "A- (Negative)", "credit_rating_downgrade", True),
    ]  # fmt: skip
    upsert(session, Event, [
        {"exchange": "nse", "kind": kind, "source_id": f"demo:{sym}:{kind.value}:{i}",
         "instrument_id": ids[sym], "symbol": sym, "isin": None, "bse_code": None,
         "company": f"{sym.title()} Ltd (synthetic demo)",
         "title": f"{title} (synthetic demo)", "detail": None, "category": category,
         "red_flag": red, "event_date": today + timedelta(days=days),
         "disseminated_at": now if days <= 0 else now - timedelta(days=5), "url": None,
         "data": {"purpose": "Financial Results"} if kind is EventKind.BOARD_MEETING else None,
         "raw_path": None, "fetched_at": now}
        for i, (sym, kind, days, title, category, red) in enumerate(events)
    ])  # fmt: skip
    fy = date(today.year - (1 if today.month > 3 else 2), 3, 31)
    upsert(session, ReconciliationIssue, [{
        "instrument_id": ids["DEMOSOFT"], "period_end": fy, "period_type": PeriodType.YEAR,
        "basis": StatementType.CONSOLIDATED, "item_code": "revenue", "source": "yfinance",
        "reference_source": "nse_xbrl", "reference_value_inr": 1000e7, "value_inr": 820e7,
        "diff_rel": 0.18, "values": {"nse_xbrl": 1000e7, "yfinance": 820e7}, "cause": "basis",
        "reasons": [f"Sales (year to {fy:%d %b %Y}, consolidated): yfinance ₹820.00 cr vs NSE "
                    "XBRL ₹1,000.00 cr, 18.0% apart (synthetic demo)",
                    "yfinance matches the standalone figure (₹818.40 cr): consolidated / "
                    "standalone mix-up"],
        "status": IssueStatus.OPEN, "detected_at": now, "checked_at": now, "resolved_at": None,
    }])  # fmt: skip


def purge(session: Session, config: AppConfig) -> int:
    session.execute(delete(Event).where(Event.source_id.like("demo:%")))
    session.execute(delete(Symbol).where(Symbol.isin.like("INE9DEMO%")))
    n = session.execute(delete(Instrument).where(Instrument.symbol.like("DEMO%"))).rowcount  # type: ignore[attr-defined]
    bench = config.jobs.universe_index
    iid = prices.instrument_id(session, bench)
    if iid is not None:
        real = session.scalar(
            select(func.count())
            .select_from(PriceDaily)
            .where(PriceDaily.instrument_id == iid, PriceDaily.source != DEMO_SOURCE)
        )
        if not real:
            session.execute(delete(Instrument).where(Instrument.id == iid))
    session.commit()
    return int(n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--purge", action="store_true", help="delete the DEMO* stocks")
    args = parser.parse_args(argv)
    config = get_config()
    with get_session_factory()() as session:
        if args.purge:
            print(f"removed {purge(session, config)} demo instruments")
        else:
            symbols = seed(session, config)
            print(f"seeded and scored: {', '.join(symbols)} (synthetic demo data)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
