"""Per-host circuit breaker (app/core/circuit_breaker.py) and its use by the router."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from redis import Redis

from app.core.circuit_breaker import CircuitBreaker, paused_text
from app.core.config import BreakerConfig, Provider

CFG = BreakerConfig(hosts=[Provider.NSE], cooldown_min=[15, 60, 360])


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2025, 6, 18, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def parts(redis_client: Redis) -> Iterator[tuple[CircuitBreaker, Clock]]:
    prefix = f"test-br-{uuid.uuid4().hex}"
    clock = Clock()
    yield CircuitBreaker(redis_client, CFG, clock=clock, prefix=prefix), clock
    for k in redis_client.scan_iter(f"{prefix}:*"):
        redis_client.delete(k)


def test_cooldown_escalates_15_min_1_h_6_h(parts: tuple[CircuitBreaker, Clock]) -> None:
    br, clock = parts
    assert br.paused_until(Provider.NSE) is None
    for minutes in (15, 60, 360, 360):  # the last level repeats
        until = br.trip(Provider.NSE)
        assert until == clock.t + timedelta(minutes=minutes)
        assert br.paused_until(Provider.NSE) == until
        clock.t = until + timedelta(seconds=1)  # the pause ends; blocked again right after
        assert br.paused_until(Provider.NSE) is None


def test_a_block_while_paused_changes_nothing_and_success_resets(
    parts: tuple[CircuitBreaker, Clock],
) -> None:
    br, clock = parts
    first = br.trip(Provider.NSE)
    assert br.trip(Provider.NSE) == first  # same pause, no escalation
    clock.t = first + timedelta(minutes=1)
    br.success(Provider.NSE)  # a call got through: back to the first level
    assert br.trip(Provider.NSE) == clock.t + timedelta(minutes=15)


def test_unwatched_hosts_are_never_paused(parts: tuple[CircuitBreaker, Clock]) -> None:
    br, _ = parts
    assert br.trip(Provider.YFINANCE) is None and br.paused_until(Provider.YFINANCE) is None
    assert br.paused() == {}


def test_text_is_in_indian_time(parts: tuple[CircuitBreaker, Clock]) -> None:
    br, _ = parts
    until = br.trip(Provider.NSE)
    assert until is not None
    assert paused_text("nse", until) == "NSE paused until 14:45 IST"  # 09:15 UTC
    assert br.paused() == {"nse": until}
