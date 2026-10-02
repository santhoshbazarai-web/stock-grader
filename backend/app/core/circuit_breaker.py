"""Per-host circuit breaker (shared through Redis, so api and worker agree).

``trip`` (a blocked / 403 response) pauses the host for ``breaker.cooldown_min[level]`` minutes,
15 min → 1 h → 6 h; a trip while the previous pause has just ended (no success since) moves up a
level. ``success`` resets it. While paused, callers skip the host instead of burning rate-limit
tokens on requests that will be refused.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from redis import Redis

from app.core.config import BreakerConfig, Provider

IST = ZoneInfo("Asia/Kolkata")


def paused_text(host: str, until: datetime) -> str:
    """``NSE paused until 14:35 IST``."""
    return f"{host.upper()} paused until {until.astimezone(IST):%H:%M} IST"


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        cfg: BreakerConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        prefix: str = "breaker",
    ) -> None:
        self._redis, self._cfg, self._clock, self._prefix = redis, cfg, clock, prefix

    def _key(self, host: Provider) -> str:
        return f"{self._prefix}:{host}"

    def _state(self, host: Provider) -> tuple[int, datetime | None]:
        raw = self._redis.get(self._key(host))
        if not raw:
            return -1, None
        d = json.loads(raw)
        return int(d["level"]), datetime.fromisoformat(d["until"])

    def watches(self, host: Provider) -> bool:
        return host in self._cfg.hosts

    def paused_until(self, host: Provider) -> datetime | None:
        """When the pause ends, or None when the host may be called."""
        if not self.watches(host):
            return None
        _, until = self._state(host)
        return until if until is not None and until > self._clock() else None

    def trip(self, host: Provider) -> datetime | None:
        """A blocked response: pause the host, one level longer than last time."""
        if not self.watches(host):
            return None
        level, until = self._state(host)
        now = self._clock()
        if until is not None and until > now:  # already paused: another block changes nothing
            return until
        level = min(level + 1, len(self._cfg.cooldown_min) - 1)
        until = now + timedelta(minutes=self._cfg.cooldown_min[level])
        # the record outlives the pause by the longest cooldown, so the level is remembered
        ttl = int((until - now).total_seconds() + max(self._cfg.cooldown_min) * 60)
        self._redis.set(self._key(host), json.dumps({"level": level, "until": until.isoformat()}),
                        ex=ttl)  # fmt: skip
        return until

    def success(self, host: Provider) -> None:
        if self.watches(host) and self.paused_until(host) is None:
            self._redis.delete(self._key(host))

    def paused(self) -> dict[str, datetime]:
        """Every watched host currently paused → when it resumes."""
        out = {}
        for host in self._cfg.hosts:
            until = self.paused_until(host)
            if until is not None:
                out[str(host.value)] = until
        return out
