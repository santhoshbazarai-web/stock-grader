from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from app.core.config import Dataset, Provider, ProvidersConfig, load_config
from app.core.rate_limiter import RateLimitTimeout
from app.data.gaps import InMemoryGapRecorder
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.router import IST, NSE_CLOSE, DataRouter, Outcome, PriceProvider
from tests.conftest import REPO_CONFIG_DIR

# Wednesday 2025-06-18 18:15 IST, just after the eod_prices job would run.
NOW = datetime(2025, 6, 18, 18, 15, tzinfo=IST)
TODAY = NOW.date()
START, END = date(2025, 1, 1), TODAY


def bars(last: date, n: int = 5) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(last) - pd.Timedelta(days=i) for i in range(n)][::-1])
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100}, index=idx
    )


Behaviour = Callable[[], Any]


def returns(value: Any) -> Behaviour:
    return lambda: value


def raises(exc: Exception) -> Behaviour:
    def fn() -> Any:
        raise exc

    return fn


@dataclass
class FakePriceProvider:
    """Plays back one behaviour per call (the last one repeats)."""

    name: Provider
    behaviours: list[Behaviour]
    calls: int = 0

    def _next(self) -> Any:
        b = self.behaviours[min(self.calls, len(self.behaviours) - 1)]
        self.calls += 1
        return b()

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        result: pd.DataFrame = self._next()
        return result

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        return self.daily_ohlcv(index, start, end)

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        result: dict[str, float] = self._next()
        return result


@dataclass
class FakeFundamentalsProvider:
    name: Provider
    behaviours: list[Behaviour]
    calls: int = 0

    def annual(self, symbol: str) -> pd.DataFrame:
        self.calls += 1
        result: pd.DataFrame = self.behaviours[0]()
        return result

    def quarterly(self, symbol: str) -> pd.DataFrame:
        return self.annual(symbol)


@dataclass
class FakeLimiter:
    acquired: list[Provider] = field(default_factory=list)
    exhausted: set[Provider] = field(default_factory=set)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        if provider in self.exhausted:
            raise RateLimitTimeout(f"{provider}: no token within {timeout}s")
        self.acquired.append(provider)


@pytest.fixture
def config() -> ProvidersConfig:
    return load_config(REPO_CONFIG_DIR).providers


@dataclass
class Harness:
    router: DataRouter
    gaps: InMemoryGapRecorder
    limiter: FakeLimiter
    sleeps: list[float]


@pytest.fixture
def make_router(config: ProvidersConfig) -> Callable[..., Harness]:
    def make(*providers: object, now: datetime = NOW) -> Harness:
        gaps, limiter, sleeps = InMemoryGapRecorder(), FakeLimiter(), []
        router = DataRouter(
            {p.name: p for p in providers},  # type: ignore[attr-defined]
            config,
            limiter=limiter,
            gaps=gaps,
            clock=lambda: now,
            sleep=sleeps.append,
        )
        return Harness(router, gaps, limiter, sleeps)

    return make


def outcomes(result: Any) -> list[tuple[Provider, Outcome]]:
    return [(a.provider, a.outcome) for a in result.attempts]


# ───────────── success ─────────────


def test_primary_success(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.ok and not res.stale
    assert res.source is Provider.FYERS
    assert res.fetched_at == NOW
    assert res.as_of == datetime(2025, 6, 18, 15, 30, tzinfo=IST)
    assert outcomes(res) == [(Provider.FYERS, Outcome.OK)]
    assert kite.calls == 0
    assert h.limiter.acquired == [Provider.FYERS]
    assert res.reasons[-1] == "served by fyers"
    assert not h.gaps.open


# ───────────── empty ─────────────


@pytest.mark.parametrize("empty", [pd.DataFrame(), None])
def test_empty_falls_through(make_router: Callable[..., Harness], empty: Any) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(empty)])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.source is Provider.KITE
    assert outcomes(res) == [(Provider.FYERS, Outcome.EMPTY), (Provider.KITE, Outcome.OK)]
    assert fyers.calls == 1  # empty is not retried


