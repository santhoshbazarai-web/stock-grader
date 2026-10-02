import uuid
from collections.abc import Callable, Iterator

import pytest
from redis import Redis

from app.core.config import Provider, RateLimit, load_config
from app.core.rate_limiter import RateLimiter, RateLimitTimeout
from tests.conftest import REPO_CONFIG_DIR

LIMITS = {Provider.FYERS: RateLimit(per_sec=2, per_min=5)}


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


MakeLimiter = Callable[..., RateLimiter]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_limiter(redis_client: Redis, clock: FakeClock) -> Iterator[MakeLimiter]:
    prefix = f"test-rl-{uuid.uuid4().hex}"  # isolate each test's buckets

    def make(limits: dict[Provider, RateLimit] = LIMITS) -> RateLimiter:
        return RateLimiter(redis_client, limits, key_prefix=prefix, clock=clock, sleep=clock.sleep)

    yield make
    for key in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(key)


def test_burst_up_to_per_sec_then_wait(make_limiter: MakeLimiter) -> None:
    rl = make_limiter()
    assert rl.try_acquire(Provider.FYERS) == 0
    assert rl.try_acquire(Provider.FYERS) == 0
    wait = rl.try_acquire(Provider.FYERS)
    assert wait == pytest.approx(0.5)  # 1 token at 2 tokens/s


def test_refills_over_time(make_limiter: MakeLimiter, clock: FakeClock) -> None:
    rl = make_limiter()
    rl.try_acquire(Provider.FYERS)
    rl.try_acquire(Provider.FYERS)
    clock.t += 0.5
    assert rl.try_acquire(Provider.FYERS) == 0


def test_per_min_bucket_limits_sustained_rate(make_limiter: MakeLimiter, clock: FakeClock) -> None:
    rl = make_limiter()
    taken = 0
    for _ in range(20):  # 10 s of attempts, one every 0.5 s: per_sec never binds
        if rl.try_acquire(Provider.FYERS) == 0:
            taken += 1
        clock.t += 0.5
    # capacity 5 plus refill 5/60 per s over ~10 s ≈ 0.8 → still 5
    assert taken == 5
    wait = rl.try_acquire(Provider.FYERS)
    assert 0 < wait <= 12  # one token at 5/60 per s


def test_denied_request_consumes_nothing(make_limiter: MakeLimiter, clock: FakeClock) -> None:
    # Drain per_sec; denied requests must not eat into the per_min budget.
    rl = make_limiter()
    rl.try_acquire(Provider.FYERS)
    rl.try_acquire(Provider.FYERS)
    for _ in range(10):
        assert rl.try_acquire(Provider.FYERS) > 0
    clock.t += 1.0
    assert rl.try_acquire(Provider.FYERS) == 0
    assert rl.try_acquire(Provider.FYERS) == 0
    assert rl.try_acquire(Provider.FYERS) > 0  # per_sec empty again
    clock.t += 1.0
    assert rl.try_acquire(Provider.FYERS) == 0  # 5th of 5 per_min tokens
    clock.t += 1.0
    assert rl.try_acquire(Provider.FYERS) > 0  # per_min exhausted


def test_shared_across_instances(make_limiter: MakeLimiter) -> None:
    a, b = make_limiter(), make_limiter()
    assert a.try_acquire(Provider.FYERS) == 0
    assert b.try_acquire(Provider.FYERS) == 0
    assert a.try_acquire(Provider.FYERS) > 0


def test_providers_are_independent(make_limiter: MakeLimiter) -> None:
    rl = make_limiter({**LIMITS, Provider.KITE: RateLimit(per_sec=1, per_min=1)})
    assert rl.try_acquire(Provider.KITE) == 0
    assert rl.try_acquire(Provider.KITE) > 0
    assert rl.try_acquire(Provider.FYERS) == 0


def test_unlimited_provider(make_limiter: MakeLimiter) -> None:
    rl = make_limiter()
    for _ in range(100):
        assert rl.try_acquire(Provider.SCREENER) == 0


def test_acquire_sleeps_until_token(make_limiter: MakeLimiter, clock: FakeClock) -> None:
    rl = make_limiter()
    for _ in range(3):
        rl.acquire(Provider.FYERS, timeout=5)
    assert clock.sleeps == [pytest.approx(0.5)]


def test_acquire_times_out(make_limiter: MakeLimiter, clock: FakeClock) -> None:
    rl = make_limiter()
    for _ in range(5):
        rl.acquire(Provider.FYERS, timeout=5)
    with pytest.raises(RateLimitTimeout, match="fyers"):
        rl.acquire(Provider.FYERS, timeout=5)  # per_min needs ~12 s


def test_request_larger_than_capacity_rejected(make_limiter: MakeLimiter) -> None:
    with pytest.raises(ValueError, match="capacity"):
        make_limiter().try_acquire(Provider.FYERS, tokens=3)


