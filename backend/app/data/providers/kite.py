"""Kite Connect (``kiteconnect``): OAuth, daily OHLCV, index OHLCV and LTP. Read-only.

Historical candles need the paid "Historical data" add-on. Without it Kite answers
``PermissionException``; that becomes :class:`ProviderUnavailable` so the router falls back, and
the denial is remembered for the current token so later calls skip the HTTP round-trip.

Exception mapping (the SDK raises typed exceptions; ``requests`` errors propagate raw):
- ``TokenException``, ``PermissionException``, ``InputException`` → ``ProviderUnavailable``
- ``NetworkException`` (incl. 429), ``DataException``, ``GeneralException``, ``requests``
  errors → ``ProviderError`` (retried by the router)

Never construct ``KiteConnect(debug=True)``: its debug log prints the Authorization header.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from itertools import batched
from typing import Any, NoReturn, Protocol
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import kiteconnect.exceptions as kex
import pandas as pd
import requests
from kiteconnect import KiteConnect

from app.core.config import ApiLimits, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter
from app.data.providers.base import IssuedToken, ProviderError, ProviderUnavailable
from app.data.providers.kite_instruments import InstrumentStore, KiteInstruments

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
EXCHANGE = "NSE"
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def _raise_mapped(exc: Exception, what: str) -> NoReturn:
    detail = f"Kite {what}: {type(exc).__name__}: {exc}"
    if isinstance(exc, kex.PermissionException):
        raise ProviderUnavailable(f"{detail} (API add-on not subscribed?)") from exc
    if isinstance(exc, kex.TokenException | kex.InputException):
        raise ProviderUnavailable(detail) from exc
    if isinstance(exc, kex.KiteException | requests.RequestException):
        raise ProviderError(detail) from exc
    raise exc


def next_daily_expiry(now: datetime, at_ist: time) -> datetime:
    """The next ``at_ist`` (IST wall clock) strictly after ``now``, in UTC."""
    local = now.astimezone(IST)
    candidate = datetime.combine(local.date(), at_ist, tzinfo=IST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


# ───────────────────────── OAuth ─────────────────────────


class KiteAuth:
    """Request-token flow (SPEC §3.3). The redirect URL itself is set in the Kite developer
    console; ``state`` travels via Kite's ``redirect_params`` and comes back on the callback."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        token_expiry_ist: time,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._token_expiry_ist = token_expiry_ist
        self._clock = clock

    def login_url(self, state: str) -> str:
        base: str = KiteConnect(api_key=self._api_key).login_url()
        return f"{base}&redirect_params={quote(urlencode({'state': state}))}"

    def exchange(self, request_token: str) -> IssuedToken:
        kite = KiteConnect(api_key=self._api_key)
        try:
            resp = kite.generate_session(request_token, api_secret=self._api_secret)
        except Exception as exc:
            _raise_mapped(exc, "session exchange")
        token = resp.get("access_token") if isinstance(resp, dict) else None
        if not token:
            raise ProviderUnavailable("Kite session response had no access_token")
        return IssuedToken(str(token), next_daily_expiry(self._clock(), self._token_expiry_ist))


# ───────────────────────── market data ─────────────────────────


class KiteClient(Protocol):
    """The subset of ``KiteConnect`` used here."""

    def historical_data(
        self, instrument_token: int, from_date: str, to_date: str, interval: str
    ) -> list[dict[str, Any]]: ...

    def ltp(self, *instruments: Any) -> dict[str, Any]: ...

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]: ...


def default_client_factory(api_key: str, token: str) -> KiteClient:
    client: KiteClient = KiteConnect(api_key=api_key, access_token=token)
    return client


def _windows(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur <= end:
        to = min(end, cur + timedelta(days=max_days - 1))
        out.append((cur, to))
        cur = to + timedelta(days=1)
    return out


def candles_to_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    """SDK-formatted candles (``date`` is a tz-aware datetime) → date-indexed OHLCV (IST dates)."""
    if not records:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))
    df = pd.DataFrame(records)
    dates = [pd.Timestamp(d.astimezone(IST).date()) for d in df.pop("date")]
    df.index = pd.DatetimeIndex(dates, name="date")
    df = df[OHLCV_COLUMNS].astype({"volume": "int64"})
    return df


