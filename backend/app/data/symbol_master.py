"""Symbol master (SPEC v0.2 §3.5): NSE ``EQUITY_L.csv``, the BSE scrip master and the Fyers
symbol master joined on ISIN, plus aliases from NSE's symbol-change and name-change files.
Pure: parsers take file contents, :func:`join_masters` returns rows to store.

The exchanges change these formats without notice, so headers are matched by keywords and
unknown shapes raise :class:`MasterFormatError` naming what was found, never partial data.
"""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_TICKER_RE = re.compile(r"^(NSE|BSE):[A-Z0-9&_.\-]+$")
_DATE_FORMATS = ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y")

AliasKind = Literal["former_symbol", "former_name", "bse_symbol", "bse_name", "user"]


class MasterFormatError(ValueError):
    pass


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if r]


def _has_date(row: list[str]) -> bool:
    """A data row, not a header: some cell is a date."""
    return any(_date(c) is not None for c in row)


def _cell(row: list[str], index: int | None) -> str:
    return row[index] if index is not None and index < len(row) else ""


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z]+", " ", h.lower()).strip()


def _find(header: list[str], *keys: str, exclude: tuple[str, ...] = ()) -> int | None:
    """Index of the first column whose normalised header contains every word of one key."""
    names = [_norm_header(h) for h in header]
    for key in keys:
        words = key.split()
        for i, n in enumerate(names):
            if all(w in n.split() or w in n for w in words) and not any(x in n for x in exclude):
                return i
    return None