def test_empty_ltp_dict_falls_through(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns({})])
    kite = FakePriceProvider(Provider.KITE, [returns({"TCS": 3500.0})])
    res = make_router(fyers, kite).router.ltp(["TCS"])
    assert res.data == {"TCS": 3500.0}
    assert res.source is Provider.KITE


# ───────────── exceptions ─────────────


def test_transient_error_retried_with_backoff_then_falls_through(
    make_router: Callable[..., Harness], config: ProvidersConfig
) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [raises(ProviderError("HTTP 503"))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)

    res = h.router.daily_ohlcv("TCS", START, END)

    n = config.retry.max_attempts
    assert fyers.calls == n
    base = config.retry.backoff_base_s
    assert h.sleeps == [min(config.retry.backoff_max_s, base * 2**i) for i in range(n - 1)]
    assert h.limiter.acquired == [Provider.FYERS] * n + [Provider.KITE]  # token per try
    assert res.source is Provider.KITE
    assert res.attempts[0].outcome is Outcome.ERROR
    assert res.attempts[0].tries == n
    assert "HTTP 503" in res.reasons[0]


def test_transient_error_recovers_on_retry(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(
        Provider.FYERS, [raises(ProviderError("timeout")), returns(bars(TODAY))]
    )
    h = make_router(fyers, FakePriceProvider(Provider.KITE, [returns(bars(TODAY))]))

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.source is Provider.FYERS
    assert res.attempts[0].tries == 2
    assert len(h.sleeps) == 1


def test_backoff_is_capped(config: ProvidersConfig) -> None:
    retry = config.retry.model_copy(
        update={"max_attempts": 6, "backoff_base_s": 1.0, "backoff_max_s": 3.0}
    )
    cfg = config.model_copy(update={"retry": retry})
    sleeps: list[float] = []
    fyers = FakePriceProvider(Provider.FYERS, [raises(ProviderError("x"))])
    router = DataRouter(
        {Provider.FYERS: fyers},
        cfg,
        limiter=FakeLimiter(),
        gaps=InMemoryGapRecorder(),
        clock=lambda: NOW,
        sleep=sleeps.append,
    )
    router.daily_ohlcv("TCS", START, END)
    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_provider_unavailable_not_retried(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [raises(ProviderUnavailable("token expired"))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert fyers.calls == 1 and h.sleeps == []
    assert outcomes(res) == [(Provider.FYERS, Outcome.UNAVAILABLE), (Provider.KITE, Outcome.OK)]
    assert res.attempts[0].detail == "token expired"


def test_unexpected_exception_not_retried_but_falls_through(
    make_router: Callable[..., Harness],
) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [raises(KeyError("close"))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    res = make_router(fyers, kite).router.daily_ohlcv("TCS", START, END)
    assert fyers.calls == 1
    assert res.source is Provider.KITE
    assert "KeyError" in (res.attempts[0].detail or "")


def test_rate_limit_timeout_falls_through(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)
    h.limiter.exhausted.add(Provider.FYERS)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert fyers.calls == 0
    assert outcomes(res) == [(Provider.FYERS, Outcome.RATE_LIMITED), (Provider.KITE, Outcome.OK)]


# ───────────── stale ─────────────


def test_stale_falls_through_to_fresh(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY - timedelta(days=3)))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY))])
    h = make_router(fyers, kite)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.source is Provider.KITE and not res.stale
    assert outcomes(res) == [(Provider.FYERS, Outcome.STALE), (Provider.KITE, Outcome.OK)]


