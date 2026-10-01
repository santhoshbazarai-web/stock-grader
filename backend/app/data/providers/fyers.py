"""Fyers API v3 (``fyers-apiv3``): OAuth, daily OHLCV, index OHLCV and LTP. Read-only.

Symbology: equities ``NSE:<SYMBOL>-<SERIES>`` (``NSE:TCS-EQ``), indices ``NSE:<CODE>-INDEX``
(``NSE:NIFTY500-INDEX``).

The SDK never raises on data calls: HTTP and network failures come back as
``{"s": "error", "code": ..., "message": ...}``. :func:`_check` maps those onto the router's
errors — auth failures and unknown symbols → :class:`ProviderUnavailable` (fall through, no
retry); anything else → :class:`ProviderError` (retried with backoff by the router).
"""

import base64
import binascii
import json
import logging
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

from app.core.config import ApiLimits, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.providers.base import IssuedToken, ProviderError, ProviderUnavailable
from app.data.raw_store import RawStore, RawStoreError
from app.data.symbol_master import FyersSymbol, parse_fyers_master

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# Fyers API error codes (https://myapi.fyers.in/docsv3 → "Error codes"). API facts, not tunables.
AUTH_ERROR_CODES = frozenset({-8, -15, -16, -17})  # token expired / invalid / unauthenticated
INVALID_SYMBOL_CODES = frozenset({-300})


# ───────────────────────── symbology ─────────────────────────


def to_fyers_symbol(symbol: str, series: str = "EQ") -> str:
    """``TCS`` → ``NSE:TCS-EQ``. Already-qualified symbols pass through."""
    if ":" in symbol:
        return symbol
    return f"NSE:{symbol.strip().upper()}-{series}"


def to_fyers_index(index: str) -> str:
    """``NIFTY500`` → ``NSE:NIFTY500-INDEX``."""
    if ":" in index:
        return index
    return f"NSE:{index.strip().upper()}-INDEX"


def from_fyers_symbol(fyers_symbol: str) -> str:
    """``NSE:BAJAJ-AUTO-EQ`` → ``BAJAJ-AUTO``; ``NSE:NIFTY500-INDEX`` → ``NIFTY500``."""
    body = fyers_symbol.split(":", 1)[-1]
    return body.rsplit("-", 1)[0]


# ───────────────────────── OAuth ─────────────────────────


