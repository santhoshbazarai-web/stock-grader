"""Fyers provider tests. HTTP is served from tests/fixtures/fyers via ``responses`` through the
real fyers-apiv3 SDK; any unmatched request raises, so nothing reaches the network."""

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import responses
from requests import PreparedRequest

from app.core.config import ApiLimits, Provider, load_config
from app.data.providers.base import (
    IndexPriceProvider,
    PriceProvider,
    ProviderError,
    ProviderUnavailable,
)
from app.data.providers.fyers import (
    FyersAuth,
    FyersProvider,
    build_fyers_provider,
    from_fyers_symbol,
    jwt_expiry,
    to_fyers_index,
    to_fyers_symbol,
)
from tests.conftest import REPO_CONFIG_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "fyers"
HISTORY_URL = "https://api-t1.fyers.in/data/history"
QUOTES_URL = "https://api-t1.fyers.in/data/quotes"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
APP_ID = "ABCD1234-100"
REDIRECT = "http://localhost:8000/api/brokers/fyers/callback"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@dataclass
class HistoryServer:
    """Serves history_tcs_daily.json sliced to the requested range; records every request."""

    requests: list[dict[str, str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    overlap_days: int = 0  # widen each response to simulate overlapping chunks
    no_data_before: date | None = None

    def __call__(self, request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(request.url or "").query).items()}
        self.requests.append(q)
        self.headers.append(str(request.headers.get("Authorization")))
        frm = pd.Timestamp(q["range_from"]) - pd.Timedelta(days=self.overlap_days)
        to = pd.Timestamp(q["range_to"]) + pd.Timedelta(days=self.overlap_days)
        if self.no_data_before and pd.Timestamp(q["range_to"]) < pd.Timestamp(self.no_data_before):
            return 200, {}, json.dumps(fixture("history_no_data.json"))
        body = fixture("history_tcs_daily.json")
        body["candles"] = [
            c
            for c in body["candles"]
            if frm <= pd.Timestamp(c[0], unit="s", tz="Asia/Kolkata").tz_localize(None) <= to
        ]
        return 200, {}, json.dumps(body)

    @property
    def windows(self) -> list[tuple[str, str]]:
        return [(r["range_from"], r["range_to"]) for r in self.requests]


@pytest.fixture
def history(http: responses.RequestsMock) -> HistoryServer:
    server = HistoryServer()
    http.add_callback(responses.GET, HISTORY_URL, callback=server)
    return server


@dataclass
class CountingLimiter:
    calls: list[Provider] = field(default_factory=list)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        self.calls.append(provider)


LIMITS = ApiLimits(history_max_days=366, quotes_max_symbols=50)


def make_provider(
    token: Callable[[], str | None] = lambda: "tok",
    limits: ApiLimits = LIMITS,
    limiter: CountingLimiter | None = None,
) -> FyersProvider:
    return FyersProvider(APP_ID, token, limits, limiter=limiter)


# ───────────────────────── symbology ─────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("TCS", "NSE:TCS-EQ"),
        ("bajaj-auto", "NSE:BAJAJ-AUTO-EQ"),
        ("M&M", "NSE:M&M-EQ"),
        ("NSE:SBIN-BE", "NSE:SBIN-BE"),
    ],
)
def test_to_fyers_symbol(symbol: str, expected: str) -> None:
    assert to_fyers_symbol(symbol) == expected


def test_index_and_reverse_mapping() -> None:
    assert to_fyers_index("NIFTY500") == "NSE:NIFTY500-INDEX"
    assert from_fyers_symbol("NSE:BAJAJ-AUTO-EQ") == "BAJAJ-AUTO"
    assert from_fyers_symbol("NSE:NIFTY500-INDEX") == "NIFTY500"


def test_implements_router_protocols() -> None:
    p = make_provider()
    assert isinstance(p, PriceProvider) and isinstance(p, IndexPriceProvider)
    assert p.name is Provider.FYERS


# ───────────────────────── daily OHLCV ─────────────────────────


