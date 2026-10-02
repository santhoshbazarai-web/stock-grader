"""Per-provider rate limiting with Redis token buckets (AGENTS.md rule 9).

Each provider has two buckets from ``providers.yaml`` ``rate_limits``: ``per_sec`` (capacity
``per_sec``, refilled at ``per_sec``/s) and ``per_min`` (capacity ``per_min``, refilled at
``per_min``/60 per s). A request takes a token from *both* buckets atomically (one Lua script),
so api and worker processes share the same budget.

Time comes from Redis (``TIME``) unless a clock is injected, so api and worker containers with
skewed clocks cannot freeze a bucket (a ``ts`` in the future never refilled).

Priority: a stock the user is waiting on (``on_demand()``) may drain the buckets; background
jobs (the default) always leave ``background_reserve`` of the per-minute bucket untouched and
step aside while an on-demand caller waits (a ``user_waiting`` key that expires by itself, so a
crashed waiter cannot starve the jobs). ``release`` returns a token for a request never sent.
"""

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol

from redis import Redis

from app.core.config import Provider, RateLimit

# KEYS: bucket keys. ARGV: now (< 0: Redis TIME), tokens, then (capacity, refill_per_sec,
# reserve) for each key; ``reserve`` tokens must remain after taking (0 for on-demand callers).
# Returns the seconds to wait before retrying ("0" when the tokens were taken).
_TOKEN_BUCKET_LUA = """
local now = tonumber(ARGV[1])
if now < 0 then
  local t = redis.call('TIME')
  now = tonumber(t[1]) + tonumber(t[2]) / 1000000
end
local n = tonumber(ARGV[2])
local wait = 0
local state = {}
for i, key in ipairs(KEYS) do
  local cap = tonumber(ARGV[3 * i])
  local rate = tonumber(ARGV[3 * i + 1])
  local reserve = tonumber(ARGV[3 * i + 2])
  local b = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(b[1]) or cap
  local ts = tonumber(b[2]) or now
  if now > ts then
    tokens = math.min(cap, tokens + (now - ts) * rate)
  end
  ts = now
  state[i] = {tokens, ts, cap, rate}
  if tokens < n + reserve then
    wait = math.max(wait, (n + reserve - tokens) / rate)
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


Priority = Literal["on_demand", "background"]
_PRIORITY: ContextVar[Priority] = ContextVar("rate_limit_priority", default="background")

_RELEASE_LUA = """
local key, n, cap = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2])
local t = tonumber(redis.call('HGET', key, 'tokens'))
if t then redis.call('HSET', key, 'tokens', tostring(math.min(cap, t + n))) end
return 1
"""


@contextmanager
def on_demand() -> Iterator[None]:
    """Requests made inside (a user waiting on a stock) take priority over background jobs."""
    token = _PRIORITY.set("on_demand")
    try:
        yield
    finally:
        _PRIORITY.reset(token)


class Limiter(Protocol):
    def acquire(self, provider: Provider, *, timeout: float) -> None: ...


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        limits: Mapping[Provider, RateLimit],
        *,
        key_prefix: str = "ratelimit",
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        background_reserve: float = 0.0,
    ) -> None:
        self._limits = dict(limits)
        self._prefix = key_prefix
        self._clock = clock  # None: Redis TIME
        self._sleep = sleep
        self._redis = redis
        self._reserve = background_reserve  # share of the per-minute bucket kept for users
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)
        self._release = redis.register_script(_RELEASE_LUA)

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        sec, usec = self._redis.time()
        return float(sec) + usec / 1e6

    def _waiting_key(self, provider: Provider) -> str:
        return f"{self._prefix}:{provider}:user_waiting"

    def _buckets(self, provider: Provider) -> list[tuple[str, float, float]]:
        limit = self._limits[provider]
        return [
            (f"{self._prefix}:{provider}:sec", limit.per_sec, limit.per_sec),
            (f"{self._prefix}:{provider}:min", limit.per_min, limit.per_min / 60.0),
        ]

    def try_acquire(
        self, provider: Provider, tokens: int = 1, *, priority: Priority | None = None
    ) -> float:
        """Take ``tokens`` if available and return 0; otherwise take nothing and return the
        seconds until they would be available. Providers without a limit always succeed.
        Background callers also wait while an on-demand caller is waiting."""
        if provider not in self._limits:
            return 0.0
        prio = priority or _PRIORITY.get()
        buckets = self._buckets(provider)
        if any(tokens > cap for _, cap, _ in buckets):
            raise ValueError(f"{tokens} tokens exceeds bucket capacity for {provider}")
        if prio == "background" and self._redis.exists(self._waiting_key(provider)):
            return 0.5
        args: list[float] = [-1.0 if self._clock is None else self._clock(), tokens]
        for key, cap, rate in buckets:
            reserve = self._reserve * cap if prio == "background" and key.endswith(":min") else 0
            args += [cap, rate, reserve]
        result = self._script(keys=[k for k, _, _ in buckets], args=args)
        return float(result)

    def acquire(
        self, provider: Provider, *, timeout: float, priority: Priority | None = None
    ) -> None:
        """Block until a token is taken; raise :class:`RateLimitTimeout` after ``timeout`` s."""
        prio = priority or _PRIORITY.get()
        deadline = self._now() + timeout
        flag = self._waiting_key(provider)
        try:
            while True:
                wait = self.try_acquire(provider, priority=prio)
                if wait == 0:
                    return
                if self._now() + wait > deadline:
                    raise RateLimitTimeout(f"{provider}: no rate-limit token within {timeout}s")
                if prio == "on_demand":  # background callers yield; expires if we crash
                    self._redis.set(flag, "1", px=int((wait + 2) * 1000))
                self._sleep(wait)
        finally:
            if prio == "on_demand":
                self._redis.delete(flag)

    def release(self, provider: Provider, tokens: int = 1) -> None:
        """Give back tokens for a request that never reached the host."""
        if provider not in self._limits:
            return
        for key, cap, _ in self._buckets(provider):
            self._release(keys=[key], args=[tokens, cap])