def jwt_expiry(token: str) -> datetime:
    """``exp`` claim of a JWT access token (payload only; signature is Fyers' concern)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    except (IndexError, KeyError, ValueError, TypeError, binascii.Error) as exc:
        raise ProviderError("Fyers access token has no readable expiry") from exc


class FyersAuth:
    """Authorization-code flow (SPEC §3.3)."""

    def __init__(self, app_id: str, secret: str, redirect_uri: str) -> None:
        self._app_id = app_id
        self._secret = secret
        self._redirect_uri = redirect_uri

    def _session(self, state: str | None = None) -> fyersModel.SessionModel:
        return fyersModel.SessionModel(
            client_id=self._app_id,
            secret_key=self._secret,
            redirect_uri=self._redirect_uri,
            response_type="code",
            grant_type="authorization_code",
            state=state,
        )

    def login_url(self, state: str) -> str:
        url: str = self._session(state).generate_authcode()
        return url

    def exchange(self, auth_code: str) -> IssuedToken:
        session = self._session()
        session.set_token(auth_code)
        try:
            resp = session.generate_token()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Fyers token exchange failed: {type(exc).__name__}") from exc
        if not isinstance(resp, dict) or resp.get("s") != "ok" or not resp.get("access_token"):
            code = resp.get("code") if isinstance(resp, dict) else None
            message = resp.get("message") if isinstance(resp, dict) else None
            raise ProviderUnavailable(f"Fyers rejected the auth code ({code}: {message})")
        token = str(resp["access_token"])
        return IssuedToken(token, jwt_expiry(token))


# ───────────────────────── market data ─────────────────────────


class FyersClient(Protocol):
    """The subset of ``fyersModel.FyersModel`` used here."""

    def history(self, data: dict[str, Any]) -> Any: ...

    def quotes(self, data: dict[str, Any]) -> Any: ...


def default_client_factory(app_id: str, token: str) -> FyersClient:
    # The SDK writes fyersApi.log / fyersRequests.log into log_path; keep them out of the CWD.
    client: FyersClient = fyersModel.FyersModel(
        client_id=app_id, token=token, is_async=False, log_path=tempfile.gettempdir()
    )
    return client


def _check(resp: Any, what: str) -> dict[str, Any]:
    if not isinstance(resp, dict):
        raise ProviderError(f"Fyers {what}: unexpected response type {type(resp).__name__}")
    status = resp.get("s")
    if status in ("ok", "no_data"):
        return resp
    code, message = resp.get("code"), resp.get("message")
    if code in AUTH_ERROR_CODES:
        raise ProviderUnavailable(f"Fyers {what}: token rejected ({code}: {message})")
    if code in INVALID_SYMBOL_CODES:
        raise ProviderUnavailable(f"Fyers {what}: invalid symbol ({code}: {message})")
    raise ProviderError(f"Fyers {what}: error ({code}: {message})")


def _date_windows(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    """Inclusive windows of at most ``max_days`` days covering ``start..end``."""
    cur = start
    while cur <= end:
        to = min(end, cur + timedelta(days=max_days - 1))
        yield cur, to
        cur = to + timedelta(days=1)


def candles_to_frame(candles: list[list[float]]) -> pd.DataFrame:
    """Fyers ``[[epoch, o, h, l, c, v], ...]`` → date-indexed OHLCV (dates in IST)."""
    if not candles:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))
    df = pd.DataFrame(candles, columns=["ts", *OHLCV_COLUMNS])
    ist = pd.to_datetime(df.pop("ts"), unit="s", utc=True).dt.tz_convert(IST)
    df.index = pd.DatetimeIndex(ist.dt.tz_localize(None).dt.normalize(), name="date")
    df["volume"] = df["volume"].astype("int64")
    return df


class FyersProvider:
    """Implements ``PriceProvider`` and ``IndexPriceProvider``.

    ``token_source`` is called per request so a token reconnected during the day is picked up
    without a restart; no valid token → :class:`ProviderUnavailable` (router falls through).

    Rate limiting: the router takes one token per logical call; this class takes one for every
    *additional* HTTP request (history chunks, quote batches), so each request is covered.
    """

    name = Provider.FYERS

    def __init__(
        self,
        app_id: str,
        token_source: Callable[[], str | None],
        limits: ApiLimits,
        *,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
        client_factory: Callable[[str, str], FyersClient] = default_client_factory,
        masters: list[str] | None = None,
        masters_timeout_s: float = 30.0,
        raw_store: RawStore | None = None,
    ) -> None:
        self._masters = masters or []
        self._masters_timeout_s = masters_timeout_s
        self._raw = raw_store
        self._app_id = app_id
        self._token_source = token_source
        self._limits = limits
        self._limiter = limiter
        self._rate_limit_timeout_s = rate_limit_timeout_s
        self._client_factory = client_factory
        self._client: FyersClient | None = None
        self._client_token: str | None = None

    def _get_client(self) -> FyersClient:
        token = self._token_source()
        if not token:
            raise ProviderUnavailable("no valid Fyers token — reconnect Fyers in Settings")
        if self._client is None or token != self._client_token:
            self._client = self._client_factory(self._app_id, token)
            self._client_token = token
        return self._client

    def _throttle(self, request_no: int) -> None:
        if request_no > 0 and self._limiter is not None:
            self._limiter.acquire(self.name, timeout=self._rate_limit_timeout_s)

    # ───────────── OHLCV ─────────────

    def _history(self, fyers_symbol: str, start: date, end: date) -> pd.DataFrame:
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        client = self._get_client()
        frames: list[pd.DataFrame] = []
        windows = _date_windows(start, end, self._limits.history_max_days)
        for i, (frm, to) in enumerate(windows):
            self._throttle(i)
            resp = _check(
                client.history(
                    {
                        "symbol": fyers_symbol,
                        "resolution": "D",
                        "date_format": "1",
                        "range_from": frm.isoformat(),
                        "range_to": to.isoformat(),
                        "cont_flag": "1",
                    }
                ),
                f"history {fyers_symbol}",
            )
            frames.append(candles_to_frame(resp.get("candles") or []))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return candles_to_frame([])
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.loc[pd.Timestamp(start) : pd.Timestamp(end)]

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._history(to_fyers_symbol(symbol), start, end)

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        return self._history(to_fyers_index(index), start, end)

    # ───────────── LTP ─────────────

    def symbol_master(self) -> list[FyersSymbol]:
        """The public symbol masters (FyersSymbolMasterProvider); no token needed."""
        if not self._masters:
            raise ProviderUnavailable("no Fyers symbol masters configured")
        return fetch_fyers_masters(self._masters, timeout_s=self._masters_timeout_s,
                                   raw_store=self._raw, throttle=self._throttle)  # fmt: skip

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price keyed by the caller's symbols. Symbols Fyers rejects are omitted."""
        client = self._get_client()
        by_fyers = {to_fyers_symbol(s): s for s in symbols}
        out: dict[str, float] = {}
        batches = batched(by_fyers, self._limits.quotes_max_symbols)
        for i, batch in enumerate(batches):
            self._throttle(i)
            resp = _check(client.quotes({"symbols": ",".join(batch)}), "quotes")
            for item in resp.get("d") or []:
                values = item.get("v") or {}
                lp = values.get("lp")
                if item.get("s") != "ok" or lp is None:
                    logger.warning("Fyers quote for %s skipped: %s", item.get("n"), values)
                    continue
                key = by_fyers.get(item.get("n", ""), from_fyers_symbol(item.get("n", "")))
                out[key] = float(lp)
        return out


