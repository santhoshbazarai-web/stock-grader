"""Kite provider tests. HTTP is served from tests/fixtures/kite via ``responses`` through the
real kiteconnect SDK; any unmatched request raises, so nothing reaches the network."""

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
import pytest
import responses
from redis import Redis
from requests import PreparedRequest

from app.core.config import ApiLimits, Dataset, Provider, load_config
from app.data.gaps import InMemoryGapRecorder
from app.data.providers.base import (
    IndexPriceProvider,
    PriceProvider,
    ProviderError,
    ProviderUnavailable,
)
from app.data.providers.kite import (
    IST,
    KiteAuth,
    KiteProvider,
    build_kite_provider,
    next_daily_expiry,
)
from app.data.providers.kite_instruments import (
    KiteInstruments,
    MemoryInstrumentStore,
    RedisInstrumentStore,
    build_token_map,
)
from app.data.router import DataRouter, Outcome
from tests.conftest import REPO_CONFIG_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "kite"
ROOT = "https://api.kite.trade"
INSTRUMENTS_URL = f"{ROOT}/instruments/NSE"
HISTORICAL_URL = re.compile(rf"{re.escape(ROOT)}/instruments/historical/(\d+)/day")
LTP_URL = f"{ROOT}/quote/ltp"
SESSION_URL = f"{ROOT}/session/token"
API_KEY = "kitekey"
TCS_TOKEN = 2953217
LIMITS = ApiLimits(history_max_days=2000, quotes_max_symbols=1000)
KITE_EXPIRY = time(6, 0)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def instruments_route(http: responses.RequestsMock) -> responses.BaseResponse:
    return http.add(
        responses.GET,
        INSTRUMENTS_URL,
        body=(FIXTURES / "instruments_nse.csv").read_text(),
        content_type="text/csv",
    )


