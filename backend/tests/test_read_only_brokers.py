"""Broker safety (AGENTS.md rule 7, SPEC §3.3): the app is read-only. No order, GTT, holdings,
positions or funds endpoint is ever called.

Three layers:
1. **SDK allowlist:** every public method of ``fyersModel.FyersModel`` and ``KiteConnect``
   outside the market-data / login allowlist is replaced by one that fails the test. Every
   broker code path the app has (login URL, token exchange, daily and index history, LTP,
   the Kite instruments dump) then runs through the real SDKs.
2. **HTTP:** every request those SDKs send is recorded (``responses``). Each path must be an
   allowlisted market-data / session path, and none may look like an order / portfolio /
   funds path.
3. **Source scan:** no module in ``app/`` names a forbidden SDK method or a forbidden REST
   path, so a future change cannot add one without failing here.
"""

import ast
import json
import re
from collections.abc import Iterator
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import responses
from fyers_apiv3 import fyersModel
from kiteconnect import KiteConnect
from requests import PreparedRequest

from app.core.config import ApiLimits
from app.data.providers.fyers import FyersAuth, FyersProvider
from app.data.providers.kite import KiteAuth, KiteProvider
from app.data.providers.kite_instruments import MemoryInstrumentStore

APP = Path(__file__).resolve().parents[1] / "app"
FIX = Path(__file__).parent / "fixtures"

# market data + login only
FYERS_ALLOWED = {"history", "quotes"}
KITE_ALLOWED = {"historical_data", "ltp", "instruments", "login_url", "generate_session",
                "set_access_token"}  # fmt: skip
ALLOWED_PATHS = re.compile(
    r"^(/data/history|/data/quotes|/api/v3/validate-authcode"  # Fyers
    r"|/instruments/NSE|/instruments/historical/\d+/day|/quote/ltp|/session/token)$"  # Kite
)
FORBIDDEN_PATH = re.compile(
    r"order|gtt|holding|position|fund|margin|trade|portfolio|basket|exit|convert|mf/",
    re.IGNORECASE,
)
FORBIDDEN_WORDS = ("order", "gtt", "holding", "position", "fund", "margin", "trade", "exit",
                   "convert", "basket", "alert", "smart", "ledger", "pnl", "mf_", "sip",
                   "logout", "invalidate", "charges", "trigger")  # fmt: skip


def _forbidden_methods(cls: type, allowed: set[str]) -> list[str]:
    return [n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))
            and n not in allowed and not n.isupper()]  # fmt: skip