def fetch_fyers_masters(
    urls: list[str],
    *,
    timeout_s: float,
    raw_store: RawStore | None = None,
    throttle: Callable[[int], None] = lambda _: None,
    get: Callable[..., requests.Response] = requests.get,
) -> list[FyersSymbol]:
    """Fyers' public symbol-master CSVs (no login), cached raw before parsing (§3.2a)."""
    out: list[FyersSymbol] = []
    for i, url in enumerate(urls):
        throttle(i)
        try:
            resp = get(url, timeout=timeout_s)
        except requests.RequestException as exc:
            raise ProviderError(f"Fyers {url}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Fyers {url}: HTTP {resp.status_code}")
        if raw_store is not None:
            try:
                raw_store.save("fyers", url.rsplit("/", 1)[-1], resp.content)
            except RawStoreError as exc:
                raise ProviderUnavailable(str(exc)) from exc
        out += parse_fyers_master(resp.content.decode("utf-8", errors="replace"))
    return out


def build_fyers_provider(
    app_id: str | None,
    token_source: Callable[[], str | None],
    config: ProvidersConfig,
    limiter: Limiter | None,
    raw_store: RawStore | None = None,
) -> FyersProvider | None:
    """``None`` when FYERS_APP_ID is not set (router then reports fyers as not configured)."""
    if not app_id:
        return None
    return FyersProvider(
        app_id,
        token_source,
        config.api_limits[Provider.FYERS],
        limiter=limiter,
        rate_limit_timeout_s=config.retry.rate_limit_timeout_s,
        masters=config.symbols.fyers_masters,
        masters_timeout_s=config.symbols.request_timeout_s,
        raw_store=raw_store,
    )
