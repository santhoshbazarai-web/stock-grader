"""Exchange event feeds → one event frame (SPEC v0.2 §3.8, §10 ``events``). Pure functions.

Feeds (market-wide, read by date window):

- NSE corporate announcements, board meetings (the calendar), financial-results filings,
  promoter pledges (SAST reg. 31), SAST reg. 29 disclosures, insider trades (PIT) and the bulk /
  block deal files;
- BSE corporate announcements (its "Result" category is the results filing).

Every parser returns a frame with :data:`EVENT_COLUMNS`; ``data`` keeps the structured fields
(quantities, prices, parties). ``source_id`` is the feed's own id where it has one, else a
hash of the fields that identify the row, so re-reading a window never duplicates. The field
names are those NSE's and BSE's pages used when this was written; they change without notice,
so a payload of an unknown shape raises :class:`EventFormatError`, and a row missing what
identifies it is skipped with a warning (``attrs["warnings"]``) instead of guessed at.

:func:`classify` gives an event its category from ``jobs.event_classification``.
"""

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import EventClassificationConfig
from app.data.xbrl import statement_type_from_text
from app.db.enums import EventKind

IST = ZoneInfo("Asia/Kolkata")
EVENT_COLUMNS = [
    "kind", "source_id", "symbol", "isin", "bse_code", "company", "title", "detail",
    "event_date", "disseminated_at", "url", "data",
]  # fmt: skip
_DATE_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%Y%m%d")
_DATETIME_FORMATS = (
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S",
)  # fmt: skip


class EventFormatError(ValueError):
    """The feed's shape is not the one this parser knows."""


# ───────────────────────── helpers ─────────────────────────


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text if text and text not in ("-", "NA", "N.A.", "nan", "None", "null") else None


