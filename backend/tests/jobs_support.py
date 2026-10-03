"""Shared fixtures for job tests: a JobContext over the test database, real Redis, and a real
DataRouter in front of scripted fake providers."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from redis import Redis
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Provider, load_config
from app.data.events import EVENT_COLUMNS
from app.data.gaps import DbGapRecorder
from app.data.providers.base import ProviderUnavailable
from app.data.providers.nse import ANNUAL_REPORT_COLUMNS, RESULTS_COLUMNS
from app.data.raw_store import RawStore
from app.data.router import DataRouter
from app.data.symbol_master import (
    BseScrip,
    FyersSymbol,
    NameChange,
    NseListing,
    SymbolChange,
    parse_nse_equity_list,
    parse_nse_name_changes,
    parse_nse_symbol_changes,
)
from app.db.enums import EventKind
from app.db.models import Base
from app.jobs.runner import JobContext
from tests.conftest import REPO_CONFIG_DIR

# Friday 14 Jun 2024, 18:30 IST
NOW = datetime(2024, 6, 14, 13, 0, tzinfo=UTC)
TODAY = date(2024, 6, 14)


def ohlcv(closes: dict[str, float], volume: int = 1000) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in closes], name="date")
    c = list(closes.values())
    return pd.DataFrame(
        {"open": c, "high": c, "low": c, "close": c, "volume": [volume] * len(c)}, index=idx
    )


@dataclass
class FakePrices:
    """Price provider registered as fyers. ``bars[symbol]`` is sliced to the request."""

    name: Provider = Provider.FYERS
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    requests: list[tuple[str, date, date]] = field(default_factory=list)

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.requests.append((symbol, start, end))
        df = self.bars.get(symbol, ohlcv({}))
        return df.loc[pd.Timestamp(start) : pd.Timestamp(end)]

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        return self.daily_ohlcv(index, start, end)

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        return {}

    fyers_master: list[FyersSymbol] | None = None  # public symbol master (symbol_master job)

    def symbol_master(self) -> list[FyersSymbol]:
        if self.fyers_master is None:
            raise ProviderUnavailable("Fyers master unavailable")
        return self.fyers_master


@dataclass
class FakeNse:
    name: Provider = Provider.NSE
    actions: dict[str, pd.DataFrame] = field(default_factory=dict)
    action_requests: list[tuple[str, date, date]] = field(default_factory=list)
    delivery_frame: pd.DataFrame | None = None
    surveillance_frame: pd.DataFrame | None = None
    constituents: dict[str, pd.DataFrame] = field(default_factory=dict)
    holdings: dict[str, pd.DataFrame] = field(default_factory=dict)
    # results filings: symbol → listing rows (see RESULTS_COLUMNS); url → document bytes
    filings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    documents: dict[str, bytes] = field(default_factory=dict)
    filing_requests: list[str] = field(default_factory=list)
    document_requests: list[str] = field(default_factory=list)
    # annual reports: symbol → listing rows (see ANNUAL_REPORT_COLUMNS); url → document bytes
    reports: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    report_documents: dict[str, bytes] = field(default_factory=dict)
    report_requests: list[str] = field(default_factory=list)
    # symbol master: file name (tests/fixtures/symbols) → text; a missing file is unavailable
    symbol_files: dict[str, str] = field(default_factory=dict)
    # event feeds (SPEC §3.8): kind → frame (app.data.events columns); missing → no rows;
    # a kind in feed_errors fails
    feeds: dict[EventKind, pd.DataFrame] = field(default_factory=dict)
    feed_errors: set[EventKind] = field(default_factory=set)
    feed_requests: list[tuple[EventKind, date, date]] = field(default_factory=list)

    def events(self, kind: EventKind, start: date, end: date) -> pd.DataFrame:
        self.feed_requests.append((kind, start, end))
        if kind in self.feed_errors:
            raise ProviderUnavailable(f"{kind.value} feed blocked")
        return self.feeds.get(kind, pd.DataFrame(columns=EVENT_COLUMNS))

    def results_filings(self, symbol: str) -> pd.DataFrame:
        self.filing_requests.append(symbol)
        if symbol not in self.filings:
            raise ProviderUnavailable(f"no results list for {symbol}")
        return pd.DataFrame(self.filings[symbol], columns=RESULTS_COLUMNS).astype(object)

    def results_document(self, url: str) -> bytes:
        self.document_requests.append(url)
        if url not in self.documents:
            raise ProviderUnavailable(f"not found: {url}")
        return self.documents[url]

    def _symbol_file(self, name: str) -> str:
        if name not in self.symbol_files:
            raise ProviderUnavailable(f"{name} unavailable")
        return self.symbol_files[name]

    def equity_list(self) -> list[NseListing]:
        return parse_nse_equity_list(self._symbol_file("EQUITY_L.csv"))

    def symbol_changes(self) -> list[SymbolChange]:
        return parse_nse_symbol_changes(self._symbol_file("symbolchange.csv"))

    def name_changes(self) -> list[NameChange]:
        return parse_nse_name_changes(self._symbol_file("namechange.csv"))

    def annual_reports(self, symbol: str) -> pd.DataFrame:
        if symbol not in self.reports:
            raise ProviderUnavailable(f"no annual-report list for {symbol}")
        return pd.DataFrame(self.reports[symbol], columns=ANNUAL_REPORT_COLUMNS).astype(object)

    def annual_report_document(self, url: str) -> bytes:
        self.report_requests.append(url)
        if url not in self.report_documents:
            raise ProviderUnavailable(f"not found: {url}")
        return self.report_documents[url]

    def corporate_actions(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.action_requests.append((symbol, start, end))
        cols = ["ex_date", "action_type", "ratio_old", "ratio_new", "dividend_per_share",
                "record_date", "description"]  # fmt: skip
        df = self.actions.get(symbol, pd.DataFrame(columns=cols))
        return df[(df["ex_date"] >= start) & (df["ex_date"] <= end)] if not df.empty else df

    def delivery(self, day: date) -> pd.DataFrame:
        return self.delivery_frame if self.delivery_frame is not None else pd.DataFrame()

    def surveillance(self) -> pd.DataFrame:
        return self.surveillance_frame if self.surveillance_frame is not None else pd.DataFrame()

    def index_constituents(self, index: str) -> pd.DataFrame:
        return self.constituents.get(index, pd.DataFrame())

    def shareholding(self, symbol: str) -> pd.DataFrame:
        return self.holdings.get(symbol, pd.DataFrame())


@dataclass
class FakeBse:
    name: Provider = Provider.BSE
    scrips: list[BseScrip] | None = None
    announcements: pd.DataFrame | None = None  # None: no rows

    def events(self, kind: EventKind, start: date, end: date) -> pd.DataFrame:
        if kind is not EventKind.ANNOUNCEMENT:
            raise ProviderUnavailable("BSE: announcements only")
        return self.announcements if self.announcements is not None else \
            pd.DataFrame(columns=EVENT_COLUMNS)  # fmt: skip

    def scrip_master(self) -> list[BseScrip]:
        if self.scrips is None:
            raise ProviderUnavailable("BSE unavailable")
        return self.scrips


@dataclass
class FakeQuarterly:
    name: Provider = Provider.YFINANCE
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    annual_frames: dict[str, pd.DataFrame] = field(default_factory=dict)

    def annual(self, symbol: str) -> pd.DataFrame:
        return self.annual_frames.get(symbol, pd.DataFrame())

    def quarterly(self, symbol: str) -> pd.DataFrame:
        return self.frames.get(symbol, pd.DataFrame())


def action(ex: str, kind: str, old: float | None = None, new: float | None = None,
           dps: float | None = None) -> dict[str, Any]:  # fmt: skip
    return {"ex_date": date.fromisoformat(ex), "action_type": kind, "ratio_old": old,
            "ratio_new": new, "dividend_per_share": dps, "record_date": None,
            "description": f"{kind} test"}  # fmt: skip


class NoLimit:
    def acquire(self, provider: Provider, *, timeout: float) -> None:
        return None


@dataclass
class Env:
    ctx: JobContext
    prices: FakePrices
    nse: FakeNse
    quarterly: FakeQuarterly
    Session: sessionmaker[Session]
    bse: FakeBse

    def session(self) -> Session:
        return self.Session()


@pytest.fixture
def env(migrated_engine: Engine, redis_client: Redis, tmp_path: Path) -> Iterator[Env]:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    config = load_config(REPO_CONFIG_DIR)
    prices, nse, quarterly, bse = FakePrices(), FakeNse(), FakeQuarterly(), FakeBse()
    gaps = DbGapRecorder(factory)
    router = DataRouter(
        {
            Provider.FYERS: prices,
            Provider.NSE: nse,
            Provider.YFINANCE: quarterly,
            Provider.BSE: bse,
        },
        config.providers,
        limiter=NoLimit(),
        gaps=gaps,
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    ctx = JobContext(
        config, factory, router, redis_client, gaps, clock=lambda: NOW,
        raw_store=RawStore(tmp_path / "raw", clock=lambda: NOW),
    )  # fmt: skip
    yield Env(ctx, prices, nse, quarterly, factory, bse)
    with migrated_engine.begin() as conn:
        tables = ", ".join(Base.metadata.tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    for pattern in ("job-lock:*", "results-index:*"):
        for key in redis_client.scan_iter(pattern):
            redis_client.delete(key)


def watch(session: Session, instrument_id: int) -> None:
    """Put a stock on the Default watchlist (creating the list if the test DB has none)."""
    from app.db.models import Watchlist, WatchlistItem

    wl = session.scalar(select(Watchlist).where(Watchlist.name == "Default"))
    if wl is None:
        wl = Watchlist(name="Default")
        session.add(wl)
        session.flush()
    session.add(WatchlistItem(watchlist_id=wl.id, instrument_id=instrument_id))