class KiteProvider:
    """Implements ``PriceProvider`` and ``IndexPriceProvider``.

    Like Fyers: the token is read per call, no token → ``ProviderUnavailable``, and every HTTP
    request after the first in a call (extra history windows, LTP batches, the instruments
    download) takes its own rate-limit token.
    """

    name = Provider.KITE

    def __init__(
        self,
        api_key: str,
        token_source: Callable[[], str | None],
        limits: ApiLimits,
        instrument_store: InstrumentStore,
        instruments_cache_hours: float,
        *,
        limiter: Limiter | None = None,
        rate_limit_timeout_s: float = 0.0,
        client_factory: Callable[[str, str], KiteClient] = default_client_factory,
    ) -> None:
        self._api_key = api_key
        self._token_source = token_source
        self._limits = limits
        self._limiter = limiter
        self._rate_limit_timeout_s = rate_limit_timeout_s
        self._client_factory = client_factory
        self._client: KiteClient | None = None
        self._client_token: str | None = None
        self._historical_denied_for: str | None = None
        self._requests_this_call = 0
        self.instruments = KiteInstruments(
            self._download_instruments, instrument_store, instruments_cache_hours
        )

    # ───────────── plumbing ─────────────

    def _get_client(self) -> KiteClient:
        token = self._token_source()
        if not token:
            raise ProviderUnavailable("no valid Kite token — reconnect Kite in Settings")
        if self._client is None or token != self._client_token:
            self._client = self._client_factory(self._api_key, token)
            self._client_token = token
        return self._client

    def _request[T](self, fn: Callable[[], T], what: str) -> T:
        """One HTTP request: rate-limit all but the first of a call, map SDK errors."""
        if self._requests_this_call > 0 and self._limiter is not None:
            self._limiter.acquire(self.name, timeout=self._rate_limit_timeout_s)
        self._requests_this_call += 1
        try:
            return fn()
        except Exception as exc:
            _raise_mapped(exc, what)

    def _begin_call(self) -> KiteClient:
        self._requests_this_call = 0
        return self._get_client()

    def _active_client(self) -> KiteClient:
        """The client resolved by ``_begin_call`` (one token lookup per public call)."""
        if self._client is None:
            raise RuntimeError("_begin_call() must run first")
        return self._client

    def _download_instruments(self) -> list[dict[str, Any]]:
        client = self._active_client()
        return self._request(lambda: client.instruments(EXCHANGE), "instruments dump")

    # ───────────── OHLCV ─────────────

    def _history(self, token_id: int | None, label: str, start: date, end: date) -> pd.DataFrame:
        if token_id is None:
            raise ProviderUnavailable(f"Kite: {label} not in the NSE instruments dump")
        client = self._active_client()
        frames = []
        for frm, to in _windows(start, end, self._limits.history_max_days):
            request = partial(
                client.historical_data,
                token_id,
                f"{frm.isoformat()} 00:00:00",
                f"{to.isoformat()} 23:59:59",
                "day",
            )
            try:
                records = self._request(request, f"historical {label}")
            except ProviderUnavailable as exc:
                if isinstance(exc.__cause__, kex.PermissionException):
                    self._historical_denied_for = self._client_token
                    logger.warning("Kite historical data not permitted; using fallbacks")
                raise
            frames.append(candles_to_frame(records))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return candles_to_frame([])
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.loc[pd.Timestamp(start) : pd.Timestamp(end)]

    def _guarded_history(
        self, lookup: Callable[[], int | None], label: str, start: date, end: date
    ) -> pd.DataFrame:
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        self._begin_call()
        if self._historical_denied_for is not None and (
            self._historical_denied_for == self._client_token
        ):
            raise ProviderUnavailable("Kite historical data not permitted for this account")
        return self._history(lookup(), label, start, end)

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._guarded_history(
            lambda: self.instruments.equity_token(symbol), f"NSE:{symbol}", start, end
        )

    def index_ohlcv(self, index: str, start: date, end: date) -> pd.DataFrame:
        return self._guarded_history(
            lambda: self.instruments.index_token(index), f"index {index}", start, end
        )

    # ───────────── LTP ─────────────

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price keyed by the caller's symbols; unknown symbols are omitted."""
        client = self._begin_call()
        by_kite = {f"{EXCHANGE}:{s.strip().upper()}": s for s in symbols}
        out: dict[str, float] = {}
        for batch in batched(by_kite, self._limits.quotes_max_symbols):
            data = self._request(partial(client.ltp, list(batch)), "ltp")
            for key, item in (data or {}).items():
                price = item.get("last_price") if isinstance(item, dict) else None
                if key in by_kite and price is not None:
                    out[by_kite[key]] = float(price)
        return out


def build_kite_provider(
    api_key: str | None,
    token_source: Callable[[], str | None],
    config: ProvidersConfig,
    instrument_store: InstrumentStore,
    limiter: Limiter | None,
) -> KiteProvider | None:
    """``None`` when KITE_API_KEY is not set (router then reports kite as not configured)."""
    if not api_key:
        return None
    return KiteProvider(
        api_key,
        token_source,
        config.api_limits[Provider.KITE],
        instrument_store,
        config.instruments_cache_hours,
        limiter=limiter,
        rate_limit_timeout_s=config.retry.rate_limit_timeout_s,
    )