def normalise_name(name: str) -> str:
    """Comparison form of a company name: lower case, punctuation and legal suffixes dropped."""
    text = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    text = re.sub(r"\b(limited|ltd|the|co|company|corporation|corpn|corp|pvt|private)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ───────────────────────── NSE ─────────────────────────


@dataclass(frozen=True)
class NseListing:
    symbol: str
    name: str
    series: str | None
    listing_date: date | None
    face_value: float | None
    isin: str


def parse_nse_equity_list(text: str) -> list[NseListing]:
    """``EQUITY_L.csv``: SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, …, ISIN NUMBER,
    FACE VALUE. Rows without a valid ISIN are skipped."""
    rows = _rows(text)
    if not rows:
        raise MasterFormatError("EQUITY_L: empty file")
    header = rows[0]
    cols = {
        "symbol": _find(header, "symbol"),
        "name": _find(header, "name of company", "company name", "name"),
        "series": _find(header, "series"),
        "listing": _find(header, "date of listing", "listing"),
        "isin": _find(header, "isin"),
        "face": _find(header, "face value"),
    }
    if cols["symbol"] is None or cols["name"] is None or cols["isin"] is None:
        raise MasterFormatError(f"EQUITY_L: unexpected header {header}")
    out = []
    for r in rows[1:]:
        isin, symbol = _cell(r, cols["isin"]).upper(), _cell(r, cols["symbol"]).upper()
        if not ISIN_RE.match(isin) or not symbol:
            continue
        out.append(NseListing(symbol, _cell(r, cols["name"]), _cell(r, cols["series"]) or None,
                              _date(_cell(r, cols["listing"])), _float(_cell(r, cols["face"])),
                              isin))  # fmt: skip
    if not out:
        raise MasterFormatError("EQUITY_L: no rows with an ISIN")
    return out


@dataclass(frozen=True)
class SymbolChange:
    old: str
    new: str
    changed_on: date | None
    company: str | None = None


def parse_nse_symbol_changes(text: str) -> list[SymbolChange]:
    """``symbolchange.csv``: company, old symbol, new symbol, date of change. The header (if
    any) is matched by keywords; without one the columns are taken in that order."""
    rows = _rows(text)
    if not rows:
        return []
    header = rows[0] if not _has_date(rows[0]) else []
    old = _find(header, "old", "prev", "key symbol")
    new = _find(header, "new")
    when = _find(header, "date", "applicable", "dt")
    company = _find(header, "company", "name")
    body = rows[1:]
    if old is None or new is None:
        header = rows[0]
        if len(header) >= 3 and all(re.match(r"^[A-Z0-9&_.\-]+$", c) for c in header[1:3]):
            company, old, new, when = 0, 1, 2, 3 if len(header) > 3 else None
            body = rows
        else:
            raise MasterFormatError(f"symbol changes: unexpected header {header}")
    out = []
    for r in body:
        if max(old, new) >= len(r) or not r[old] or not r[new]:
            continue
        out.append(SymbolChange(
            r[old].upper(), r[new].upper(),
            _date(_cell(r, when)),
            _cell(r, company) or None,
        ))  # fmt: skip
    return out


@dataclass(frozen=True)
class NameChange:
    symbol: str
    old_name: str
    new_name: str
    changed_on: date | None


def parse_nse_name_changes(text: str) -> list[NameChange]:
    """``namechange.csv``: symbol, previous name, new name, date."""
    rows = _rows(text)
    if not rows:
        return []
    header = rows[0] if not _has_date(rows[0]) else []
    symbol = _find(header, "symbol")
    old = _find(header, "prev", "old")
    new = _find(header, "new")
    when = _find(header, "date", "dt")
    body = rows[1:]
    if symbol is None or old is None or new is None:
        header = rows[0]
        if len(header) >= 3 and re.match(r"^[A-Z0-9&_.\-]+$", header[0]):
            symbol, old, new, when = 0, 1, 2, 3 if len(header) > 3 else None
            body = rows
        else:
            raise MasterFormatError(f"name changes: unexpected header {header}")
    out = []
    for r in body:
        if max(symbol, old, new) >= len(r) or not r[symbol] or not r[old]:
            continue
        out.append(NameChange(r[symbol].upper(), r[old], r[new], _date(_cell(r, when))))
    return out


# ───────────────────────── BSE ─────────────────────────


@dataclass(frozen=True)
class BseScrip:
    code: str  # numeric scrip code, e.g. "500180"
    bse_id: str | None  # BSE's short symbol, e.g. "HDFCBANK"
    name: str
    isin: str
    active: bool
    face_value: float | None


def _pick(rec: dict[str, Any], *keys: str) -> Any:
    lower = {k.lower(): v for k, v in rec.items()}
    return next((lower[k.lower()] for k in keys if lower.get(k.lower()) not in (None, "")), None)


def parse_bse_scrips(payload: Any) -> list[BseScrip]:
    """BSE ``ListofScripData`` JSON: SCRIP_CD, scrip_id, Scrip_Name / Issuer_Name, Status,
    ISIN_NUMBER, FACE_VALUE. Rows without a valid ISIN are skipped."""
    records = payload.get("Table", payload.get("data")) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise MasterFormatError(f"BSE scrips: expected a list, got {type(payload).__name__}")
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        code = str(_pick(rec, "SCRIP_CD", "Scrip_Code", "scripcode") or "").strip()
        isin = str(_pick(rec, "ISIN_NUMBER", "ISIN", "isin") or "").strip().upper()
        if not code.isdigit() or not ISIN_RE.match(isin):
            continue
        status = str(_pick(rec, "Status", "status") or "Active").strip().lower()
        bse_id = str(_pick(rec, "scrip_id", "Scrip_Id", "SCRIP_ID") or "").strip().upper() or None
        out.append(BseScrip(
            code, bse_id, str(_pick(rec, "Scrip_Name", "Issuer_Name", "SCRIP_NAME") or "").strip(),
            isin, status.startswith("active"), _float(_pick(rec, "FACE_VALUE", "Face_Value")),
        ))  # fmt: skip
    if records and not out:
        raise MasterFormatError(f"BSE scrips: no usable rows; keys {sorted(records[0])}")
    return out


# ───────────────────────── Fyers ─────────────────────────


@dataclass(frozen=True)
class FyersSymbol:
    ticker: str  # e.g. "NSE:HDFCBANK-EQ"
    isin: str
    name: str


def parse_fyers_master(text: str) -> list[FyersSymbol]:
    """Fyers ``sym_details`` CSV (no header): the ISIN and the ``EXCHANGE:SYMBOL-SERIES`` ticker
    are found by pattern in each row (their column positions have moved before); the name is
    the second column. Rows without both are skipped."""
    out = []
    for r in _rows(text):
        isin = next((c.upper() for c in r if ISIN_RE.match(c.upper())), None)
        ticker = next((c for c in r if _TICKER_RE.match(c)), None)
        if isin and ticker:
            out.append(FyersSymbol(ticker, isin, r[1] if len(r) > 1 else ""))
    return out


# ───────────────────────── the join ─────────────────────────


@dataclass
class SymbolRow:
    isin: str
    name: str
    nse_symbol: str | None = None
    nse_series: str | None = None
    bse_code: str | None = None
    bse_id: str | None = None
    fyers_symbol: str | None = None
    listing_date: date | None = None
    face_value: float | None = None
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AliasRow:
    isin: str
    alias: str
    kind: AliasKind
    valid_until: date | None
    source: str


@dataclass
class MasterJoin:
    symbols: list[SymbolRow]
    aliases: list[AliasRow]
    warnings: list[str]


def current_symbol(symbol: str, changes: Iterable[SymbolChange]) -> str:
    """Follow old → new symbol changes (in date order) to today's symbol."""
    step = {c.old: c.new for c in sorted(changes, key=lambda c: c.changed_on or date.min)}
    seen = {symbol}
    while symbol in step and step[symbol] not in seen:
        symbol = step[symbol]
        seen.add(symbol)
    return symbol


def join_masters(
    nse: list[NseListing] | None,
    bse: list[BseScrip] | None,
    fyers: list[FyersSymbol] | None,
    symbol_changes: list[SymbolChange],
    name_changes: list[NameChange],
) -> MasterJoin:
    """One row per ISIN from whichever masters list it. NSE gives the symbol, series, listing
    date and preferred name; BSE the scrip code and its own symbol / name (kept as aliases when
    they differ); Fyers the broker ticker (the NSE one when both exchanges are listed). A master
    that could not be fetched is ``None``: its columns are left out, not blanked."""
    rows: dict[str, SymbolRow] = {}
    warnings: list[str] = []
    for n in nse or []:
        row = rows.get(n.isin)
        if row is not None and row.nse_series in ("EQ", "BE") and n.series not in ("EQ", "BE"):
            continue  # one ISIN in two series: keep the rolling-settlement one
        rows[n.isin] = SymbolRow(n.isin, n.name, n.symbol, n.series, listing_date=n.listing_date,
                                 face_value=n.face_value, sources=["nse"])  # fmt: skip
    aliases: list[AliasRow] = []
    for b in bse or []:
        row = rows.get(b.isin)
        if row is None:
            if not b.active:
                continue
            row = rows[b.isin] = SymbolRow(b.isin, b.name, face_value=b.face_value)
        row.bse_code, row.bse_id = b.code, b.bse_id
        row.sources.append("bse")
        if b.bse_id and b.bse_id != row.nse_symbol and row.nse_symbol:
            aliases.append(AliasRow(b.isin, b.bse_id, "bse_symbol", None, "bse"))
        if b.name and row.nse_symbol and normalise_name(b.name) != normalise_name(row.name):
            aliases.append(AliasRow(b.isin, b.name, "bse_name", None, "bse"))
    for f in fyers or []:
        row = rows.get(f.isin)
        if row is None:
            continue
        nse_ticker = f.ticker.startswith("NSE:")
        if row.fyers_symbol is None or (nse_ticker and not row.fyers_symbol.startswith("NSE:")):
            row.fyers_symbol = f.ticker
            if "fyers" not in row.sources:
                row.sources.append("fyers")

    by_symbol = {r.nse_symbol: r for r in rows.values() if r.nse_symbol}
    unresolved = 0
    for c in symbol_changes:
        target = by_symbol.get(current_symbol(c.new, symbol_changes))
        if target is None:
            unresolved += 1
            continue
        if c.old != target.nse_symbol:
            aliases.append(AliasRow(target.isin, c.old, "former_symbol", c.changed_on, "nse"))
    for nc in name_changes:
        target = by_symbol.get(current_symbol(nc.symbol, symbol_changes))
        if target is None:
            unresolved += 1
            continue
        for old in {nc.old_name, nc.new_name}:
            if old and normalise_name(old) != normalise_name(target.name):
                aliases.append(AliasRow(target.isin, old, "former_name", nc.changed_on, "nse"))
    if unresolved:
        warnings.append(f"{unresolved} symbol/name change(s) for symbols no longer listed")
    unique: dict[tuple[str, str, str], AliasRow] = {}
    for a in aliases:
        key = (a.isin, a.kind, a.alias.upper())
        prev = unique.get(key)
        if prev is None or (a.valid_until or date.min) > (prev.valid_until or date.min):
            unique[key] = a
    return MasterJoin(sorted(rows.values(), key=lambda r: r.isin), list(unique.values()), warnings)