def test_all_stale_returns_freshest_flagged(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY - timedelta(days=5)))])
    kite = FakePriceProvider(Provider.KITE, [returns(bars(TODAY - timedelta(days=2)))])
    yf = FakePriceProvider(Provider.YFINANCE, [returns(bars(TODAY - timedelta(days=4)))])
    h = make_router(fyers, kite, yf)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.stale and res.source is Provider.KITE
    assert res.as_of == datetime.combine(TODAY - timedelta(days=2), NSE_CLOSE, tzinfo=IST)
    assert outcomes(res) == [(Provider.FYERS, Outcome.STALE), (Provider.KITE, Outcome.STALE),
                             (Provider.NSE, Outcome.NOT_CONFIGURED),
                             (Provider.YFINANCE, Outcome.STALE)]  # fmt: skip
    assert not h.gaps.open  # stale data is not missing data
    assert "all providers stale" in res.reasons[-1]


def test_bar_staleness_anchored_at_close(make_router: Callable[..., Harness]) -> None:
    # Yesterday's bar next morning is fresh (30h from 15:30 close)...
    morning = datetime(2025, 6, 19, 10, 0, tzinfo=IST)
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY))])
    assert not make_router(fyers, now=morning).router.daily_ohlcv("TCS", START, END).stale
    # ...but stale once another full session has closed.
    late = datetime(2025, 6, 19, 21, 31, tzinfo=IST)
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY))])
    assert make_router(fyers, now=late).router.daily_ohlcv("TCS", START, END).stale


def test_explicit_as_of_attr_wins(make_router: Callable[..., Harness]) -> None:
    old_upload = pd.DataFrame({"pat": [1.0]}, index=pd.DatetimeIndex([pd.Timestamp("2025-03-31")]))
    old_upload.attrs["as_of"] = NOW - timedelta(days=200)
    fresh_upload = old_upload.copy()
    fresh_upload.attrs["as_of"] = NOW - timedelta(days=1)
    screener = FakeFundamentalsProvider(Provider.SCREENER, [returns(old_upload)])
    yf = FakeFundamentalsProvider(Provider.YFINANCE, [returns(fresh_upload)])

    res = make_router(screener, yf).router.fin_annual("TCS")

    assert outcomes(res) == [
        (Provider.SCREENER, Outcome.STALE),
        (Provider.YFINANCE, Outcome.OK),
    ]


def test_unknown_freshness_is_fresh(make_router: Callable[..., Harness]) -> None:
    # Fundamentals indexed by a year-old period end are not stale by index alone.
    df = pd.DataFrame({"pat": [1.0]}, index=pd.DatetimeIndex([pd.Timestamp("2024-03-31")]))
    screener = FakeFundamentalsProvider(Provider.SCREENER, [returns(df)])
    res = make_router(screener).router.fin_annual("TCS")
    assert res.source is Provider.SCREENER and not res.stale and res.as_of is None


def test_dataset_without_staleness_rule(
    make_router: Callable[..., Harness], config: ProvidersConfig
) -> None:
    assert Dataset.INDEX_OHLCV not in config.staleness_hours
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY - timedelta(days=30)))])
    res = make_router(fyers).router.index_ohlcv("NIFTY500", START, END)
    assert res.source is Provider.FYERS and not res.stale


# ───────────── all fail → data gap ─────────────


def test_all_fail_records_gap(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [raises(ProviderUnavailable("no token"))])
    kite = FakePriceProvider(Provider.KITE, [returns(pd.DataFrame())])
    yf = FakePriceProvider(Provider.YFINANCE, [raises(ProviderError("429"))])
    h = make_router(fyers, kite, yf)

    res = h.router.daily_ohlcv("TCS", START, END)

    assert res.data is None and res.source is None and not res.ok
    gap = h.gaps.open[(Dataset.DAILY_OHLCV, "TCS")]
    assert gap.providers_tried == ["fyers", "kite", "nse", "yfinance"]
    assert "no token" in gap.reason and "429" in gap.reason
    assert "recorded as a data gap" in res.reasons[-1]