@dataclass
class HistoricalServer:
    requests: list[dict[str, str]] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    error: tuple[int, str] | None = None

    def __call__(self, request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        url = request.url or ""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.requests.append(q)
        match = HISTORICAL_URL.match(url)
        assert match
        self.tokens.append(int(match.group(1)))
        self.headers.append(str(request.headers.get("Authorization")))
        headers = {"Content-Type": "application/json"}
        if self.error:
            status, name = self.error
            return status, headers, json.dumps(fixture(name))
        frm, to = pd.Timestamp(q["from"]), pd.Timestamp(q["to"])
        body = fixture("historical_tcs_day.json")
        body["data"]["candles"] = [
            c for c in body["data"]["candles"] if frm <= pd.Timestamp(c[0]).tz_localize(None) <= to
        ]
        return 200, headers, json.dumps(body)

    @property
    def windows(self) -> list[tuple[str, str]]:
        return [(r["from"], r["to"]) for r in self.requests]


@pytest.fixture
def historical(
    http: responses.RequestsMock, instruments_route: responses.BaseResponse
) -> HistoricalServer:
    server = HistoricalServer()
    http.add_callback(responses.GET, HISTORICAL_URL, callback=server)
    return server


@dataclass
class CountingLimiter:
    calls: list[Provider] = field(default_factory=list)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        self.calls.append(provider)


def make_provider(
    token: Callable[[], str | None] = lambda: "tok",
    limits: ApiLimits = LIMITS,
    limiter: CountingLimiter | None = None,
    store: MemoryInstrumentStore | None = None,
) -> KiteProvider:
    return KiteProvider(
        API_KEY,
        token,
        limits,
        store if store is not None else MemoryInstrumentStore(),
        20,
        limiter=limiter,
    )


def warm_store() -> MemoryInstrumentStore:
    from kiteconnect import KiteConnect

    rows = KiteConnect(api_key="x")._parse_instruments(
        (FIXTURES / "instruments_nse.csv").read_bytes()
    )
    store = MemoryInstrumentStore()
    store.save(build_token_map(rows), 3600)
    return store


# ───────────────────────── instruments cache ─────────────────────────


def test_token_map_from_dump() -> None:
    m = warm_store().load()
    assert m is not None
    assert m["EQ:TCS"] == TCS_TOKEN
    assert m["EQ:M&M"] == 519937
    assert m["IDX:NIFTY500"] == 268041
    assert m["IDX:NIFTYBANK"] == 260105
    assert not any("NIFTY24MARFUT" in k for k in m)  # derivatives ignored


def test_cache_layers_and_ttl() -> None:
    downloads: list[int] = []
    clock = [0.0]

    def download() -> list[dict[str, Any]]:
        downloads.append(1)
        return [
            {
                "instrument_token": "1",
                "tradingsymbol": "TCS",
                "segment": "NSE",
                "instrument_type": "EQ",
            }
        ]

    store = MemoryInstrumentStore()
    a = KiteInstruments(download, store, ttl_hours=1, clock=lambda: clock[0])
    assert a.equity_token("TCS") == 1 and len(downloads) == 1
    assert a.equity_token("tcs") == 1 and len(downloads) == 1  # memory hit

    b = KiteInstruments(download, store, ttl_hours=1, clock=lambda: clock[0])
    assert b.equity_token("TCS") == 1 and len(downloads) == 1  # shared-store hit

    clock[0] = 3601.0
    store.data = None  # the Redis TTL would have expired too
    assert a.equity_token("TCS") == 1 and len(downloads) == 2
    assert a.equity_token("NOPE") is None


def test_empty_dump_is_an_error() -> None:
    inst = KiteInstruments(lambda: [], MemoryInstrumentStore(), ttl_hours=1)
    with pytest.raises(ProviderError, match="no NSE equities"):
        inst.equity_token("TCS")


def test_redis_store_roundtrip(redis_client: Redis) -> None:
    key = f"test-kite-instr-{uuid.uuid4().hex}"
    store = RedisInstrumentStore(redis_client, key=key)
    try:
        assert store.load() is None
        store.save({"EQ:TCS": 1, "IDX:NIFTY500": 2}, ttl_s=120)
        assert store.load() == {"EQ:TCS": 1, "IDX:NIFTY500": 2}
        assert 0 < redis_client.ttl(key) <= 120
        store.save({"EQ:INFY": 3}, ttl_s=120)  # replaced wholesale, not merged
        assert store.load() == {"EQ:INFY": 3}
    finally:
        redis_client.delete(key, f"{key}:tmp")


def test_provider_downloads_dump_once(
    http: responses.RequestsMock, historical: HistoricalServer, instruments_route: Any
) -> None:
    store = MemoryInstrumentStore()
    make_provider(store=store).daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    make_provider(store=store).daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    assert instruments_route.call_count == 1
    assert store.saves == 1


# ───────────────────────── daily OHLCV ─────────────────────────


def test_implements_router_protocols() -> None:
    p = make_provider()
    assert isinstance(p, PriceProvider) and isinstance(p, IndexPriceProvider)
    assert p.name is Provider.KITE


def test_daily_ohlcv_parses_candles(historical: HistoricalServer) -> None:
    df = make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 31))

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None
    assert df.index[0] == pd.Timestamp("2024-01-01")
    assert pd.Timestamp("2024-01-26") not in df.index
    assert len(df) == 22
    assert df["volume"].dtype == "int64"

    assert historical.tokens == [TCS_TOKEN]
    assert historical.requests == [
        {
            "from": "2024-01-01 00:00:00",
            "to": "2024-01-31 23:59:59",
            "interval": "day",
            "continuous": "0",
            "oi": "0",
        }
    ]
    assert historical.headers == [f"token {API_KEY}:tok"]


def test_chunks_to_max_range(historical: HistoricalServer) -> None:
    limiter = CountingLimiter()
    p = make_provider(
        limits=ApiLimits(history_max_days=30, quotes_max_symbols=1000),
        limiter=limiter,
        store=warm_store(),
    )
    df = p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 3, 29))
    assert historical.windows == [
        ("2024-01-01 00:00:00", "2024-01-30 23:59:59"),
        ("2024-01-31 00:00:00", "2024-02-29 23:59:59"),
        ("2024-03-01 00:00:00", "2024-03-29 23:59:59"),
    ]
    assert len(df) == 64 and df.index.is_unique and df.index.is_monotonic_increasing
    assert limiter.calls == [Provider.KITE] * 2  # router pays for the first request


def test_instruments_download_is_rate_limited_too(historical: HistoricalServer) -> None:
    limiter = CountingLimiter()
    make_provider(limiter=limiter).daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    assert limiter.calls == [Provider.KITE]  # dump (free, first) + historical (paid)