def test_keys_expire(make_limiter: MakeLimiter, redis_client: Redis) -> None:
    rl = make_limiter()
    rl.try_acquire(Provider.FYERS)
    keys = list(redis_client.scan_iter(f"{rl._prefix}:*"))
    assert len(keys) == 2
    assert all(redis_client.pttl(k) > 0 for k in keys)


def test_repo_limits_cover_network_providers() -> None:
    limits = load_config(REPO_CONFIG_DIR).providers.rate_limits
    assert {Provider.FYERS, Provider.KITE, Provider.YFINANCE, Provider.NSE} <= set(limits)


def test_background_leaves_a_reserve_for_on_demand(redis_client: Redis, clock: FakeClock) -> None:
    prefix = f"test-rl-{uuid.uuid4().hex}"
    rl = RateLimiter(
        redis_client,
        {Provider.NSE: RateLimit(per_sec=10, per_min=10)},
        key_prefix=prefix,
        clock=clock,
        sleep=clock.sleep,
        background_reserve=0.3,
    )
    # burst of 10 per minute: background may take 7, the last 3 are kept for the user
    taken = 0
    while rl.try_acquire(Provider.NSE, priority="background") == 0:
        taken += 1
    assert taken == 7
    for _ in range(3):  # the user still gets through
        assert rl.try_acquire(Provider.NSE, priority="on_demand") == 0
    assert rl.try_acquire(Provider.NSE, priority="on_demand") > 0
    for k in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(k)


def test_background_yields_while_an_on_demand_caller_waits_and_the_flag_expires(
    redis_client: Redis,
) -> None:
    prefix = f"test-rl-{uuid.uuid4().hex}"
    rl = RateLimiter(redis_client, {Provider.NSE: RateLimit(per_sec=10, per_min=600)},
                     key_prefix=prefix)  # real Redis TIME, no injected clock  # fmt: skip
    flag = f"{prefix}:{Provider.NSE}:user_waiting"
    redis_client.set(flag, "1", px=300)  # an on-demand waiter that then crashed
    assert rl.try_acquire(Provider.NSE, priority="background") == 0.5  # steps aside
    assert rl.try_acquire(Provider.NSE, priority="on_demand") == 0  # users are not held up
    import time as _t

    _t.sleep(0.4)  # the flag expired by itself: nobody holds the bucket forever
    assert rl.try_acquire(Provider.NSE, priority="background") == 0
    for k in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(k)


def test_on_demand_acquire_sets_and_clears_its_waiting_flag(
    redis_client: Redis, clock: FakeClock
) -> None:
    prefix = f"test-rl-{uuid.uuid4().hex}"
    seen: list[bool] = []

    def sleep(s: float) -> None:
        seen.append(bool(redis_client.exists(f"{prefix}:{Provider.NSE}:user_waiting")))
        clock.sleep(s)

    rl = RateLimiter(redis_client, {Provider.NSE: RateLimit(per_sec=1, per_min=30)},
                     key_prefix=prefix, clock=clock, sleep=sleep)  # fmt: skip
    rl.acquire(Provider.NSE, timeout=5, priority="on_demand")
    rl.acquire(Provider.NSE, timeout=5, priority="on_demand")  # waits ~1 s for a token
    assert seen == [True] and not redis_client.exists(f"{prefix}:{Provider.NSE}:user_waiting")
    with pytest.raises(RateLimitTimeout):  # the flag is also cleared on failure
        rl.acquire(Provider.NSE, timeout=0.1, priority="on_demand")
    assert not redis_client.exists(f"{prefix}:{Provider.NSE}:user_waiting")
    for k in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(k)


def test_a_skewed_clock_cannot_freeze_a_bucket(redis_client: Redis) -> None:
    prefix = f"test-rl-{uuid.uuid4().hex}"
    limits = {Provider.NSE: RateLimit(per_sec=10, per_min=600)}
    ahead = RateLimiter(redis_client, limits, key_prefix=prefix, clock=lambda: 4e9)  # fmt: skip
    assert ahead.try_acquire(Provider.NSE) == 0  # leaves ts far in the future
    now = RateLimiter(redis_client, limits, key_prefix=prefix)  # Redis TIME
    for _ in range(9):
        assert now.try_acquire(Provider.NSE) == 0  # not frozen: the bucket is usable
    for k in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(k)


def test_release_returns_a_token_for_a_request_never_sent(make_limiter: MakeLimiter) -> None:
    rl = make_limiter()
    rl.try_acquire(Provider.FYERS)
    rl.try_acquire(Provider.FYERS)
    assert rl.try_acquire(Provider.FYERS) > 0
    rl.release(Provider.FYERS)
    assert rl.try_acquire(Provider.FYERS) == 0