def test_daily_ohlcv_parses_candles(history: HistoryServer) -> None:
    df = make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 31))

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None
    assert df.index[0] == pd.Timestamp("2024-01-01")  # 00:00 IST epoch → same IST date
    assert pd.Timestamp("2024-01-26") not in df.index  # holiday
    assert pd.Timestamp("2024-01-06") not in df.index  # Saturday
    assert len(df) == 22
    assert df["volume"].dtype == "int64"
    assert df.iloc[0]["open"] == 3700.0

    [req] = history.requests
    assert req == {
        "symbol": "NSE:TCS-EQ",
        "resolution": "D",
        "date_format": "1",
        "range_from": "2024-01-01",
        "range_to": "2024-01-31",
        "cont_flag": "1",
    }
    assert history.headers == [f"{APP_ID}:tok"]


def test_chunks_to_max_range(history: HistoryServer) -> None:
    limiter = CountingLimiter()
    p = make_provider(limits=ApiLimits(history_max_days=30, quotes_max_symbols=50), limiter=limiter)

    df = p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 3, 29))

    assert history.windows == [
        ("2024-01-01", "2024-01-30"),
        ("2024-01-31", "2024-02-29"),
        ("2024-03-01", "2024-03-29"),
    ]
    assert len(df) == 64  # every candle in the fixture, once
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert limiter.calls == [Provider.FYERS] * 2  # the router pays for the first request


def test_repo_config_chunking(history: HistoryServer) -> None:
    limits = load_config(REPO_CONFIG_DIR).providers.api_limits[Provider.FYERS]
    make_provider(limits=limits).daily_ohlcv("TCS", date(2023, 1, 1), date(2024, 3, 29))
    assert history.windows == [("2023-01-01", "2024-01-01"), ("2024-01-02", "2024-03-29")]


def test_overlapping_chunks_deduplicated(history: HistoryServer) -> None:
    history.overlap_days = 3
    p = make_provider(limits=ApiLimits(history_max_days=10, quotes_max_symbols=50))
    df = p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 2, 15))
    assert df.index.is_unique
    assert df.index.min() >= pd.Timestamp("2024-01-01")
    assert df.index.max() <= pd.Timestamp("2024-02-15")


def test_no_data_returns_empty_frame(http: responses.RequestsMock) -> None:
    http.add(responses.GET, HISTORY_URL, json=fixture("history_no_data.json"))
    df = make_provider().daily_ohlcv("NEWIPO", date(2024, 1, 1), date(2024, 1, 31))
    assert df.empty and list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_no_data_in_early_chunks_is_skipped(history: HistoryServer) -> None:
    history.no_data_before = date(2024, 1, 1)
    p = make_provider(limits=ApiLimits(history_max_days=100, quotes_max_symbols=50))
    df = p.daily_ohlcv("TCS", date(2023, 6, 1), date(2024, 1, 31))
    assert len(history.requests) == 3
    assert df.index.min() == pd.Timestamp("2024-01-01")


def test_index_ohlcv_uses_index_symbol(history: HistoryServer) -> None:
    make_provider().index_ohlcv("NIFTY500", date(2024, 1, 1), date(2024, 1, 10))
    assert history.requests[0]["symbol"] == "NSE:NIFTY500-INDEX"


def test_start_after_end_rejected() -> None:
    with pytest.raises(ValueError, match="after end"):
        make_provider().daily_ohlcv("TCS", date(2024, 2, 1), date(2024, 1, 1))


# ───────────────────────── errors ─────────────────────────


@pytest.mark.parametrize(
    ("status", "body", "exc", "match"),
    [
        (401, "error_token_expired.json", ProviderUnavailable, "token rejected"),
        (400, "error_invalid_symbol.json", ProviderUnavailable, "invalid symbol"),
        (503, "error_server.json", ProviderError, "503"),
    ],
)
def test_error_mapping(
    http: responses.RequestsMock, status: int, body: str, exc: type[Exception], match: str
) -> None:
    http.add(responses.GET, HISTORY_URL, json=fixture(body), status=status)
    with pytest.raises(exc, match=match) as info:
        make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 31))
    if exc is ProviderError:
        assert not isinstance(info.value, ProviderUnavailable)  # transient → router retries


def test_network_failure_is_transient(http: responses.RequestsMock) -> None:
    # No route registered: `responses` raises ConnectionError, which the SDK swallows into
    # {"s": "error", "code": -99}. It must surface as a retryable ProviderError.
    with pytest.raises(ProviderError) as info:
        make_provider().daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 31))
    assert not isinstance(info.value, ProviderUnavailable)


def test_missing_token_is_unavailable_without_http(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderUnavailable, match="reconnect"):
        make_provider(token=lambda: None).daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 2))
    assert len(http.calls) == 0