@pytest.fixture
def sdk_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every non-allowlisted SDK method with a tripwire; returns the tripped names."""
    tripped: list[str] = []
    for cls, allowed in ((fyersModel.FyersModel, FYERS_ALLOWED), (KiteConnect, KITE_ALLOWED)):
        for name in _forbidden_methods(cls, allowed):

            def trip(*_a: Any, _n: str = f"{cls.__name__}.{name}", **_k: Any) -> Any:
                tripped.append(_n)
                raise AssertionError(f"read-only app called {_n}")

            monkeypatch.setattr(cls, name, trip)
    return tripped


@pytest.fixture
def http() -> Iterator[list[tuple[str, str]]]:
    """Every request to the broker hosts, answered from the recorded fixtures."""
    seen: list[tuple[str, str]] = []
    fyers, kite = FIX / "fyers", FIX / "kite"

    def serve(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        path = re.sub(r"^https://[^/]+", "", (request.url or "").split("?")[0])
        seen.append((request.method or "", path))
        body: str
        if path == "/data/history":
            body = (fyers / "history_tcs_daily.json").read_text()
        elif path == "/data/quotes":
            body = (fyers / "quotes.json").read_text()
        elif path == "/api/v3/validate-authcode":
            body = (fyers / "token_ok.json").read_text()
        elif path == "/instruments/NSE":
            return 200, {"Content-Type": "text/csv"}, (kite / "instruments_nse.csv").read_text()
        elif path.startswith("/instruments/historical/"):
            body = (kite / "historical_tcs_day.json").read_text()
        elif path == "/quote/ltp":
            body = (kite / "ltp.json").read_text()
        elif path == "/session/token":
            body = (kite / "session_ok.json").read_text()
        else:
            return 404, {}, json.dumps({"s": "error", "message": f"unexpected path {path}"})
        return 200, {"Content-Type": "application/json"}, body

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        for host in ("https://api-t1.fyers.in", "https://api.kite.trade"):
            for method in (responses.GET, responses.POST, responses.PUT, responses.DELETE):
                mock.add_callback(method, re.compile(re.escape(host) + r"/.*"), callback=serve)
        yield seen


def test_every_broker_code_path_is_read_only(sdk_guard: list[str],
                                             http: list[tuple[str, str]]) -> None:  # fmt: skip
    # Fyers: login URL, token exchange, daily + index history, LTP (real SDK client)
    auth = FyersAuth("ABCD1234-100", "s3cret", "http://127.0.0.1:8000/api/brokers/fyers/callback")
    assert auth.login_url("state-1").startswith("https://api-t1.fyers.in/")
    token = auth.exchange("auth-code").access_token
    fyers = FyersProvider("ABCD1234-100", lambda: token,
                          ApiLimits(history_max_days=366, quotes_max_symbols=50))  # fmt: skip
    assert not fyers.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 3, 31)).empty
    fyers.index_ohlcv("NIFTY50", date(2024, 1, 1), date(2024, 3, 31))
    fyers.ltp(["TCS", "INFY"])

    # Kite (disabled by default in config, but code complete): login, session, instruments,
    # daily history, LTP
    kauth = KiteAuth("kitekey", "kitesecret", time(6, 0))
    assert kauth.login_url("state-2").startswith("https://kite.zerodha.com/connect/login")
    ktoken = kauth.exchange("request-token").access_token
    kite = KiteProvider("kitekey", lambda: ktoken,
                        ApiLimits(history_max_days=2000, quotes_max_symbols=1000),
                        MemoryInstrumentStore(), 20)  # fmt: skip
    kite.daily_ohlcv("TCS", date(2024, 1, 1), date(2024, 3, 31))
    kite.ltp(["TCS"])

    assert sdk_guard == []
    paths = {p for _, p in http}
    assert paths, "no broker requests were made"
    assert all(ALLOWED_PATHS.match(p) for p in paths), sorted(paths)
    assert not [p for p in paths if FORBIDDEN_PATH.search(p) and "historical" not in p]
    # the only writes are the two login exchanges
    assert {p for m, p in http if m != "GET"} <= {"/api/v3/validate-authcode", "/session/token"}


def test_tripwires_cover_the_dangerous_sdk_methods() -> None:
    fyers = _forbidden_methods(fyersModel.FyersModel, FYERS_ALLOWED)
    kite = _forbidden_methods(KiteConnect, KITE_ALLOWED)
    for name in (
        "place_order",
        "modify_order",
        "cancel_order",
        "place_gtt_order",
        "holdings",
        "positions",
        "funds",
        "exit_positions",
        "convert_position",
        "orderbook",
    ):
        assert name in fyers
    for name in (
        "place_order",
        "modify_order",
        "cancel_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        "get_gtts",
        "holdings",
        "positions",
        "margins",
        "orders",
        "trades",
    ):
        assert name in kite


def _python_sources() -> Iterator[tuple[Path, ast.AST]]:
    for path in APP.rglob("*.py"):
        yield path, ast.parse(path.read_text(), filename=str(path))


BROKER_CODE = (
    "data/providers/fyers.py",
    "data/providers/kite.py",
    "data/providers/kite_instruments.py",
    "api/brokers.py",
    "data/broker_tokens.py",
)
REST_PATH = re.compile(r"/(orders?|portfolio|gtt|funds|holdings|positions|margins|trades)\b")


def test_source_never_names_a_trading_endpoint() -> None:
    """Multi-word SDK method names (``place_order``, ``get_gtts`` ...) may appear nowhere in
    ``app/``; single words that are also plain English (``holdings``, ``trades``) nowhere in the
    broker code; and no string anywhere may look like a trading REST path."""
    sdk_methods = set(_forbidden_methods(fyersModel.FyersModel, FYERS_ALLOWED)) | set(
        _forbidden_methods(KiteConnect, KITE_ALLOWED))  # fmt: skip
    dangerous = {m for m in sdk_methods if any(w in m for w in FORBIDDEN_WORDS)}
    specific = {m for m in dangerous if "_" in m}
    hits = []
    for path, tree in _python_sources():
        rel = path.relative_to(APP).as_posix()
        names = dangerous if rel in BROKER_CODE else specific
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in names:
                hits.append(f"{rel}:{node.lineno} .{node.attr}")
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and REST_PATH.search(node.value)):  # fmt: skip
                hits.append(f"{rel}:{node.lineno} {node.value!r}")
    assert hits == []
    assert {"place_order", "place_gtt", "exit_positions"} <= specific


def test_tripwire_fires(sdk_guard: list[str], tmp_path: Path) -> None:
    client = fyersModel.FyersModel(client_id="x", token="t", is_async=False,
                                   log_path=str(tmp_path))  # fmt: skip
    with pytest.raises(AssertionError, match=r"read-only app called FyersModel\.place_order"):
        client.place_order({"symbol": "NSE:TCS-EQ"})
    with pytest.raises(AssertionError, match=r"KiteConnect\.holdings"):
        KiteConnect(api_key="k").holdings()
    assert sdk_guard == ["FyersModel.place_order", "KiteConnect.holdings"]