def test_repo_config_single_request_for_three_years(historical: HistoricalServer) -> None:
    limits = load_config(REPO_CONFIG_DIR).providers.api_limits[Provider.KITE]
    make_provider(limits=limits).daily_ohlcv("TCS", date(2021, 1, 1), date(2024, 3, 29))
    assert len(historical.requests) == 1


def test_index_ohlcv_uses_index_token(historical: HistoricalServer) -> None:
    make_provider().index_ohlcv("NIFTY500", date(2024, 1, 1), date(2024, 1, 5))
    assert historical.tokens == [268041]


def test_unknown_symbol_unavailable_without_historical_call(
    historical: HistoricalServer,
) -> None:
    with pytest.raises(ProviderUnavailable, match="not in the NSE instruments dump"):
        make_provider().daily_ohlcv("NOSUCH", date(2024, 1, 1), date(2024, 1, 5))
    assert historical.requests == []


def test_empty_range_returns_empty_frame(
    http: responses.RequestsMock, instruments_route: Any
) -> None:
    http.add(responses.GET, HISTORICAL_URL, json=fixture("historical_empty.json"))
    df = make_provider().daily_ohlcv("TCS", date(2020, 1, 1), date(2020, 1, 31))
    assert df.empty and list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_start_after_end_rejected() -> None:
    with pytest.raises(ValueError, match="after end"):
        make_provider().daily_ohlcv("TCS", date(2024, 2, 1), date(2024, 1, 1))


# ───────────────────────── entitlement & errors ─────────────────────────


def test_missing_historical_entitlement_is_unavailable_and_remembered(
    historical: HistoricalServer,
) -> None:
    historical.error = (403, "error_permission.json")
    tokens = iter(["tok", "tok", "tok2"])  # one lookup per call
    p = make_provider(token=lambda: next(tokens))

    with pytest.raises(ProviderUnavailable, match="add-on"):
        p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    with pytest.raises(ProviderUnavailable, match="not permitted"):
        p.index_ohlcv("NIFTY500", date(2024, 1, 1), date(2024, 1, 5))
    assert len(historical.requests) == 1  # second call skipped the round-trip

    with pytest.raises(ProviderUnavailable):  # new token (reconnect) → tries again
        p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    assert len(historical.requests) == 2


def test_router_falls_back_when_historical_not_entitled(historical: HistoricalServer) -> None:
    historical.error = (403, "error_permission.json")
    cfg = load_config(REPO_CONFIG_DIR).providers

    @dataclass
    class FakeYf:
        name: Provider = Provider.YFINANCE

        def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
            idx = pd.DatetimeIndex([pd.Timestamp(end)])
            return pd.DataFrame({"open": [1.0], "close": [1.0]}, index=idx)

        def ltp(self, symbols: list[str]) -> dict[str, float]:
            return {}

    router = DataRouter(
        {Provider.KITE: make_provider(), Provider.YFINANCE: FakeYf()},
        cfg,
        limiter=CountingLimiter(),
        gaps=InMemoryGapRecorder(),
        clock=lambda: datetime(2024, 1, 5, 18, 0, tzinfo=IST),
        sleep=lambda _: None,
    )
    res = router.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))

    assert res.source is Provider.YFINANCE
    kite_attempt = next(a for a in res.attempts if a.provider is Provider.KITE)
    assert kite_attempt.outcome is Outcome.UNAVAILABLE and kite_attempt.tries == 1
    assert "PermissionException" in (kite_attempt.detail or "")
    assert Dataset.DAILY_OHLCV in cfg.priority


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (403, "error_token.json", ProviderUnavailable),
        (400, "error_input.json", ProviderUnavailable),
        (429, "error_rate_limit.json", ProviderError),
        (500, "error_general.json", ProviderError),
    ],
)
def test_error_mapping(
    historical: HistoricalServer, status: int, body: str, exc: type[Exception]
) -> None:
    historical.error = (status, body)
    with pytest.raises(exc) as info:
        make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    if exc is ProviderError:
        assert not isinstance(info.value, ProviderUnavailable)


def test_network_failure_is_transient(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderError) as info:  # no routes: ConnectionError from `responses`
        make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 5))
    assert not isinstance(info.value, ProviderUnavailable)
    assert "ConnectionError" in str(info.value)


def test_missing_token_is_unavailable_without_http(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderUnavailable, match="reconnect"):
        make_provider(token=lambda: None).daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 2))
    assert len(http.calls) == 0


