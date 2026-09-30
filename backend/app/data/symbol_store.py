"""Writing the symbol master (SPEC v0.2 §3.5): instruments for every NSE listing, one
``symbols`` row per ISIN, and the aliases search uses.

- **NSE is required.** It names the instruments everything else is keyed by. BSE and Fyers are
  optional: a master that could not be fetched leaves its columns and aliases as they were.
- **ISIN is the identity.** An instrument whose ISIN now trades under another symbol (an NSE
  symbol change) is renamed, so its price and fundamentals history follows the company. If
  the new symbol already has its own instrument, nothing is merged: the conflict is reported.
- **Aliases are replaced per kind** from the file that produced them (former symbols from the
  symbol-change file, and so on), for the companies in this refresh; aliases the owner added,
  and those of companies no longer listed, are kept.
- A company missing from both exchange masters of a refresh becomes ``inactive``; it is kept,
  with its aliases, so old names still resolve.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.data.symbol_master import MasterJoin
from app.db.enums import AliasKind, SymbolStatus
from app.db.models import Instrument, Symbol, SymbolAlias
from app.db.upsert import upsert

_KIND_OF_MASTER: dict[str, tuple[AliasKind, ...]] = {
    "symbol_changes": (AliasKind.FORMER_SYMBOL,),
    "name_changes": (AliasKind.FORMER_NAME,),
    "bse": (AliasKind.BSE_SYMBOL, AliasKind.BSE_NAME),
}


@dataclass
class StoreSummary:
    instruments: int = 0
    renamed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    symbols: int = 0
    inactive: int = 0
    aliases: int = 0


def _instruments(session: Session, join: MasterJoin, now: datetime, out: StoreSummary) -> None:
    by_symbol = {i.symbol: i for i in session.scalars(select(Instrument))}
    by_isin = {i.isin: i for i in by_symbol.values() if i.isin}
    rows: list[dict[str, Any]] = []
    for s in join.symbols:
        if not s.nse_symbol:
            continue
        holder = by_isin.get(s.isin)
        if holder is not None and holder.symbol != s.nse_symbol:
            if s.nse_symbol in by_symbol:
                out.conflicts.append(f"{s.isin}: {holder.symbol} is now {s.nse_symbol}, which "
                                     "already has its own instrument; not merged")  # fmt: skip
                continue
            out.renamed.append(f"{holder.symbol} → {s.nse_symbol}")
            del by_symbol[holder.symbol]
            holder.symbol = s.nse_symbol
            by_symbol[s.nse_symbol] = holder
            session.flush()
        other = by_symbol.get(s.nse_symbol)
        if other is not None and other.isin not in (None, s.isin):
            out.conflicts.append(f"{s.nse_symbol}: instrument has ISIN {other.isin}, the NSE "
                                 f"list says {s.isin} (a reused symbol?); not updated")  # fmt: skip
            continue
        rows.append({"symbol": s.nse_symbol, "isin": s.isin, "name": s.name,
                     "series": s.nse_series, "listing_date": s.listing_date,
                     "face_value": s.face_value, "is_active": True, "is_index": False,
                     "source": "nse", "fetched_at": now})  # fmt: skip
    if rows:
        out.instruments = upsert(session, Instrument, rows,
                                 update=["isin", "name", "series", "listing_date", "face_value",
                                         "is_active", "source", "fetched_at"])  # fmt: skip


def store_master(
    session: Session,
    join: MasterJoin,
    *,
    fetched: Iterable[str],
    today: date,
    now: datetime,
) -> StoreSummary:
    """Write ``join``. ``fetched`` names the files read this time: ``nse`` (required),
    ``bse``, ``fyers``, ``symbol_changes``, ``name_changes``. Does not commit."""
    got = set(fetched)
    if "nse" not in got:
        raise ValueError("the NSE equity list is required")
    out = StoreSummary()
    _instruments(session, join, now, out)
    ids: dict[str, int] = {sym: iid for sym, iid in session.execute(
        select(Instrument.symbol, Instrument.id))}  # fmt: skip

    cols = ["name", "nse_symbol", "nse_series", "listing_date", "instrument_id", "status",
            "sources", "last_seen"]  # fmt: skip
    if "bse" in got:
        cols += ["bse_code", "bse_id", "face_value"]
    if "fyers" in got:
        cols.append("fyers_symbol")
    existing = {s.isin: s for s in session.scalars(select(Symbol))}
    rows = []
    for s in join.symbols:
        old = existing.get(s.isin)
        kept = [src for src in (old.sources if old else []) if src not in got]
        rows.append({
            "isin": s.isin, "name": s.name, "nse_symbol": s.nse_symbol,
            "nse_series": s.nse_series, "bse_code": s.bse_code, "bse_id": s.bse_id,
            "fyers_symbol": s.fyers_symbol, "listing_date": s.listing_date,
            "face_value": s.face_value, "status": SymbolStatus.ACTIVE,
            "instrument_id": ids.get(s.nse_symbol) if s.nse_symbol else None,
            "sources": sorted(set(s.sources) | set(kept)), "last_seen": today,
        })  # fmt: skip
    if rows:
        out.symbols = upsert(session, Symbol, rows, update=cols)
    if {"nse", "bse"} <= got:
        seen = {s.isin for s in join.symbols}
        stale = [isin for isin, s in existing.items()
                 if isin not in seen and s.status is SymbolStatus.ACTIVE]  # fmt: skip
        if stale:
            session.execute(update(Symbol).where(Symbol.isin.in_(stale))
                            .values(status=SymbolStatus.INACTIVE))  # fmt: skip
        out.inactive = len(stale)

    sym_ids: dict[str, int] = {isin: sid for isin, sid in session.execute(
        select(Symbol.isin, Symbol.id))}  # fmt: skip
    kinds = [k for master, ks in _KIND_OF_MASTER.items() if master in got for k in ks]
    if kinds:
        listed = [sym_ids[r.isin] for r in join.symbols if r.isin in sym_ids]
        session.execute(delete(SymbolAlias).where(SymbolAlias.kind.in_(kinds),
                                                  SymbolAlias.symbol_id.in_(listed)))  # fmt: skip
        alias_rows = [
            {"symbol_id": sym_ids[a.isin], "alias": a.alias[:200], "kind": AliasKind(a.kind),
             "source": a.source, "valid_until": a.valid_until}
            for a in join.aliases if a.kind in kinds and a.isin in sym_ids
        ]  # fmt: skip
        if alias_rows:
            out.aliases = upsert(session, SymbolAlias, alias_rows)
    return out