def _first(rec: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        if (v := _text(rec.get(k))) is not None:
            return v
    return None


def parse_date(value: Any) -> date | None:
    text = _text(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    dt = parse_datetime(text)
    return dt.date() if dt else None


def parse_datetime(value: Any) -> datetime | None:
    """Exchange timestamps are IST."""
    text = _text(value)
    if text is None:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _num(value: Any) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _key(*parts: Any) -> str:
    """A stable id for a feed row without one of its own."""
    raw = json.dumps([str(p) if p is not None else None for p in parts], ensure_ascii=True)
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


def _records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in (*keys, "data"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    raise EventFormatError(f"unexpected payload shape: {type(payload).__name__}"
                           + (f" with keys {sorted(payload)[:10]}" if isinstance(payload, dict)
                              else ""))  # fmt: skip


def _https(url: str | None, hosts: Iterable[str] | None = None) -> str | None:
    """Only https links (optionally on ``hosts``) are kept; they come from a third party."""
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    if hosts is not None and (parts.hostname or "").lower() not in set(hosts):
        return None
    return url


def _frame(rows: list[dict[str, Any]], warnings: list[str]) -> pd.DataFrame:
    unique = list({(r["kind"], r["source_id"]): r for r in rows}.values())
    df = pd.DataFrame(unique, columns=EVENT_COLUMNS).astype(object)
    df = df.where(df.notna(), None)
    df.attrs["warnings"] = warnings
    return df


def _row(kind: EventKind, source_id: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(EVENT_COLUMNS)
    row.update(kind=kind.value, source_id=source_id[:200], **fields)
    if row["symbol"]:
        row["symbol"] = str(row["symbol"]).strip().upper()
    if row["isin"]:
        row["isin"] = str(row["isin"]).strip().upper()[:12]
    row["data"] = {k: v for k, v in (row["data"] or {}).items() if v is not None} or None
    return row


def _dissem(rec: dict[str, Any], *keys: str) -> datetime | None:
    return next((ts for k in keys if (ts := parse_datetime(rec.get(k))) is not None), None)


# ───────────────────────── NSE ─────────────────────────


def parse_nse_announcements(payload: Any) -> pd.DataFrame:
    """``/api/corporate-announcements``: ``symbol``, ``sm_name``, ``sm_isin``, ``desc`` (the
    subject), ``attchmntText`` (the text), ``attchmntFile``, ``an_dt`` / ``sort_date`` and
    ``seq_id``."""
    rows, warnings = [], []
    for rec in _records(payload):
        subject = _first(rec, "desc", "subject")
        when = _dissem(rec, "an_dt", "sort_date", "dt", "exchdisstime")
        if subject is None or (_first(rec, "symbol", "sm_isin") is None):
            warnings.append(f"announcement without a subject or company: {sorted(rec)[:8]}")
            continue
        sid = _first(rec, "seq_id", "seqId") or _key(rec.get("symbol"), subject, when)
        rows.append(_row(
            EventKind.ANNOUNCEMENT, sid, symbol=_first(rec, "symbol"),
            isin=_first(rec, "sm_isin", "isin"), company=_first(rec, "sm_name", "companyName"),
            title=subject, detail=_first(rec, "attchmntText", "details"),
            event_date=when.date() if when else None, disseminated_at=when,
            url=_https(_first(rec, "attchmntFile")),
            data={"industry": _first(rec, "smIndustry")},
        ))  # fmt: skip
    return _frame(rows, warnings)


def parse_nse_board_meetings(payload: Any) -> pd.DataFrame:
    """``/api/corporate-board-meetings``: ``bm_symbol``, ``bm_date`` (the meeting),
    ``bm_purpose``, ``bm_desc``, ``sm_name``, ``sm_isin``, ``bm_timestamp``, ``attachment``."""
    rows, warnings = [], []
    for rec in _records(payload):
        symbol = _first(rec, "bm_symbol", "symbol")
        meeting = parse_date(rec.get("bm_date"))
        purpose = _first(rec, "bm_purpose", "purpose") or "Board meeting"
        if symbol is None or meeting is None:
            warnings.append(f"board meeting without a symbol or date: {sorted(rec)[:8]}")
            continue
        rows.append(_row(
            EventKind.BOARD_MEETING, f"{symbol}:{meeting.isoformat()}:{purpose[:120]}",
            symbol=symbol, isin=_first(rec, "sm_isin", "isin"),
            company=_first(rec, "sm_name", "companyName"),
            title=f"Board meeting: {purpose}", detail=_first(rec, "bm_desc", "desc"),
            event_date=meeting, disseminated_at=_dissem(rec, "bm_timestamp"),
            url=_https(_first(rec, "attachment")), data={"purpose": purpose},
        ))  # fmt: skip
    return _frame(rows, warnings)


def parse_nse_results_feed(payload: Any, hosts: list[str]) -> pd.DataFrame:
    """``/api/corporates-financial-results`` for all companies: ``symbol``, ``companyName``,
    ``isin``, ``fromDate`` / ``toDate``, ``consolidated``, ``audited``, ``relatingTo``,
    ``xbrl`` (the document) and the dissemination time (``broadCastDate``, ``exchdisstime``
    or ``filingDate``). One event per filing: its XBRL URL, else symbol + period + basis."""
    rows, warnings = [], []
    for rec in _records(payload):
        symbol = _first(rec, "symbol")
        end = parse_date(rec.get("toDate"))
        if symbol is None or end is None:
            warnings.append(f"results filing without a symbol or period: {sorted(rec)[:8]}")
            continue
        nature = statement_type_from_text(_first(rec, "consolidated") or "")
        basis = nature.value if nature else None
        xbrl = _first(rec, "xbrl")
        xbrl = _https(xbrl, hosts) if xbrl and xbrl.lower().endswith(".xml") else None
        start = parse_date(rec.get("fromDate"))
        relating = _first(rec, "relatingTo", "period")
        sid = xbrl or f"{symbol}:{end.isoformat()}:{basis or 'unknown'}:{relating or ''}"
        rows.append(_row(
            EventKind.RESULTS, sid, symbol=symbol, isin=_first(rec, "isin"),
            company=_first(rec, "companyName", "sm_name"),
            title=f"Financial results {relating + ' ' if relating else ''}"
                  f"(period ended {end:%d %b %Y}{', ' + basis if basis else ''})",
            event_date=end,
            disseminated_at=_dissem(rec, "broadCastDate", "exchdisstime", "filingDate"),
            url=xbrl,
            data={"period_start": start.isoformat() if start else None,
                  "period_end": end.isoformat(), "basis": basis,
                  "audited": _first(rec, "audited"), "relating_to": relating},
        ))  # fmt: skip
    return _frame(rows, warnings)


def parse_nse_pledges(payload: Any) -> pd.DataFrame:
    """``/api/corporate-pledgedata``: ``comName``, ``symbol`` (when given), ``shp`` (the
    period), promoter holding and ``numSharesPledged``, ``percPromoterShares`` (pledged, % of
    promoter holding), ``percTotShares`` (% of all shares), ``broadcastDt``."""
    rows, warnings = [], []
    for rec in _records(payload):
        company = _first(rec, "comName", "companyName")
        when = _dissem(rec, "broadcastDt", "compBroadcastDate", "disclosureToDate")
        if company is None and _first(rec, "symbol") is None:
            warnings.append(f"pledge disclosure without a company: {sorted(rec)[:8]}")
            continue
        pct_promoter = _num(rec.get("percPromoterShares"))
        pct_total = _num(rec.get("percTotShares"))
        title = "Promoter pledge disclosure"
        if pct_promoter is not None:
            title += f": {pct_promoter:g}% of promoter holding pledged"
        rows.append(_row(
            EventKind.PLEDGE, _key(company, _first(rec, "symbol"), rec.get("shp"), when,
                                   rec.get("numSharesPledged")),
            symbol=_first(rec, "symbol"), company=company, title=title,
            detail=_first(rec, "reasonForPledge", "remarks"),
            event_date=(when.date() if when else parse_date(rec.get("shp"))),
            disseminated_at=when,
            data={"shares_pledged": _num(rec.get("numSharesPledged")),
                  "promoter_shares": _num(rec.get("totPromoterHolding")),
                  "pledged_pct_of_promoter": pct_promoter, "pledged_pct_of_total": pct_total,
                  "period": _first(rec, "shp")},
        ))  # fmt: skip
    return _frame(rows, warnings)


def parse_nse_sast(payload: Any) -> pd.DataFrame:
    """``/api/corporate-sast-reg29``: ``symbol``, ``company``, ``acquirerName``,
    ``acqSaleType`` (acquisition / sale), share counts, ``date`` / ``timestamp``,
    ``attachement``."""
    rows, warnings = [], []
    for rec in _records(payload):
        symbol = _first(rec, "symbol")
        who = _first(rec, "acquirerName", "acqName")
        when = _dissem(rec, "timestamp", "date", "broadcastDt")
        if symbol is None or who is None:
            warnings.append(f"SAST disclosure without a symbol or acquirer: {sorted(rec)[:8]}")
            continue
        side = _first(rec, "acqSaleType", "acquisitionSale") or "Disclosure"
        qty = _num(rec.get("noOfShareAcq")) or _num(rec.get("noOfShareSale"))
        rows.append(_row(
            EventKind.SAST, _key(symbol, who, side, when, qty), symbol=symbol,
            company=_first(rec, "company", "companyName"),
            title=f"SAST reg. 29: {who}, {side.lower()}"
                  + (f" of {qty:,.0f} shares" if qty else ""),
            event_date=when.date() if when else parse_date(rec.get("date")),
            disseminated_at=when, url=_https(_first(rec, "attachement", "attachment")),
            data={"party": who, "side": side, "shares": qty,
                  "holding_after_pct": _num(rec.get("totAftShareAcqPer"))
                  or _num(rec.get("totAftShareAcq"))},
        ))  # fmt: skip
    return _frame(rows, warnings)


def parse_nse_pit(payload: Any) -> pd.DataFrame:
    """``/api/corporates-pit``: ``symbol``, ``company``, ``acqName``, ``personCategory``,
    ``secAcq`` (shares), ``secVal`` (₹), ``tdpTransactionType`` (Buy / Sell / Pledge ...),
    ``acqMode``, ``acqfromDt`` / ``acqtoDt``, ``date`` (dissemination), ``did``."""
    rows, warnings = [], []
    for rec in _records(payload):
        symbol = _first(rec, "symbol")
        who = _first(rec, "acqName")
        if symbol is None or who is None:
            warnings.append(f"insider trade without a symbol or person: {sorted(rec)[:8]}")
            continue
        category = _first(rec, "personCategory") or "person"
        side = _first(rec, "tdpTransactionType", "acqMode") or "transaction"
        qty, value = _num(rec.get("secAcq")), _num(rec.get("secVal"))
        when = _dissem(rec, "date", "intimDt", "broadcastDt")
        traded = parse_date(rec.get("acqtoDt")) or parse_date(rec.get("acqfromDt"))
        sid = _first(rec, "did") or _key(symbol, who, side, qty, value, traded, when)
        rows.append(_row(
            EventKind.INSIDER_TRADE, sid, symbol=symbol,
            company=_first(rec, "company", "companyName"),
            title=f"Insider trade: {who} ({category}) {side.lower()}"
                  + (f" {qty:,.0f} shares" if qty else "")
                  + (f", ₹{value / 1e7:,.2f} cr" if value else ""),
            detail=_first(rec, "acqMode", "remarks"), event_date=traded or (when.date()
                                                                           if when else None),
            disseminated_at=when,
            data={"party": who, "person_category": category, "side": side, "shares": qty,
                  "value_inr": value, "mode": _first(rec, "acqMode"),
                  "holding_before_pct": _num(rec.get("befAcqSharesPer")),
                  "holding_after_pct": _num(rec.get("afterAcqSharesPer"))},
        ))  # fmt: skip
    return _frame(rows, warnings)


_DEAL_HEADERS = {
    "date": ("date", "deal date"),
    "symbol": ("symbol",),
    "company": ("security name", "name of security"),
    "client": ("client name",),
    "side": ("buy/sell", "buy / sell"),
    "qty": ("quantity traded", "quantity"),
    "price": ("trade price / wght. avg. price", "trade price/wght. avg. price",
              "weighted average price", "trade price"),
}  # fmt: skip


def parse_nse_deals(text: str, kind: EventKind) -> pd.DataFrame:
    """``bulk.csv`` / ``block.csv`` on NSE archives (the latest trading day): Date, Symbol,
    Security Name, Client Name, Buy/Sell, Quantity Traded, Trade Price / Wght. Avg. Price."""
    if kind not in (EventKind.BULK_DEAL, EventKind.BLOCK_DEAL):
        raise ValueError(f"not a deal kind: {kind}")
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = [" ".join(h.split()).lower() for h in next(reader, [])]
    col = {k: next((header.index(n) for n in names if n in header), None)
           for k, names in _DEAL_HEADERS.items()}  # fmt: skip
    missing = [k for k in ("date", "symbol", "client", "side", "qty") if col[k] is None]
    if not header or missing:
        raise EventFormatError(f"deal file without columns {missing}: header {header[:8]}")
    label = "Bulk deal" if kind is EventKind.BULK_DEAL else "Block deal"
    rows, warnings = [], []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue

        def cell(k: str, cells: list[str] = cells) -> str | None:
            i = col[k]
            return _text(cells[i]) if i is not None and i < len(cells) else None

        day, symbol, client = parse_date(cell("date")), cell("symbol"), cell("client")
        if day is None or symbol is None or client is None:
            warnings.append(f"{label.lower()} row without a date, symbol or client: {cells[:4]}")
            continue
        side = (cell("side") or "").upper()
        qty, price = _num(cell("qty")), _num(cell("price"))
        rows.append(_row(
            kind, _key(day, symbol, client, side, qty, price), symbol=symbol,
            company=cell("company"),
            title=f"{label}: {client} {'bought' if side.startswith('B') else 'sold'}"
                  + (f" {qty:,.0f} shares" if qty else "")
                  + (f" at ₹{price:,.2f}" if price else ""),
            event_date=day,
            data={"client": client, "side": side or None, "shares": qty, "price": price},
        ))  # fmt: skip
    return _frame(rows, warnings)


# ───────────────────────── BSE ─────────────────────────


def parse_bse_announcements(
    payload: Any, *, results_categories: list[str], attachment_url: str
) -> tuple[pd.DataFrame, int | None]:
    """``AnnSubCategoryGetData``: ``Table`` (``NEWSID``, ``SCRIP_CD``, ``SLONGNAME``,
    ``NEWSSUB`` / ``HEADLINE``, ``MORE``, ``CATEGORYNAME``, ``SUBCATNAME``, ``DT_TM`` /
    ``NEWS_DT``, ``ATTACHMENTNAME``) and ``Table1`` (``ROWCNT``, the total). Announcements in
    ``results_categories`` are results filings. Returns (events, total rows or None)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Table"), list):
        raise EventFormatError("BSE announcements: no 'Table' list")
    total = None
    t1 = payload.get("Table1")
    if isinstance(t1, list) and t1 and isinstance(t1[0], dict):
        total = int(n) if (n := _num(t1[0].get("ROWCNT"))) is not None else None
    results = {c.strip().lower() for c in results_categories}
    rows, warnings = [], []
    for rec in payload["Table"]:
        if not isinstance(rec, dict):
            continue
        news_id, code = _first(rec, "NEWSID"), _first(rec, "SCRIP_CD")
        subject = _first(rec, "NEWSSUB", "HEADLINE")
        if news_id is None or code is None or subject is None:
            warnings.append(f"BSE announcement without an id, scrip or subject: {sorted(rec)[:8]}")
            continue
        category = _first(rec, "CATEGORYNAME")
        when = _dissem(rec, "DT_TM", "NEWS_DT", "DissemDT")
        attachment = _first(rec, "ATTACHMENTNAME")
        is_result = (category or "").lower() in results
        rows.append(_row(
            EventKind.RESULTS if is_result else EventKind.ANNOUNCEMENT, news_id,
            bse_code=code.split(".")[0], company=_first(rec, "SLONGNAME"), title=subject,
            detail=_first(rec, "MORE", "HEADLINE") if _first(rec, "MORE", "HEADLINE") != subject
            else None,
            event_date=when.date() if when else None, disseminated_at=when,
            url=_https(f"{attachment_url}{attachment}") if attachment else None,
            data={"category": category, "subcategory": _first(rec, "SUBCATNAME")},
        ))  # fmt: skip
    return _frame(rows, warnings), total


# ───────────────────────── classification ─────────────────────────


_SPACE = re.compile(r"\s+")


def classify(title: str, detail: str | None, cfg: EventClassificationConfig) -> str | None:
    """The first category (in config order) with a rule whose words all appear in the title
    or text (lower-cased); None when none matches."""
    text = _SPACE.sub(" ", f"{title} {detail or ''}".lower())
    for category, rules in cfg.categories.items():
        if any(all(word in text for word in rule) for rule in rules):
            return category
    return None


def is_results_meeting(purpose: str | None, purposes: list[str]) -> bool:
    """A board meeting whose purpose names results (``results_watch.results_purposes``)."""
    text = (purpose or "").lower()
    return any(p.lower() in text for p in purposes)


def normalise_company(name: str | None) -> str | None:
    """Company name for matching a feed that gives no symbol: lower case, punctuation and the
    legal suffix removed ("HDFC Bank Ltd." → "hdfc bank")."""
    if not name:
        return None
    text = re.sub(r"[^a-z0-9& ]+", " ", name.lower())
    text = re.sub(r"\b(limited|ltd|pvt|private|the)\b", " ", text)
    return " ".join(text.split()) or None
