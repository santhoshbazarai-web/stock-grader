"""Writing exchange events (SPEC v0.2 §3.8, §10 ``events``): link each to an instrument,
classify it, and upsert it on (exchange, kind, source_id).

Linking, first match wins: the feed's NSE symbol (an instrument, or a former symbol in the
alias table), its ISIN, its BSE code (via the symbol master), then its company name
(normalised: case, punctuation and "Ltd"/"Limited" ignored) against the symbol master and
instruments. An event that matches nothing is still stored, unlinked.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.core.config import EventClassificationConfig
from app.data.events import classify, normalise_company
from app.db.enums import AliasKind, EventKind
from app.db.models import Event, Instrument, Symbol, SymbolAlias
from app.db.upsert import upsert

_UPDATED = ("instrument_id", "symbol", "isin", "bse_code", "company", "title", "detail",
            "category", "red_flag", "event_date", "disseminated_at", "url", "data", "raw_path",
            "fetched_at")  # fmt: skip


@dataclass
class Linker:
    """Lookup tables for linking feed rows to instruments (built once per store call)."""

    by_symbol: dict[str, int] = field(default_factory=dict)
    by_isin: dict[str, int] = field(default_factory=dict)
    by_bse: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session) -> "Linker":
        out = cls()
        for iid, sym, isin, name in session.execute(
            select(Instrument.id, Instrument.symbol, Instrument.isin, Instrument.name)
            .where(Instrument.is_index.is_(False))
        ):  # fmt: skip
            out.by_symbol[sym] = iid
            if isin:
                out.by_isin[isin] = iid
            if (n := normalise_company(name)) is not None:
                out.by_name.setdefault(n, iid)
        for sid, isin, bse, name in session.execute(
            select(Symbol.instrument_id, Symbol.isin, Symbol.bse_code, Symbol.name)
            .where(Symbol.instrument_id.is_not(None))
        ):  # fmt: skip
            if sid is None:
                continue
            iid = sid
            out.by_isin.setdefault(isin, iid)
            if bse:
                out.by_bse[bse] = iid
            if (n := normalise_company(name)) is not None:
                out.by_name.setdefault(n, iid)
        for alias, kind, aid in session.execute(
            select(SymbolAlias.alias, SymbolAlias.kind, Symbol.instrument_id)
            .join(Symbol, Symbol.id == SymbolAlias.symbol_id)
            .where(Symbol.instrument_id.is_not(None),
                   SymbolAlias.kind.in_([AliasKind.FORMER_SYMBOL, AliasKind.FORMER_NAME,
                                         AliasKind.BSE_NAME]))
        ):  # fmt: skip
            if aid is None:
                continue
            iid = aid
            if kind is AliasKind.FORMER_SYMBOL:
                out.by_symbol.setdefault(alias.upper(), iid)
            elif (n := normalise_company(alias)) is not None:
                out.by_name.setdefault(n, iid)
        return out

    def link(self, rec: dict[str, Any]) -> int | None:
        for value, table in ((rec.get("symbol"), self.by_symbol), (rec.get("isin"), self.by_isin),
                             (rec.get("bse_code"), self.by_bse),
                             (normalise_company(rec.get("company")), self.by_name)):  # fmt: skip
            if value and value in table:
                return table[value]
        return None


@dataclass(frozen=True)
class StoredEvents:
    rows: int
    new_ids: list[int]  # events not seen before this call
    unlinked: int


def _plain(value: Any) -> Any:
    if value is None or (not isinstance(value, dict | list | str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def store_events(
    session: Session,
    frame: pd.DataFrame,
    *,
    exchange: str,
    cfg: EventClassificationConfig,
    now: datetime,
    linker: Linker | None = None,
) -> StoredEvents:
    """Upsert one feed's frame. Re-reading a window updates the rows (text, link, category)
    but never ``handled_at`` / ``pipeline_run_id``. Does not commit."""
    if frame.empty:
        return StoredEvents(0, [], 0)
    linker = linker or Linker.load(session)
    rows: list[dict[str, Any]] = []
    unlinked = 0
    for raw in frame.to_dict("records"):
        rec: dict[str, Any] = {str(k): _plain(v) for k, v in raw.items()}
        kind = EventKind(rec["kind"])
        category = classify(rec["title"], rec.get("detail"), cfg)
        if kind is EventKind.RESULTS:
            category = "results"
        elif kind is EventKind.BOARD_MEETING and category is None:
            category = "board_meeting"
        iid = linker.link(rec)
        unlinked += iid is None
        rows.append({
            "exchange": exchange, "kind": kind, "source_id": str(rec["source_id"])[:200],
            "instrument_id": iid, "symbol": (rec.get("symbol") or None),
            "isin": rec.get("isin"), "bse_code": rec.get("bse_code"),
            "company": (rec.get("company") or None) and str(rec["company"])[:200],
            "title": str(rec["title"]), "detail": rec.get("detail"), "category": category,
            "red_flag": category in cfg.red_flags,
            "event_date": _date(rec.get("event_date")),
            "disseminated_at": rec.get("disseminated_at"),
            "url": (rec.get("url") or None) and str(rec["url"])[:1024],
            "data": rec.get("data"), "raw_path": rec.get("raw_path"), "fetched_at": now,
        })  # fmt: skip
    keys: list[tuple[str, str, str]] = [(r["exchange"], r["kind"].value, r["source_id"])
                                        for r in rows]  # fmt: skip
    known = set(_existing(session, keys))
    upsert(session, Event, rows, update=list(_UPDATED))
    new_keys = [k for k in keys if k not in known]
    new_ids = (
        list(
            session.scalars(
                select(Event.id).where(
                    tuple_(Event.exchange, Event.kind, Event.source_id).in_(new_keys)
                )
            )
        )
        if new_keys
        else []
    )
    return StoredEvents(len(rows), sorted(new_ids), unlinked)


def _existing(session: Session, keys: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    keys = list(keys)
    out: list[tuple[str, str, str]] = []
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        out += [(e, k.value, s) for e, k, s in session.execute(
            select(Event.exchange, Event.kind, Event.source_id)
            .where(tuple_(Event.exchange, Event.kind, Event.source_id).in_(chunk)))]  # fmt: skip
    return out


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()
