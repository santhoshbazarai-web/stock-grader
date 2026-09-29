"""Shared fixtures for job tests: a JobContext over the test database, real Redis, and a real
DataRouter in front of scripted fake providers."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
from redis import Redis
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Provider, load_config
from app.data.gaps import DbGapRecorder
from app.data.router import DataRouter
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


@dataclass
class FakeNse:
    name: Provider = Provider.NSE
    actions: dict[str, pd.DataFrame] = field(default_factory=dict)
    action_requests: list[tuple[str, date, date]] = field(default_factory=list)
    delivery_frame: pd.DataFrame | None = None
    surveillance_frame: pd.DataFrame | None = None
    constituents: dict[str, pd.DataFrame] = field(default_factory=dict)
    holdings: dict[str, pd.DataFrame] = field(default_factory=dict)

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
class FakeQuarterly:
    name: Provider = Provider.YFINANCE
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)

    def annual(self, symbol: str) -> pd.DataFrame:
        return pd.DataFrame()

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

    def session(self) -> Session:
        return self.Session()


@pytest.fixture
def env(migrated_engine: Engine, redis_client: Redis) -> Iterator[Env]:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    config = load_config(REPO_CONFIG_DIR)
    prices, nse, quarterly = FakePrices(), FakeNse(), FakeQuarterly()
    gaps = DbGapRecorder(factory)
    router = DataRouter(
        {Provider.FYERS: prices, Provider.NSE: nse, Provider.YFINANCE: quarterly},
        config.providers,
        limiter=NoLimit(),
        gaps=gaps,
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    ctx = JobContext(config, factory, router, redis_client, gaps, clock=lambda: NOW)
    yield Env(ctx, prices, nse, quarterly, factory)
    with migrated_engine.begin() as conn:
        tables = ", ".join(Base.metadata.tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    for key in redis_client.scan_iter("job-lock:*"):
        redis_client.delete(key)
