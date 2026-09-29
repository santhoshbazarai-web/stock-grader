"""Per-provider rate limiting with Redis token buckets (AGENTS.md rule 9).

Each provider has two buckets from ``providers.yaml`` ``rate_limits``: ``per_sec`` (capacity
``per_sec``, refilled at ``per_sec``/s) and ``per_min`` (capacity ``per_min``, refilled at
``per_min``/60 per s). A request takes a token from *both* buckets atomically (one Lua script),
so api and worker processes share the same budget.
"""

import time
from collections.abc import Callable, Mapping
from typing import Protocol

from redis import Redis

from app.core.config import Provider, RateLimit

# KEYS: bucket keys. ARGV: now, tokens, then (capacity, refill_per_sec) for each key.
# Returns the seconds to wait before retrying ("0" when the tokens were taken).
_TOKEN_BUCKET_LUA = """
local now = tonumber(ARGV[1])
local n = tonumber(ARGV[2])
local wait = 0
local state = {}
for i, key in ipairs(KEYS) do
  local cap = tonumber(ARGV[1 + 2 * i])
  local rate = tonumber(ARGV[2 + 2 * i])
  local b = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(b[1]) or cap
  local ts = tonumber(b[2]) or now
  if now > ts then
    tokens = math.min(cap, tokens + (now - ts) * rate)
    ts = now
  end
  state[i] = {tokens, ts, cap, rate}
  if tokens < n then
    wait = math.max(wait, (n - tokens) / rate)
  end
end
for i, key in ipairs(KEYS) do
  local s = state[i]
  if wait == 0 then s[1] = s[1] - n end
  redis.call('HSET', key, 'tokens', tostring(s[1]), 'ts', tostring(s[2]))
  redis.call('PEXPIRE', key, math.ceil(s[3] / s[4] * 1000) + 1000)
end
return tostring(wait)
"""


class RateLimitTimeout(Exception):
    """No token became available within the allowed time."""


class Limiter(Protocol):
    def acquire(self, provider: Provider, *, timeout: float) -> None: ...


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        limits: Mapping[Provider, RateLimit],
        *,
        key_prefix: str = "ratelimit",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limits = dict(limits)
        self._prefix = key_prefix
        self._clock = clock
        self._sleep = sleep
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    def _buckets(self, provider: Provider) -> list[tuple[str, float, float]]:
        limit = self._limits[provider]
        return [
            (f"{self._prefix}:{provider}:sec", limit.per_sec, limit.per_sec),
            (f"{self._prefix}:{provider}:min", limit.per_min, limit.per_min / 60.0),
        ]

    def try_acquire(self, provider: Provider, tokens: int = 1) -> float:
        """Take ``tokens`` if available and return 0; otherwise take nothing and return the
        seconds until they would be available. Providers without a limit always succeed."""
        if provider not in self._limits:
            return 0.0
        buckets = self._buckets(provider)
        if any(tokens > cap for _, cap, _ in buckets):
            raise ValueError(f"{tokens} tokens exceeds bucket capacity for {provider}")
        args: list[float] = [self._clock(), tokens]
        for _, cap, rate in buckets:
            args += [cap, rate]
        result = self._script(keys=[k for k, _, _ in buckets], args=args)
        return float(result)

    def acquire(self, provider: Provider, *, timeout: float) -> None:
        """Block until a token is taken; raise :class:`RateLimitTimeout` after ``timeout`` s."""
        deadline = self._clock() + timeout
        while True:
            wait = self.try_acquire(provider)
            if wait == 0:
                return
            if self._clock() + wait > deadline:
                raise RateLimitTimeout(f"{provider}: no rate-limit token within {timeout}s")
            self._sleep(wait)