def test_gap_resolved_after_later_success(make_router: Callable[..., Harness]) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns(None), returns(bars(TODAY))])
    h = make_router(fyers)

    assert h.router.daily_ohlcv("TCS", START, END).data is None
    assert (Dataset.DAILY_OHLCV, "TCS") in h.gaps.open
    assert h.router.daily_ohlcv("TCS", START, END).ok
    assert h.gaps.open == {}
    assert h.gaps.resolved == [(Dataset.DAILY_OHLCV, "TCS")]


def test_unconfigured_and_unsupported_providers_skipped(
    make_router: Callable[..., Harness],
) -> None:
    # screener is first for fin_annual but only a price provider is registered under it
    not_fundamentals = FakePriceProvider(Provider.SCREENER, [returns(bars(TODAY))])
    h = make_router(not_fundamentals)  # yfinance not configured at all

    res = h.router.fin_annual("TCS")

    assert outcomes(res) == [
        (Provider.SCREENER, Outcome.UNSUPPORTED),
        (Provider.YFINANCE, Outcome.NOT_CONFIGURED),
    ]
    assert res.data is None
    assert (Dataset.FIN_ANNUAL, "TCS") in h.gaps.open


def test_priority_order_comes_from_config(config: ProvidersConfig) -> None:
    cfg = config.model_copy(
        update={"priority": {**config.priority, Dataset.DAILY_OHLCV: [Provider.YFINANCE]}}
    )
    fyers = FakePriceProvider(Provider.FYERS, [returns(bars(TODAY))])
    yf = FakePriceProvider(Provider.YFINANCE, [returns(bars(TODAY))])
    router = DataRouter(
        {Provider.FYERS: fyers, Provider.YFINANCE: yf},
        cfg,
        limiter=FakeLimiter(),
        gaps=InMemoryGapRecorder(),
        clock=lambda: NOW,
        sleep=lambda _: None,
    )
    assert router.daily_ohlcv("TCS", START, END).source is Provider.YFINANCE
    assert fyers.calls == 0


def test_fetched_at_is_utc_by_default(config: ProvidersConfig) -> None:
    fyers = FakePriceProvider(Provider.FYERS, [returns({"TCS": 1.0})])
    router = DataRouter(
        {Provider.FYERS: fyers}, config, limiter=FakeLimiter(), gaps=InMemoryGapRecorder()
    )
    assert router.ltp(["TCS"]).fetched_at.tzinfo is UTC


def test_a_blocked_host_is_paused_then_skipped_without_burning_tokens(
    config: ProvidersConfig, redis_client: Any
) -> None:
    import uuid

    from app.core.circuit_breaker import CircuitBreaker
    from app.data.providers.web_session import blocked_message

    prefix = f"test-br-{uuid.uuid4().hex}"
    breaker = CircuitBreaker(redis_client, config.breaker, prefix=prefix)
    nse = FakePriceProvider(Provider.NSE, [raises(ProviderUnavailable(blocked_message("NSE")))])
    limiter = FakeLimiter()
    router = DataRouter({Provider.NSE: nse}, config, limiter=limiter, gaps=InMemoryGapRecorder(),
                        breaker=breaker, clock=lambda: NOW, sleep=lambda s: None)  # fmt: skip

    def fetch() -> Any:
        return router.fetch(Dataset.DAILY_OHLCV, PriceProvider, lambda p: p.daily_ohlcv(
            "TCS", START, END), symbol="TCS", providers=[Provider.NSE])  # fmt: skip

    try:
        assert fetch().attempts[0].detail == blocked_message("NSE")  # the 403 trips the breaker
        res = fetch()
        assert nse.calls == 1  # the second call never reached the host
        assert breaker.paused_until(Provider.NSE) is not None
        assert res.attempts[0].outcome is Outcome.UNAVAILABLE
        assert res.attempts[0].detail is not None
        assert res.attempts[0].detail.startswith("NSE paused until ")
        assert limiter.acquired == [Provider.NSE]  # only the first call took a token
    finally:
        for k in redis_client.scan_iter(f"{prefix}:*"):
            redis_client.delete(k)