def test_new_token_picked_up_without_restart(history: HistoryServer) -> None:
    tokens = iter(["old", "old", "new"])
    p = make_provider(token=lambda: next(tokens))
    for _ in range(3):
        p.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 1, 2))
    assert history.headers == [f"{APP_ID}:old", f"{APP_ID}:old", f"{APP_ID}:new"]


# ───────────────────────── LTP ─────────────────────────


def test_ltp_maps_back_and_skips_rejected(http: responses.RequestsMock) -> None:
    http.add(responses.GET, QUOTES_URL, json=fixture("quotes.json"))
    prices = make_provider().ltp(["TCS", "BAJAJ-AUTO", "NOSUCH"])
    assert prices == {"TCS": 3815.4, "BAJAJ-AUTO": 9050.0}
    [call] = http.calls
    q = parse_qs(urlparse(call.request.url or "").query)
    assert q["symbols"] == ["NSE:TCS-EQ,NSE:BAJAJ-AUTO-EQ,NSE:NOSUCH-EQ"]


def test_ltp_batches_by_quote_limit(http: responses.RequestsMock) -> None:
    http.add(responses.GET, QUOTES_URL, json=fixture("quotes.json"))
    limiter = CountingLimiter()
    p = make_provider(limits=ApiLimits(history_max_days=366, quotes_max_symbols=2), limiter=limiter)
    p.ltp(["TCS", "BAJAJ-AUTO", "NOSUCH"])
    sent = [parse_qs(urlparse(c.request.url or "").query)["symbols"][0] for c in http.calls]
    assert sent == ["NSE:TCS-EQ,NSE:BAJAJ-AUTO-EQ", "NSE:NOSUCH-EQ"]
    assert limiter.calls == [Provider.FYERS]


def test_ltp_auth_error(http: responses.RequestsMock) -> None:
    http.add(responses.GET, QUOTES_URL, json=fixture("error_token_expired.json"), status=401)
    with pytest.raises(ProviderUnavailable):
        make_provider().ltp(["TCS"])


# ───────────────────────── OAuth client ─────────────────────────


def test_login_url() -> None:
    url = urlparse(FyersAuth(APP_ID, "s3cret", REDIRECT).login_url("st4te"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == (
        "https://api-t1.fyers.in/api/v3/generate-authcode"
    )
    assert parse_qs(url.query) == {
        "client_id": [APP_ID],
        "redirect_uri": [REDIRECT],
        "response_type": ["code"],
        "state": ["st4te"],
    }
    assert "s3cret" not in url.query


def test_exchange_success(http: responses.RequestsMock) -> None:
    http.add(responses.POST, TOKEN_URL, json=fixture("token_ok.json"))

    issued = FyersAuth(APP_ID, "s3cret", REDIRECT).exchange("the-auth-code")

    assert issued.access_token == fixture("token_ok.json")["access_token"]
    assert issued.expires_at == datetime(2024, 3, 30, 0, 30, tzinfo=UTC)
    assert issued.access_token not in repr(issued)
    sent = json.loads(http.calls[0].request.body or b"{}")
    assert sent == {
        "grant_type": "authorization_code",
        "appIdHash": hashlib.sha256(f"{APP_ID}:s3cret".encode()).hexdigest(),
        "code": "the-auth-code",
    }


def test_exchange_rejected(http: responses.RequestsMock) -> None:
    http.add(responses.POST, TOKEN_URL, json=fixture("token_invalid_code.json"), status=400)
    with pytest.raises(ProviderUnavailable, match="Invalid auth code"):
        FyersAuth(APP_ID, "s3cret", REDIRECT).exchange("bad")


def test_exchange_network_error(http: responses.RequestsMock) -> None:
    with pytest.raises(ProviderError, match="ConnectionError"):
        FyersAuth(APP_ID, "s3cret", REDIRECT).exchange("code")


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", "a.eyJmb28iOjF9.c"])
def test_jwt_expiry_unreadable(token: str) -> None:
    with pytest.raises(ProviderError, match="expiry"):
        jwt_expiry(token)


# ───────────────────────── factory ─────────────────────────


def test_build_provider_needs_app_id() -> None:
    cfg = load_config(REPO_CONFIG_DIR).providers
    assert build_fyers_provider(None, lambda: "t", cfg, None) is None
    assert isinstance(build_fyers_provider(APP_ID, lambda: "t", cfg, None), FyersProvider)
