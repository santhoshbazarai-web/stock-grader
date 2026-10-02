"""Indian API transport (app/data/providers/indianapi.py): the key header and its scrubbing, the
not-configured state, retries, the HTTP errors that are never retried, and the monthly budget.
Scripted fake HTTP sessions only; nothing touches the network."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Provider, load_config
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.providers.indianapi import (
    KEY_HEADER,
    NOT_CONFIGURED,
    BudgetExhausted,
    DbQuota,
    IndianApiClient,
    NotConfigured,
    Unauthorized,
    VendorFormatError,
    VendorNotFound,
    month_of,
    scrub,
    usage_text,
)
from tests.conftest import REPO_CONFIG_DIR

PC = load_config(REPO_CONFIG_DIR).providers
CFG = PC.indianapi
STOCK = CFG.calls_per_stock[0]
YOY = CFG.calls_per_stock[1]
KEY = "test-key-NOT-REAL-0123456789"
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)


@dataclass
class Resp:
    status_code: int
    content: bytes = b'{"companyName": "X"}'


@dataclass
class FakeSession:
    script: list[Resp | Exception]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str],
            timeout: float) -> Resp:  # fmt: skip
        self.calls.append({"url": url, "params": params, "headers": headers})
        r = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(r, Exception):
            raise r
        return r


@dataclass
class CountingQuota:
    left: int = 100
    n: int = 0

    def reserve(self) -> int:
        if self.left <= 0:
            raise BudgetExhausted("budget reached")
        self.left -= 1
        self.n += 1
        return self.n

    def used(self) -> int:
        return self.n


def client(script: list[Resp | Exception], *, key: str | None = KEY,
           quota: CountingQuota | None = None) -> tuple[IndianApiClient, FakeSession]:  # fmt: skip
    s = FakeSession(script)
    c = IndianApiClient(CFG, SecretStr(key) if key is not None else None, quota=quota,
                        limiter=None, retry=PC.retry, session=s,  # type: ignore[arg-type]
                        sleep=lambda _: None, clock=lambda: NOW)  # fmt: skip
    return c, s


def test_key_goes_only_in_the_header() -> None:
    c, s = client([Resp(200, b'{"companyName": "HDFC Bank"}')])
    r = c.get(STOCK, "HDFC Bank")
    assert r.payload == {"companyName": "HDFC Bank"} and r.status == 200
    call = s.calls[0]
    assert call["url"] == "https://stock.indianapi.in/stock"
    assert call["headers"] == {KEY_HEADER: KEY} and call["params"] == {"name": "HDFC Bank"}
    assert KEY not in json.dumps(r.params) and r.params == {"name": "HDFC Bank"}
    y = c.get(YOY, "TCS")
    assert y.params == {"stats": "yoy_results", "stock_name": "TCS"}


def test_missing_key_is_not_configured_without_a_request() -> None:
    for key in (None, "", "   "):
        c, s = client([Resp(200)], key=key)
        assert not c.configured
        with pytest.raises(NotConfigured, match=r"add INDIANAPI_KEY in \.env"):
            c.get(STOCK, "TCS")
        assert s.calls == []
    assert NOT_CONFIGURED == "Indian API not configured: add INDIANAPI_KEY in .env"
    assert issubclass(NotConfigured, ProviderUnavailable)  # the router never retries it


@pytest.mark.parametrize(
    ("status", "exc", "words"),
    [
        (404, VendorNotFound, "HTTP 404 (no stock by that name)"),
        (401, Unauthorized, "check INDIANAPI_KEY"),
        (403, Unauthorized, "HTTP 403"),
        (400, ProviderUnavailable, "HTTP 400"),
    ],
)
def test_permanent_errors_are_not_retried(status: int, exc: type[Exception], words: str) -> None:
    c, s = client([Resp(status, b"")])
    with pytest.raises(exc, match=words.replace("(", r"\(").replace(")", r"\)")):
        c.get(STOCK, "TCS")
    assert len(s.calls) == 1


def test_429_and_5xx_are_retried_then_fail() -> None:
    quota = CountingQuota()
    c, s = client([Resp(429, b""), Resp(503, b""), Resp(200, b'{"a": 1}')], quota=quota)
    r = c.get(STOCK, "TCS")
    assert r.attempts == 3 and len(s.calls) == 3
    assert quota.n == 3  # every request sent counts against the budget
    c2, s2 = client([Resp(429, b"")])
    with pytest.raises(ProviderError, match="HTTP 429") as info:
        c2.get(STOCK, "TCS")
    assert not isinstance(info.value, ProviderUnavailable)
    assert len(s2.calls) == PC.retry.max_attempts


def test_errors_and_logs_never_carry_the_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    # a transport error whose text happens to contain the key (e.g. echoed by a proxy)
    c, _ = client([requests.ConnectionError(f"refused for header {KEY}")])
    with pytest.raises(ProviderError) as info:
        c.get(STOCK, "TCS")
    assert KEY not in str(info.value) and "network error ConnectionError" in str(info.value)
    assert KEY not in caplog.text
    assert scrub(f"a {KEY} b", KEY) == "a *** b" and scrub("x", None) == "x"


def test_non_json_or_empty_answer_is_a_format_error() -> None:
    for body in (b"<html>maintenance</html>", b"{}", b"[]"):
        c, _ = client([Resp(200, body)])
        with pytest.raises(VendorFormatError):
            c.get(STOCK, "TCS")


def test_exhausted_budget_stops_before_any_request() -> None:
    c, s = client([Resp(200)], quota=CountingQuota(left=0))
    with pytest.raises(BudgetExhausted):
        c.get(STOCK, "TCS")
    assert s.calls == []


def test_disabled_in_config_is_not_configured() -> None:
    off = CFG.model_copy(update={"enabled": False})
    c = IndianApiClient(off, SecretStr(KEY), quota=None, limiter=None, retry=PC.retry)
    with pytest.raises(NotConfigured, match=r"indianapi\.enabled"):
        c.get(STOCK, "TCS")


# ───────────────────────── monthly budget (database) ─────────────────────────


@pytest.fixture
def quota_db(migrated_engine: Engine) -> Iterator[sessionmaker]:  # type: ignore[type-arg]
    sf = sessionmaker(bind=migrated_engine)
    with sf() as s:
        s.execute(text("DELETE FROM api_usage"))
        s.commit()
    yield sf
    with sf() as s:
        s.execute(text("DELETE FROM api_usage"))
        s.commit()


def test_quota_counts_per_ist_month_and_stops_at_90_percent(quota_db: sessionmaker) -> None:  # type: ignore[type-arg]
    small = CFG.model_copy(update={"monthly_request_budget": 10})  # stops at 9
    q = DbQuota(quota_db, small, clock=lambda: NOW)
    assert q.used() == 0
    assert [q.reserve() for _ in range(9)] == list(range(1, 10))
    with pytest.raises(BudgetExhausted) as info:
        q.reserve()
    msg = str(info.value)
    assert msg.startswith("Indian API budget reached: 9/10 calls this month (stops at 90%)")
    assert q.used() == 9  # the refused call is not counted
    # a new month (IST): 30 Oct 19:00 UTC is already 1 Nov 00:30 IST
    nxt = DbQuota(quota_db, small, clock=lambda: datetime(2026, 10, 31, 19, 0, tzinfo=UTC))
    assert month_of(datetime(2026, 10, 31, 19, 0, tzinfo=UTC)) == "2026-11"
    assert nxt.reserve() == 1
    assert usage_text(123, 500) == "Indian API: 123/500 calls this month"
    with quota_db() as s:
        rows = s.execute(text("SELECT provider, month, calls FROM api_usage ORDER BY month")).all()
    assert [tuple(r) for r in rows] == [(Provider.INDIANAPI.value, "2026-10", 9),
                                        (Provider.INDIANAPI.value, "2026-11", 1)]  # fmt: skip


def test_source_status_messages(db: Any) -> None:
    from app.data.providers.indianapi import source_status

    assert source_status(db, CFG, None, NOW).message == "Add INDIANAPI_KEY in .env"
    off = CFG.model_copy(update={"enabled": False})
    assert "indianapi.enabled" in source_status(db, off, SecretStr(KEY), NOW).message
    db.execute(text("INSERT INTO api_usage (provider, month, calls) VALUES ('indianapi', "
                    "'2026-10', 123)"))  # fmt: skip
    st = source_status(db, CFG, SecretStr(KEY), NOW)
    assert st.configured and st.used == 123 and st.month == "2026-10"
    assert st.message == "Indian API: 123/500 calls this month"
    db.execute(text("UPDATE api_usage SET calls = 450"))
    full = source_status(db, CFG, SecretStr(KEY), NOW).message
    assert full.startswith("Indian API: 450/500 calls this month: budget reached")
    assert KEY not in repr(st)
