"""NSE public data: delivery % (``sec_bhavdata_full``), index constituents (niftyindices CSV),
surveillance lists (ASM long/short term, GSM, F&O ban), corporate actions, shareholding, and
financial-results filings (the list per symbol and each XBRL document, parsed by
``app.data.xbrl``).

NSE's JSON APIs reject clients without a browser-like session: :class:`NseSession` sends
browser headers, visits the homepage first to collect cookies (re-visiting after
``nse.cookie_ttl_s`` or on a 401/403), and takes a rate-limit token for every request after
the first of a call (the router pays for the first) — ``rate_limits.nse`` keeps it polite.
Archive CSVs are preferred where they exist. Respect NSE's terms of use; personal use only.

Response shapes are parsed defensively (NSE changes them without notice): unknown shapes
raise :class:`ProviderError` with the offending keys instead of returning partial data.
"""

import csv
import io
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy.orm import Session

from app.core.config import BhavcopyHistoryConfig, NseConfig, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.bhavcopy_store import BhavcopyStore
from app.data.canonical import labels_for, pick
from app.data.events import (
    EVENT_COLUMNS,
    EventFormatError,
    parse_nse_announcements,
    parse_nse_board_meetings,
    parse_nse_deals,
    parse_nse_pit,
    parse_nse_pledges,
    parse_nse_results_feed,
    parse_nse_sast,
)
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.raw_store import RawStore, RawStoreError
from app.data.symbol_master import (
    MasterFormatError,
    NameChange,
    NseListing,
    SymbolChange,
    parse_nse_equity_list,
    parse_nse_name_changes,
    parse_nse_symbol_changes,
)
from app.data.xbrl import audited_from_text, statement_type_from_text
from app.db.enums import CorporateActionType, EventKind, SurveillanceList

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,text/csv,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
EQUITY_SERIES = ("EQ", "BE")  # rolling-settlement equity series we track
DELIVERY_COLUMNS = [
    "symbol",
    "series",
    "date",
    "traded_qty",
    "deliverable_qty",
    "delivery_pct",
    "traded_value_cr",
]
CONSTITUENT_COLUMNS = ["symbol", "name", "industry", "series", "isin"]
SURVEILLANCE_COLUMNS = ["symbol", "list_name", "stage"]
CA_COLUMNS = [
    "ex_date",
    "action_type",
    "ratio_old",
    "ratio_new",
    "dividend_per_share",
    "record_date",
    "description",
]
ANNUAL_REPORT_COLUMNS = ["url", "fiscal_year", "disseminated_at"]
RESULTS_COLUMNS = [
    "url",
    "period_start",
    "period_end",
    "statement_type",
    "audited",
    "is_bank",
    "disseminated_at",
]
IST = ZoneInfo("Asia/Kolkata")
SHP_PARTIAL = (
    "NSE shareholding master has promoter/public split only; FII, DII, MF and pledge need a "
    "Screener upload or the XBRL filing"
)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _num(value: Any) -> float | None:
    text = str(value if value is not None else "").strip().replace(",", "")
    if text in ("", "-", "NA", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ───────────────────────── session ─────────────────────────


class NseSession:
    def __init__(
        self,
        config: NseConfig,
        *,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._limiter = limiter
        self._rl_timeout = rate_limit_timeout_s
        self._session_factory = session_factory
        self._clock = clock
        self._session = session_factory()
        self._session.headers.update(BROWSER_HEADERS)
        self._warmed_at: float | None = None
        self._requests = 0

    def begin_call(self) -> None:
        self._requests = 0

    def _send(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        if self._requests > 0 and self._limiter is not None:
            self._limiter.acquire(Provider.NSE, timeout=self._rl_timeout)
        self._requests += 1
        try:
            return self._session.get(url, params=params, timeout=self._cfg.request_timeout_s)
        except requests.RequestException as exc:
            raise ProviderError(f"NSE {url}: {type(exc).__name__}") from exc

    def _warm_up(self) -> None:
        """Visit the homepage so NSE sets its session cookies."""
        self._session = self._session_factory()
        self._session.headers.update(BROWSER_HEADERS)
        resp = self._send(f"{self._cfg.base_url}/")
        if resp.status_code >= 400:
            raise ProviderError(f"NSE homepage warm-up failed: HTTP {resp.status_code}")
        self._warmed_at = self._clock()

    def _stale(self) -> bool:
        return self._warmed_at is None or (self._clock() - self._warmed_at > self._cfg.cookie_ttl_s)

    def get(
        self, url: str, *, params: dict[str, str] | None = None, needs_cookies: bool = False
    ) -> requests.Response | None:
        """GET with NSE etiquette. ``None`` for 404 (e.g. no bhavcopy on a holiday)."""
        if needs_cookies and self._stale():
            self._warm_up()
        resp = self._send(url, params)
        if needs_cookies and resp.status_code in (401, 403):
            logger.info("NSE %s → HTTP %s; refreshing cookies once", url, resp.status_code)
            self._warm_up()
            resp = self._send(url, params)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise ProviderError(f"NSE {url}: HTTP {resp.status_code}")
        return resp

    def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        resp = self.get(url, params=params, needs_cookies=True)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"NSE {url}: response is not JSON (blocked?)") from exc


# ───────────────────────── parsers (pure) ─────────────────────────


def parse_bhavcopy(text: str) -> pd.DataFrame:
    """``sec_bhavdata_full_DDMMYYYY.csv`` → delivery frame (headers and cells are
    space-padded; non-delivery series carry ``-``)."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    header = {(f or "").strip() for f in reader.fieldnames or []}
    missing = {"SYMBOL", "SERIES", "DATE1", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"} - header
    if missing:
        raise ProviderError(f"unexpected bhavcopy header; missing {sorted(missing)}")
    rows = []
    for raw in reader:
        rec = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if rec.get("SERIES") not in EQUITY_SERIES:
            continue
        turnover_lacs = _num(rec.get("TURNOVER_LACS"))
        traded = _num(rec.get("TTL_TRD_QNTY"))
        deliv = _num(rec.get("DELIV_QTY"))
        rows.append(
            {
                "symbol": rec["SYMBOL"],
                "series": rec["SERIES"],
                "date": _parse_date(rec.get("DATE1")),
                "traded_qty": int(traded) if traded is not None else None,
                "deliverable_qty": int(deliv) if deliv is not None else None,
                "delivery_pct": _num(rec.get("DELIV_PER")),
                "traded_value_cr": turnover_lacs / 100 if turnover_lacs is not None else None,
            }
        )
    df = pd.DataFrame(rows, columns=DELIVERY_COLUMNS)
    return df.astype({"traded_qty": "Int64", "deliverable_qty": "Int64"})


BHAVCOPY_COLUMNS = [
    "symbol", "series", "date", "open", "high", "low", "close", "prev_close", "volume",
    "traded_value_cr", "deliverable_qty", "delivery_pct",
]  # fmt: skip


def parse_bhavcopy_ohlcv(text: str, series: list[str]) -> pd.DataFrame:
    """``sec_bhavdata_full`` → every ``series`` row with its raw OHLC, volume and delivery
    (the bhavcopy history builder). A row without a close is skipped (never 0)."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    header = {(f or "").strip() for f in reader.fieldnames or []}
    need = {"SYMBOL", "SERIES", "DATE1", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE",
            "TTL_TRD_QNTY"}  # fmt: skip
    if missing := need - header:
        raise ProviderError(f"unexpected bhavcopy header; missing {sorted(missing)}")
    rows, keep = [], set(series)
    for raw in reader:
        rec = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if rec.get("SERIES") not in keep or _num(rec.get("CLOSE_PRICE")) is None:
            continue
        vol, deliv, value = (_num(rec.get(k)) for k in ("TTL_TRD_QNTY", "DELIV_QTY",
                                                        "TURNOVER_LACS"))  # fmt: skip
        rows.append({
            "symbol": rec["SYMBOL"], "series": rec["SERIES"], "date": _parse_date(rec["DATE1"]),
            "open": _num(rec.get("OPEN_PRICE")), "high": _num(rec.get("HIGH_PRICE")),
            "low": _num(rec.get("LOW_PRICE")), "close": _num(rec.get("CLOSE_PRICE")),
            "prev_close": _num(rec.get("PREV_CLOSE")),
            "volume": int(vol) if vol is not None else None,
            "traded_value_cr": value / 100 if value is not None else None,
            "deliverable_qty": int(deliv) if deliv is not None else None,
            "delivery_pct": _num(rec.get("DELIV_PER")),
        })  # fmt: skip
    df = pd.DataFrame(rows, columns=BHAVCOPY_COLUMNS).astype(object)
    # a symbol listed in two kept series on one day: the first (EQ before BE in the file order)
    return df.where(df.notna(), None).drop_duplicates("symbol", keep="first", ignore_index=True)


def parse_constituents(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), dtype=str).rename(columns=lambda c: c.strip())
    expected = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
    if not expected <= set(df.columns):
        raise ProviderError(f"unexpected constituents header: {list(df.columns)}")
    out = pd.DataFrame(
        {
            "symbol": df["Symbol"].str.strip(),
            "name": df["Company Name"].str.strip(),
            "industry": df["Industry"].str.strip(),
            "series": df["Series"].str.strip(),
            "isin": df["ISIN Code"].str.strip(),
        }
    )
    return out[CONSTITUENT_COLUMNS]


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [r for r in payload["data"] if isinstance(r, dict)]
    raise ProviderError(f"unexpected NSE payload shape: {type(payload).__name__}")


def _stage(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        k = key.lower()
        if "stage" in k or "survindicator" in k:
            return str(value).strip() or None
    return None


def _flag(rec: dict[str, Any], list_name: SurveillanceList) -> dict[str, Any]:
    symbol = str(rec["symbol"]).strip()
    return {"symbol": symbol, "list_name": list_name.value, "stage": _stage(rec)}


def parse_asm(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not {"longterm", "shortterm"} & set(payload):
        keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
        raise ProviderError(f"unexpected ASM payload: {keys}")
    out = []
    for key, list_name in (
        ("longterm", SurveillanceList.ASM_LT),
        ("shortterm", SurveillanceList.ASM_ST),
    ):
        for rec in _records(payload.get(key) or {"data": []}):
            out.append({"symbol": str(rec["symbol"]).strip(), "list_name": list_name.value,
                        "stage": _stage(rec)})  # fmt: skip
    return out


def parse_gsm(payload: Any) -> list[dict[str, Any]]:
    return [
        {
            "symbol": str(r["symbol"]).strip(),
            "list_name": SurveillanceList.GSM.value,
            "stage": _stage(r),
        }
        for r in _records(payload)
    ]


_BAN_LINE = re.compile(r"^\s*\d+\s*,\s*([A-Z0-9&\-]+)\s*$")


def parse_fo_ban(text: str) -> list[dict[str, Any]]:
    """``fo_secban.csv``: a title line, then ``n,SYMBOL`` lines (or ``NIL``)."""
    return [
        {"symbol": m.group(1), "list_name": SurveillanceList.FNO_BAN.value, "stage": None}
        for line in text.splitlines()
        if (m := _BAN_LINE.match(line))
    ]


_RS = r"(?:rs|re|inr)\.?\s*/?-?\s*([\d]+(?:\.\d+)?)"


def parse_ca_subject(
    subject: str,
) -> tuple[CorporateActionType, float | None, float | None, float | None]:
    """NSE ``subject`` text → (type, ratio_old, ratio_new, dividend_per_share).

    Ratios are shares-before : shares-after. ``Bonus a:b`` (a new for every b held) →
    b : a+b. ``Split from Rs x to Rs y`` → 1 : x/y. Several dividends in one subject
    (final + special) are summed. Unparseable ratios come back as ``None``.
    """
    s = subject.lower()
    if "bonus" in s:
        m = re.search(r"(\d+)\s*:\s*(\d+)", s)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            return CorporateActionType.BONUS, b, a + b, None
        return CorporateActionType.BONUS, None, None, None
    if "split" in s or "sub-division" in s or "subdivision" in s:
        amounts = [float(x) for x in re.findall(_RS, s)]
        if len(amounts) >= 2 and amounts[1] > 0:
            return CorporateActionType.SPLIT, 1.0, amounts[0] / amounts[1], None
        return CorporateActionType.SPLIT, None, None, None
    if "dividend" in s:
        amounts = [float(x) for x in re.findall(_RS, s)]
        return CorporateActionType.DIVIDEND, None, None, (sum(amounts) if amounts else None)
    if "rights" in s:
        return CorporateActionType.RIGHTS, None, None, None
    return CorporateActionType.OTHER, None, None, None


def parse_corporate_actions(payload: Any) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    merged: dict[tuple[date, str], dict[str, Any]] = {}
    for rec in _records(payload):
        subject = str(rec.get("subject", "")).strip()
        ex_date = _parse_date(rec.get("exDate"))
        if ex_date is None:
            warnings.append(f"skipped '{subject}': no ex-date")
            continue
        kind, old, new, dps = parse_ca_subject(subject)
        if kind in (CorporateActionType.BONUS, CorporateActionType.SPLIT) and old is None:
            warnings.append(f"could not parse ratio from '{subject}' ({ex_date})")
        key = (ex_date, kind.value)
        if key in merged:  # one row per (ex_date, type): sum dividends, join descriptions
            row = merged[key]
            if dps is not None:
                row["dividend_per_share"] = (row["dividend_per_share"] or 0.0) + dps
            row["description"] += f"; {subject}"
            continue
        merged[key] = {
            "ex_date": ex_date,
            "action_type": kind.value,
            "ratio_old": old,
            "ratio_new": new,
            "dividend_per_share": dps,
            "record_date": _parse_date(rec.get("recDate")),
            "description": subject,
        }
    df = pd.DataFrame(list(merged.values()), columns=CA_COLUMNS)
    return df.sort_values("ex_date", ignore_index=True), warnings


def parse_shareholding(payload: Any) -> pd.DataFrame:
    labels = labels_for("nse", "shareholding")
    records = {}
    for rec in _records(payload):
        period_end = _parse_date(rec.get("date"))
        if period_end is None:
            continue
        values = {k: _num(v) for k, v in rec.items()}
        row: dict[str, Any] = {name: pick(values, lbls) for name, lbls in labels.items()}
        row["filing_date"] = _parse_date(rec.get("submissionDate"))
        records[pd.Timestamp(period_end)] = row
    df = pd.DataFrame.from_dict(records, orient="index")
    df.index = pd.DatetimeIndex(df.index, name="period_end")
    return df.sort_index()


def _parse_datetime(value: Any) -> datetime | None:
    """NSE timestamps ("11-Jan-2024 16:45:12") are IST."""
    text = str(value or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def xbrl_url_allowed(url: str, hosts: list[str]) -> bool:
    """Documents come from a third-party payload: fetch only https URLs on NSE's hosts."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in hosts


def _flag_yn(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    return True if text in ("y", "yes") else False if text in ("n", "no") else None


def parse_results_index(payload: Any, hosts: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """``corporates-financial-results`` → one row per XBRL document (deduplicated by URL).

    Uses ``xbrl`` (document URL), ``fromDate``/``toDate``, ``consolidated``, ``audited``,
    ``bank`` and the dissemination time (``broadCastDate``, else ``exchdisstime``, else
    ``filingDate``). Filings without an XBRL document, or whose URL is not an https URL on
    ``hosts``, are skipped with a warning."""
    rows: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for rec in _records(payload):
        period = f"{rec.get('fromDate')}..{rec.get('toDate')}"
        url = str(rec.get("xbrl") or "").strip()
        if not url.lower().endswith(".xml"):
            warnings.append(f"no XBRL document for {period} ({url or 'none'})")
            continue
        if not xbrl_url_allowed(url, hosts):
            warnings.append(f"skipped XBRL URL outside {hosts}: {url}")
            continue
        disseminated = next(
            (
                ts
                for key in ("broadCastDate", "exchdisstime", "filingDate")
                if (ts := _parse_datetime(rec.get(key))) is not None
            ),
            None,
        )
        basis = statement_type_from_text(str(rec.get("consolidated") or ""))
        rows.setdefault(
            url,
            {
                "url": url,
                "period_start": _parse_date(rec.get("fromDate")),
                "period_end": _parse_date(rec.get("toDate")),
                "statement_type": basis.value if basis else None,
                "audited": audited_from_text(str(rec.get("audited") or "")),
                "is_bank": _flag_yn(rec.get("bank")),
                "disseminated_at": disseminated,
            },
        )
    df = pd.DataFrame(list(rows.values()), columns=RESULTS_COLUMNS).astype(object)
    return df.where(df.notna(), None), warnings


def parse_annual_reports_index(payload: Any, hosts: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """``annual-reports`` → one row per document (deduplicated by URL).

    Uses ``fileName`` (the PDF or ZIP URL), ``toYr`` (the fiscal year's end year; ``fromYr`` + 1
    when absent) and the dissemination time (``broadcast_dttm``, else ``disseminationDateTime``,
    else ``submissionDate``). The field names are as NSE's page used them when this was
    written; an entry without a document or year is skipped with a warning."""
    rows: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for rec in _records(payload):
        url = str(rec.get("fileName") or rec.get("file") or "").strip()
        year = _int(rec.get("toYr"))
        if year is None and (start := _int(rec.get("fromYr"))) is not None:
            year = start + 1
        if not url or year is None:
            warnings.append(f"annual report without a document or year: {sorted(rec)}")
            continue
        if not xbrl_url_allowed(url, hosts):
            warnings.append(f"skipped annual report URL outside {hosts}: {url}")
            continue
        disseminated = next(
            (
                ts
                for key in ("broadcast_dttm", "disseminationDateTime", "submissionDate")
                if (ts := _parse_datetime(rec.get(key))) is not None
            ),
            None,
        )
        rows.setdefault(url, {"url": url, "fiscal_year": year, "disseminated_at": disseminated})
    df = pd.DataFrame(list(rows.values()), columns=ANNUAL_REPORT_COLUMNS).astype(object)
    return df.where(df.notna(), None), warnings


def _int(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


# ───────────────────────── provider ─────────────────────────


class NseProvider:
    """Implements DeliveryProvider, ConstituentsProvider, SurveillanceProvider,
    CorporateActionsProvider, ShareholdingProvider, ResultsFilingsProvider,
    AnnualReportsProvider, NseSymbolFilesProvider, EventsProvider, and PriceProvider /
    BhavcopyHistoryProvider through the bhavcopy history (only when built with a store)."""

    name = Provider.NSE

    def __init__(
        self,
        config: NseConfig,
        session: NseSession,
        raw_store: RawStore | None = None,
        *,
        history: BhavcopyStore | None = None,
        bhavcopy: BhavcopyHistoryConfig | None = None,
        today: Callable[[], date] = lambda: datetime.now(IST).date(),
    ) -> None:
        self._cfg = config
        self._http = session
        self._raw = raw_store
        self._history = history if bhavcopy is not None else None
        self._bhav = bhavcopy
        self._today = today

    # ───────────── bhavcopy: delivery + the OHLCV history builder ─────────────

    def _bhavcopy_url(self, day: date) -> str:
        return f"{self._cfg.archives_url}/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv"

    def _load_day(self, day: date) -> str:
        """Fetch one archive file into the history. → loaded | holiday | not_out."""
        assert self._history is not None and self._bhav is not None
        resp = self._http.get(self._bhavcopy_url(day))
        now = datetime.now(IST)
        if resp is None:
            if (self._today() - day).days >= self._bhav.holiday_after_days:
                self._history.mark_holiday(day, now)
                return "holiday"
            return "not_out"
        raw = self._cache(f"sec_bhavdata_full_{day:%d%m%Y}.csv", resp.content)
        df = parse_bhavcopy_ohlcv(resp.content.decode("utf-8", errors="replace"),
                                  self._bhav.series)  # fmt: skip
        self._history.save_day(day, df, raw, now)
        return "loaded"

    def delivery(self, day: date) -> pd.DataFrame:
        self._http.begin_call()
        if self._history is not None:  # one file serves every symbol's bar and delivery
            status = self._history.days(day, day).get(day) or self._load_day(day)
            if status == "loaded":
                return self._history.delivery(day, DELIVERY_COLUMNS)
            df = pd.DataFrame(columns=DELIVERY_COLUMNS)
            df.attrs["warnings"] = [f"no bhavcopy for {day.isoformat()} ({status})"]
            return df
        resp = self._http.get(self._bhavcopy_url(day))
        if resp is None:
            df = pd.DataFrame(columns=DELIVERY_COLUMNS)
            df.attrs["warnings"] = [f"no bhavcopy for {day.isoformat()} (holiday or not yet out)"]
            return df
        return parse_bhavcopy(resp.text)

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Raw daily bars from the bhavcopy history (the price fallback after the brokers).
        Up to ``bhavcopy.on_demand_max_days`` missing days are fetched now; a longer gap means
        the history is not built that far back yet (``bhavcopy_history`` job), so the router
        moves on rather than get a partial series."""
        if self._history is None or self._bhav is None:
            raise ProviderUnavailable("NSE bhavcopy history is not set up")
        self._http.begin_call()
        end = min(end, self._today())
        missing = self._history.missing(start, end)
        if len(missing) > self._bhav.on_demand_max_days:
            raise ProviderUnavailable(
                f"bhavcopy history lacks {len(missing)} trading days from {missing[0]} "
                "(built nightly by bhavcopy_history)")  # fmt: skip
        not_out = [d for d in missing if self._load_day(d) == "not_out"]
        df = self._history.ohlcv(symbol, start, end)
        if not_out:
            df.attrs["warnings"] = [f"no bhavcopy yet for {', '.join(map(str, not_out))}"]
        return df

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        raise ProviderUnavailable("NSE bhavcopy has no index bars")

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        raise ProviderUnavailable("NSE archives have no live prices")

    def load_bhavcopy_days(self, days: list[date]) -> dict[str, list[str]]:
        """Backfill: fetch each day not yet stored (newest first as given). Returns the days
        per outcome: loaded / holiday / not_out."""
        if self._history is None:
            raise ProviderUnavailable("NSE bhavcopy history is not set up")
        self._http.begin_call()
        out: dict[str, list[str]] = {"loaded": [], "holiday": [], "not_out": []}
        known = self._history.days(min(days), max(days)) if days else {}
        for d in days:
            if d not in known:
                out[self._load_day(d)].append(d.isoformat())
        return out

    def index_constituents(self, index: str) -> pd.DataFrame:
        self._http.begin_call()
        filename = self._cfg.index_constituent_files.get(index.replace(" ", "").upper())
        if filename is None:
            raise ProviderUnavailable(f"NSE: no constituents file configured for {index}")
        resp = self._http.get(f"{self._cfg.niftyindices_url}/IndexConstituent/{filename}")
        if resp is None:
            raise ProviderError(f"NSE: constituents file {filename} not found")
        return parse_constituents(resp.text)

    def surveillance(self) -> pd.DataFrame:
        """All current ASM/GSM/F&O-ban listings; any source failing fails the whole call
        (a partial list would silently under-apply the knock-out filter)."""
        self._http.begin_call()
        rows: list[dict[str, Any]] = []
        for path, parse in (("reportASM", parse_asm), ("reportGSM", parse_gsm)):
            payload = self._http.get_json(f"{self._cfg.base_url}/api/{path}")
            if payload is None:
                raise ProviderError(f"NSE {path} not found")
            rows += parse(payload)  # parse before the next request: fail fast
        ban = self._http.get(f"{self._cfg.archives_url}/content/fo/fo_secban.csv")
        if ban is None:
            raise ProviderError("NSE F&O ban list not found")
        rows += parse_fo_ban(ban.text)
        df = pd.DataFrame(rows, columns=SURVEILLANCE_COLUMNS).astype(object)
        return df.where(df.notna(), None)

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self._http.begin_call()
        payload = self._http.get_json(
            f"{self._cfg.base_url}/api/corporates-corporateActions",
            params={
                "index": "equities",
                "symbol": symbol.strip().upper(),
                "from_date": f"{start:%d-%m-%Y}",
                "to_date": f"{end:%d-%m-%Y}",
            },
        )
        df, warnings = parse_corporate_actions(payload if payload is not None else [])
        df.attrs["warnings"] = warnings
        return df

    def shareholding(self, symbol: str) -> pd.DataFrame:
        self._http.begin_call()
        payload = self._http.get_json(
            f"{self._cfg.base_url}/api/corporate-share-holdings-master",
            params={"index": "equities", "symbol": symbol.strip().upper()},
        )
        df = parse_shareholding(payload if payload is not None else [])
        df.attrs["warnings"] = [SHP_PARTIAL]
        return df

    def results_filings(self, symbol: str) -> pd.DataFrame:
        """Every results filing NSE lists for ``symbol`` (all ``results.periods``)."""
        self._http.begin_call()
        cfg = self._cfg.results
        frames, warnings = [], []
        for period in cfg.periods:
            payload = self._http.get_json(
                f"{self._cfg.base_url}{cfg.index_path}",
                params={"index": "equities", "symbol": symbol.strip().upper(), "period": period},
            )
            if self._raw is not None and payload is not None:  # cache before parsing (§3.2a)
                name = f"results_{symbol.strip().upper()}_{period}.json"
                try:
                    self._raw.save("nse", name, json.dumps(payload).encode())
                except RawStoreError as exc:
                    raise ProviderUnavailable(str(exc)) from exc
            df, w = parse_results_index(payload if payload is not None else [], cfg.xbrl_hosts)
            frames.append(df)
            warnings += w
        out = pd.concat(frames, ignore_index=True).drop_duplicates("url", ignore_index=True)
        out.attrs["warnings"] = warnings
        return out

    def results_document(self, url: str) -> bytes:
        cfg = self._cfg.results
        if not xbrl_url_allowed(url, cfg.xbrl_hosts):
            raise ProviderUnavailable(f"XBRL URL not on an allowed host: {url}")
        self._http.begin_call()
        resp = self._http.get(url)
        if resp is None:
            raise ProviderUnavailable(f"XBRL document not found: {url}")
        if len(resp.content) > cfg.max_xbrl_bytes:
            raise ProviderUnavailable(f"XBRL document larger than {cfg.max_xbrl_bytes} bytes")
        return resp.content

    def annual_reports(self, symbol: str) -> pd.DataFrame:
        """Every annual report NSE lists for ``symbol``."""
        self._http.begin_call()
        cfg = self._cfg.annual_reports
        sym = symbol.strip().upper()
        payload = self._http.get_json(f"{self._cfg.base_url}{cfg.index_path}",
                                      params={"index": "equities", "symbol": sym})  # fmt: skip
        if self._raw is not None and payload is not None:  # cache before parsing (§3.2a)
            try:
                self._raw.save("nse", f"annual_reports_{sym}.json", json.dumps(payload).encode())
            except RawStoreError as exc:
                raise ProviderUnavailable(str(exc)) from exc
        df, warnings = parse_annual_reports_index(payload if payload is not None else [],
                                                  cfg.hosts)  # fmt: skip
        df.attrs["warnings"] = warnings
        return df

    def annual_report_document(self, url: str) -> bytes:
        cfg = self._cfg.annual_reports
        if not xbrl_url_allowed(url, cfg.hosts):
            raise ProviderUnavailable(f"annual report URL not on an allowed host: {url}")
        self._http.begin_call()
        resp = self._http.get(url)
        if resp is None:
            raise ProviderUnavailable(f"annual report not found: {url}")
        if len(resp.content) > cfg.max_bytes:
            raise ProviderUnavailable(f"annual report larger than {cfg.max_bytes} bytes")
        return resp.content

    # ───────────── event feeds (SPEC §3.8, §10 events) ─────────────

    def _cache(self, name: str, content: bytes) -> str | None:
        """Cache before parsing (§3.2a); the path relative to the cache root."""
        if self._raw is None:
            return None
        try:
            return self._raw.relative(self._raw.save("nse", name, content))
        except RawStoreError as exc:
            raise ProviderUnavailable(str(exc)) from exc

    def events(self, kind: EventKind, start: date, end: date) -> pd.DataFrame:
        """One market-wide feed for [start, end]: JSON feeds in windows of at most
        ``events.max_days_per_request`` days; the bulk / block deal files (latest trading day
        only) ignore the window."""
        self._http.begin_call()
        cfg = self._cfg.events
        if kind in (EventKind.BULK_DEAL, EventKind.BLOCK_DEAL):
            path = cfg.bulk_deals_path if kind is EventKind.BULK_DEAL else cfg.block_deals_path
            resp = self._http.get(f"{self._cfg.archives_url}{path}")
            if resp is None:
                raise ProviderError(f"NSE {path} not found")
            raw = self._cache(f"{kind.value}.csv", resp.content)
            try:
                df = parse_nse_deals(resp.content.decode("utf-8", errors="replace"), kind)
            except EventFormatError as exc:
                raise ProviderError(f"NSE {kind.value}: {exc}") from exc
            df["raw_path"] = raw
            return df
        paths: dict[EventKind, str] = {
            EventKind.ANNOUNCEMENT: cfg.announcements_path,
            EventKind.BOARD_MEETING: cfg.board_meetings_path,
            EventKind.RESULTS: cfg.results_path,
            EventKind.PLEDGE: cfg.pledge_path,
            EventKind.SAST: cfg.sast_path,
            EventKind.INSIDER_TRADE: cfg.pit_path,
        }
        frames, warnings = [], []
        periods = cfg.results_periods if kind is EventKind.RESULTS else [None]
        for lo, hi in _windows(start, end, cfg.max_days_per_request):
            for period in periods:
                params = {"index": "equities", "from_date": f"{lo:%d-%m-%Y}",
                          "to_date": f"{hi:%d-%m-%Y}"}  # fmt: skip
                if period is not None:
                    params["period"] = period
                payload = self._http.get_json(f"{self._cfg.base_url}{paths[kind]}", params)
                if payload is None:
                    continue
                raw = self._cache(f"{kind.value}_{lo:%Y%m%d}_{hi:%Y%m%d}{'_' + period if period
                                   else ''}.json", json.dumps(payload).encode())  # fmt: skip
                try:
                    df = _parse_feed(kind, payload, self._cfg.results.xbrl_hosts)
                except EventFormatError as exc:
                    raise ProviderError(f"NSE {kind.value}: {exc}") from exc
                df["raw_path"] = raw
                warnings += df.attrs.get("warnings", [])
                frames.append(df)
        if not frames:
            return pd.DataFrame(columns=[*EVENT_COLUMNS, "raw_path"])
        out = pd.concat(frames, ignore_index=True).drop_duplicates(["kind", "source_id"],
                                                                   ignore_index=True)  # fmt: skip
        out.attrs["warnings"] = warnings[:50]
        return out

    # ───────────── symbol master (SPEC §3.5) ─────────────

    def _archive_text(self, path: str, name: str) -> str:
        resp = self._http.get(f"{self._cfg.archives_url}{path}")
        if resp is None:
            raise ProviderError(f"NSE {path} not found")
        if self._raw is not None:  # cache before parsing (§3.2a)
            try:
                self._raw.save("nse", name, resp.content)
            except RawStoreError as exc:
                raise ProviderUnavailable(str(exc)) from exc
        return resp.content.decode("utf-8", errors="replace")

    def equity_list(self) -> list[NseListing]:
        self._http.begin_call()
        text = self._archive_text(self._cfg.symbol_files.equity_list_path, "EQUITY_L.csv")
        try:
            return parse_nse_equity_list(text)
        except MasterFormatError as exc:
            raise ProviderError(str(exc)) from exc

    def symbol_changes(self) -> list[SymbolChange]:
        self._http.begin_call()
        text = self._archive_text(self._cfg.symbol_files.symbol_changes_path, "symbolchange.csv")
        try:
            return parse_nse_symbol_changes(text)
        except MasterFormatError as exc:
            raise ProviderError(str(exc)) from exc

    def name_changes(self) -> list[NameChange]:
        self._http.begin_call()
        text = self._archive_text(self._cfg.symbol_files.name_changes_path, "namechange.csv")
        try:
            return parse_nse_name_changes(text)
        except MasterFormatError as exc:
            raise ProviderError(str(exc)) from exc


def _windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """[start, end] in consecutive pieces of at most ``days`` days, newest first."""
    out, hi = [], end
    while hi >= start:
        lo = max(start, hi - timedelta(days=days - 1))
        out.append((lo, hi))
        hi = lo - timedelta(days=1)
    return out


def _parse_feed(kind: EventKind, payload: Any, hosts: list[str]) -> pd.DataFrame:
    if kind is EventKind.ANNOUNCEMENT:
        return parse_nse_announcements(payload)
    if kind is EventKind.BOARD_MEETING:
        return parse_nse_board_meetings(payload)
    if kind is EventKind.RESULTS:
        return parse_nse_results_feed(payload, hosts)
    if kind is EventKind.PLEDGE:
        return parse_nse_pledges(payload)
    if kind is EventKind.SAST:
        return parse_nse_sast(payload)
    return parse_nse_pit(payload)


def build_nse_provider(
    config: ProvidersConfig,
    limiter: Limiter | None,
    raw_store: RawStore | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> NseProvider:
    session = NseSession(
        config.nse, limiter=limiter, rate_limit_timeout_s=config.retry.rate_limit_timeout_s
    )
    history = BhavcopyStore(session_factory) if session_factory is not None else None
    return NseProvider(config.nse, session, raw_store, history=history, bhavcopy=config.bhavcopy)
