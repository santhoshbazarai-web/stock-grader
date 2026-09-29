"""On-demand refresh queue (SPEC §8 ``POST /api/stocks/{symbol}/refresh``).

The API pushes a symbol onto a Redis list (deduplicated by a set, so repeated clicks queue it
once); the worker's ``refresh_queue`` job drains it every minute. For each symbol it re-runs the
per-symbol data jobs (prices, corporate actions, results, shareholding — seasonal windows
ignored), then rebuilds and stores the report.
"""

from redis import Redis

QUEUE_KEY = "refresh:queue"
PENDING_KEY = "refresh:pending"


def enqueue_refresh(redis: Redis, symbol: str) -> tuple[bool, int]:
    """(newly queued, queue length). A symbol already waiting is not queued twice."""
    sym = symbol.upper()
    added = bool(redis.sadd(PENDING_KEY, sym))
    if added:
        redis.rpush(QUEUE_KEY, sym)
    return added, int(redis.llen(QUEUE_KEY))


def pop_refresh(redis: Redis) -> str | None:
    raw = redis.lpop(QUEUE_KEY)
    if raw is None:
        return None
    sym = raw.decode() if isinstance(raw, bytes) else str(raw)
    redis.srem(PENDING_KEY, sym)
    return sym


def queued(redis: Redis) -> list[str]:
    return [s.decode() if isinstance(s, bytes) else str(s) for s in redis.lrange(QUEUE_KEY, 0, -1)]
