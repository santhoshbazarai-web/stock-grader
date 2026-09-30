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

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, get_config
from app.data import prices
from app.db.enums import AliasKind, SymbolStatus
from app.db.models import Instrument, PriceDaily, Symbol, SymbolAlias
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


def purge(session: Session, config: AppConfig) -> int:
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