# ───────────────────────── LTP ─────────────────────────


def test_ltp_maps_back_and_omits_unknown(http: responses.RequestsMock) -> None:
    http.add(responses.GET, LTP_URL, json=fixture("ltp.json"))
    prices = make_provider().ltp(["TCS", "BAJAJ-AUTO", "NOSUCH"])
    assert prices == {"TCS": 3815.4, "BAJAJ-AUTO": 9050.0}
    q = parse_qs(urlparse(http.calls[0].request.url or "").query)
    assert q["i"] == ["NSE:TCS", "NSE:BAJAJ-AUTO", "NSE:NOSUCH"]


def test_ltp_batches(http: responses.RequestsMock) -> None:
    http.add(responses.GET, LTP_URL, json=fixture("ltp.json"))
    limiter = CountingLimiter()
    p = make_provider(
        limits=ApiLimits(history_max_days=2000, quotes_max_symbols=2), limiter=limiter
    )
    p.ltp(["TCS", "BAJAJ-AUTO", "NOSUCH"])
    sent = [parse_qs(urlparse(c.request.url or "").query)["i"] for c in http.calls]
    assert sent == [["NSE:TCS", "NSE:BAJAJ-AUTO"], ["NSE:NOSUCH"]]
    assert limiter.calls == [Provider.KITE]


# ───────────────────────── OAuth ─────────────────────────


def test_login_url_carries_state() -> None:
    url = urlparse(KiteAuth(API_KEY, "sec", KITE_EXPIRY).login_url("st.at.e"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://kite.zerodha.com/connect/login"
    q = parse_qs(url.query)
    assert q["api_key"] == [API_KEY] and q["v"] == ["3"]
    assert parse_qs(unquote(q["redirect_params"][0])) == {"state": ["st.at.e"]}
    assert "sec" not in url.query


@pytest.mark.parametrize(
    ("now_ist", "expected_ist"),
    [
        (datetime(2024, 3, 29, 9, 10), datetime(2024, 3, 30, 6, 0)),
        (datetime(2024, 3, 30, 3, 0), datetime(2024, 3, 30, 6, 0)),  # after midnight
        (datetime(2024, 3, 30, 6, 0), datetime(2024, 3, 31, 6, 0)),  # exactly at expiry
    ],
)
def test_next_daily_expiry(now_ist: datetime, expected_ist: datetime) -> None:
    got = next_daily_expiry(now_ist.replace(tzinfo=IST), KITE_EXPIRY)
    assert got == expected_ist.replace(tzinfo=IST) and got.tzinfo is UTC


def test_exchange_success(http: responses.RequestsMock) -> None:
    http.add(responses.POST, SESSION_URL, json=fixture("session_ok.json"))
    now = datetime(2024, 3, 29, 9, 10, tzinfo=IST)

    issued = KiteAuth(API_KEY, "sec", KITE_EXPIRY, clock=lambda: now).exchange("req-tok")

    assert issued.access_token == "kite-access-token-abc123"
    assert issued.expires_at == datetime(2024, 3, 30, 0, 30, tzinfo=UTC)  # 06:00 IST
    assert "kite-access-token-abc123" not in repr(issued)
    body = parse_qs(http.calls[0].request.body or "")
    assert body == {
        "api_key": [API_KEY],
        "request_token": ["req-tok"],
        "checksum": [hashlib.sha256(f"{API_KEY}req-toksec".encode()).hexdigest()],
    }


def test_exchange_rejected(http: responses.RequestsMock) -> None:
    http.add(responses.POST, SESSION_URL, json=fixture("session_bad_token.json"), status=403)
    with pytest.raises(ProviderUnavailable, match="TokenException"):
        KiteAuth(API_KEY, "sec", KITE_EXPIRY).exchange("stale")


def test_exchange_network_error(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderError, match="ConnectionError"):
        KiteAuth(API_KEY, "sec", KITE_EXPIRY).exchange("req-tok")


def test_build_provider_needs_api_key() -> None:
    cfg = load_config(REPO_CONFIG_DIR).providers
    store = MemoryInstrumentStore()
    assert build_kite_provider(None, lambda: "t", cfg, store, None) is None
    assert isinstance(build_kite_provider(API_KEY, lambda: "t", cfg, store, None), KiteProvider)
