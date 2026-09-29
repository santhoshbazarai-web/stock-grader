"""Instrument-token lookup for Kite, built from the daily instruments dump.

Kite's historical API is keyed by numeric ``instrument_token``, not symbol. The NSE dump
(``GET /instruments/NSE``, CSV, republished daily) maps ``tradingsymbol`` → token. Lookups go:

1. in-process map, if younger than ``instruments_cache_hours``;
2. the shared store (Redis hash with the same TTL), so api and worker download it once;
3. a fresh download, written back to the store.

Keys: ``EQ:<TRADINGSYMBOL>`` for NSE equities, ``IDX:<NAME WITHOUT SPACES>`` for indices, so
Kite's ``NIFTY 500`` is found as ``IDX:NIFTY500`` (the canonical index code used elsewhere).
"""

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, cast

from redis import Redis

from app.data.providers.base import ProviderError

logger = logging.getLogger(__name__)

EQUITY_TYPE = "EQ"
EQUITY_SEGMENT = "NSE"
INDEX_SEGMENT = "INDICES"


def equity_key(symbol: str) -> str:
    return f"EQ:{symbol.strip().upper()}"


def index_key(index: str) -> str:
    return f"IDX:{index.replace(' ', '').strip().upper()}"


def build_token_map(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Instrument rows (as parsed by ``KiteConnect.instruments``) → lookup map."""
    out: dict[str, int] = {}
    for row in rows:
        segment, symbol = row.get("segment"), str(row.get("tradingsymbol", ""))
        token = int(row["instrument_token"])
        if segment == EQUITY_SEGMENT and row.get("instrument_type") == EQUITY_TYPE:
            out[equity_key(symbol)] = token
        elif segment == INDEX_SEGMENT:
            out[index_key(symbol)] = token
    return out


class InstrumentStore(Protocol):
    def load(self) -> dict[str, int] | None: ...

    def save(self, mapping: Mapping[str, int], ttl_s: float) -> None: ...


class MemoryInstrumentStore:
    def __init__(self) -> None:
        self.data: dict[str, int] | None = None
        self.saves = 0

    def load(self) -> dict[str, int] | None:
        return dict(self.data) if self.data else None

    def save(self, mapping: Mapping[str, int], ttl_s: float) -> None:
        self.data = dict(mapping)
        self.saves += 1


class RedisInstrumentStore:
    def __init__(self, redis: Redis, key: str = "kite:instruments:NSE") -> None:
        self._redis = redis
        self._key = key

    def load(self) -> dict[str, int] | None:
        raw: dict[bytes, bytes] = self._redis.hgetall(self._key)  # type: ignore[assignment]
        if not raw:
            return None
        return {k.decode(): int(v) for k, v in raw.items()}

    def save(self, mapping: Mapping[str, int], ttl_s: float) -> None:
        tmp = f"{self._key}:tmp"
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(tmp)
        if mapping:
            pipe.hset(tmp, mapping=cast(Any, dict(mapping)))
            pipe.rename(tmp, self._key)  # atomic swap: readers never see a half-written hash
            pipe.expire(self._key, max(1, int(ttl_s)))
        pipe.execute()


class KiteInstruments:
    def __init__(
        self,
        download: Callable[[], list[dict[str, Any]]],
        store: InstrumentStore,
        ttl_hours: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._download = download
        self._store = store
        self._ttl_s = ttl_hours * 3600
        self._clock = clock
        self._map: dict[str, int] | None = None
        self._loaded_at = 0.0

    def _current(self) -> dict[str, int]:
        if self._map is not None and self._clock() - self._loaded_at < self._ttl_s:
            return self._map
        mapping = self._store.load()
        if mapping is None:
            mapping = build_token_map(self._download())
            if not mapping:
                raise ProviderError("Kite instruments dump had no NSE equities or indices")
            logger.info("downloaded Kite NSE instruments dump (%d instruments)", len(mapping))
            self._store.save(mapping, self._ttl_s)
        self._map, self._loaded_at = mapping, self._clock()
        return mapping

    def equity_token(self, symbol: str) -> int | None:
        return self._current().get(equity_key(symbol))

    def index_token(self, index: str) -> int | None:
        return self._current().get(index_key(index))
