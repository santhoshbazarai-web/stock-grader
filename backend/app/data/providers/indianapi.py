"""stock.indianapi.in: the bulk fundamentals source when NSE is unavailable (SPEC §3.2).

The vendor looks stocks up by *name* and answers JSON. This module is the transport only:
- the key (env ``INDIANAPI_KEY``) goes in the ``X-Api-Key`` header and nowhere else. It is
  never logged, never stored with a response, and scrubbed from every error message;
- every request takes a rate-limit token (``rate_limits.indianapi``) and one unit of the
  monthly budget (:class:`DbQuota`). Calls stop at ``stop_at_fraction`` of the budget;
- transient failures (network, 5xx, 429) are retried with exponential backoff
  (``providers.retry``); 401/403/404 and an exhausted budget are not.

Without a key the provider is "not configured": callers skip it, and the UI says
"Add INDIANAPI_KEY in .env". Name resolution, identity checks, the raw cache and the mapping
to canonical statements are in ``app/data/indianapi_*.py``.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import IndianApiConfig, IndianApiEndpoint, Provider, RetryConfig
from app.core.rate_limiter import Limiter, RateLimitTimeout
from app.data.providers.base import ProviderError, ProviderUnavailable

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
KEY_HEADER = "X-Api-Key"
NOT_CONFIGURED = "Indian API not configured: add INDIANAPI_KEY in .env"
DISABLED = "Indian API disabled in providers.yaml (indianapi.enabled)"


class NotConfigured(ProviderUnavailable):
    """No INDIANAPI_KEY (or the source is disabled in providers.yaml)."""


class BudgetExhausted(ProviderUnavailable):
    """The month's call budget is used up to ``stop_at_fraction``."""


class VendorNotFound(ProviderUnavailable):
    """HTTP 404: the vendor has no stock under this name."""


class Unauthorized(ProviderUnavailable):
    """HTTP 401 / 403: the key is wrong, expired or not entitled."""


class VendorFormatError(ProviderUnavailable):
    """The answer is not the JSON object the endpoint returns."""


def scrub(text_: str, secret: str | None) -> str:
    """``text_`` with the key (if any) replaced: errors and logs never carry it."""
    return text_.replace(secret, "***") if secret else text_


def usage_text(used: int, budget: int) -> str:
    return f"Indian API: {used}/{budget} calls this month"


# ───────────────────────── monthly budget ─────────────────────────


class Quota(Protocol):
    def reserve(self) -> int:
        """Count one call; raises :class:`BudgetExhausted` when none is left."""
        ...

    def used(self) -> int: ...


def month_of(now: datetime) -> str:
    return now.astimezone(IST).strftime("%Y-%m")


def calls_this_month(session: Session, now: datetime,
                     provider: Provider = Provider.INDIANAPI) -> int:  # fmt: skip
    n = session.execute(
        text("SELECT calls FROM api_usage WHERE provider = :p AND month = :m"),
        {"p": provider.value, "m": month_of(now)},
    ).scalar()
    return int(n or 0)


@dataclass(frozen=True)
class SourceStatus:
    """What the Data sources page and the pipeline panel say about the Indian API."""

    enabled: bool
    configured: bool
    used: int
    budget: int
    stop_at: int
    month: str
    message: str


def source_status(session: Session, cfg: IndianApiConfig, key: SecretStr | None,
                  now: datetime) -> SourceStatus:  # fmt: skip
    has_key = key is not None and bool(key.get_secret_value().strip())
    used = calls_this_month(session, now)
    if not cfg.enabled:
        msg = DISABLED
    elif not has_key:
        msg = "Add INDIANAPI_KEY in .env"
    elif used >= cfg.stop_at:
        msg = (f"{usage_text(used, cfg.monthly_request_budget)}: budget reached (stops at "
               f"{cfg.stop_at_fraction:.0%}); cached data is used")  # fmt: skip
    else:
        msg = usage_text(used, cfg.monthly_request_budget)
    return SourceStatus(cfg.enabled, cfg.enabled and has_key, used, cfg.monthly_request_budget,
                        cfg.stop_at, month_of(now), msg)  # fmt: skip


_RESERVE = text(
    "INSERT INTO api_usage (provider, month, calls, last_call_at) "
    "VALUES (:provider, :month, 1, :now) "
    "ON CONFLICT (provider, month) DO UPDATE "
    "SET calls = api_usage.calls + 1, last_call_at = :now "
    "WHERE api_usage.calls < :stop_at RETURNING calls"
)


class DbQuota:
    """The ``api_usage`` row of the current IST month. ``reserve`` is one atomic statement:
    concurrent workers can't overshoot the limit."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cfg: IndianApiConfig,
        *,
        provider: Provider = Provider.INDIANAPI,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sf, self._cfg, self._provider, self._clock = session_factory, cfg, provider, clock

    def reserve(self) -> int:
        if self._cfg.stop_at < 1:
            raise BudgetExhausted(self.exhausted_message(0))
        now = self._clock()
        with self._sf() as s:
            args = {"provider": self._provider.value, "month": month_of(now), "now": now,
                    "stop_at": self._cfg.stop_at}  # fmt: skip
            row = s.execute(_RESERVE, args).first()
            s.commit()
        if row is None:
            raise BudgetExhausted(self.exhausted_message(self.used()))
        return int(row[0])

    def used(self) -> int:
        with self._sf() as s:
            return calls_this_month(s, self._clock(), self._provider)

    def exhausted_message(self, used: int) -> str:
        c = self._cfg
        return (f"Indian API budget reached: {used}/{c.monthly_request_budget} calls this month "
                f"(stops at {c.stop_at_fraction:.0%}). Cached data is used; new calls resume "
                "next month, or raise providers.indianapi.monthly_request_budget")  # fmt: skip


# ───────────────────────── transport ─────────────────────────


@dataclass(frozen=True)
class VendorResponse:
    """One answer. ``params`` never contains the key."""

    endpoint: str
    params: dict[str, str]
    status: int
    content: bytes
    payload: Any
    fetched_at: datetime
    attempts: int = 1
    notes: list[str] = field(default_factory=list)


class IndianApiClient:
    def __init__(
        self,
        cfg: IndianApiConfig,
        key: SecretStr | None,
        *,
        quota: Quota | None,
        limiter: Limiter | None,
        retry: RetryConfig,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg, self._quota, self._limiter, self._retry = cfg, quota, limiter, retry
        self._key = key.get_secret_value().strip() if key is not None else ""
        self._http = session or requests.Session()
        self._sleep, self._clock = sleep, clock

    @property
    def configured(self) -> bool:
        return bool(self._key) and self._cfg.enabled

    def used_this_month(self) -> int | None:
        return self._quota.used() if self._quota is not None else None

    def _fail(self, exc_type: type[ProviderError], msg: str) -> ProviderError:
        return exc_type(scrub(msg, self._key))

    def get(self, endpoint: IndianApiEndpoint, vendor_name: str) -> VendorResponse:
        if not self.configured:
            raise NotConfigured(NOT_CONFIGURED if self._cfg.enabled else DISABLED)
        params = {**endpoint.params, endpoint.name_param: vendor_name}
        url = f"{self._cfg.base_url}{endpoint.path}"
        what = f"Indian API {endpoint.path} {params}"
        last: ProviderError | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            if attempt > 1:
                self._sleep(min(self._retry.backoff_max_s,
                                self._retry.backoff_base_s * 2 ** (attempt - 2)))  # fmt: skip
            if self._quota is not None:
                self._quota.reserve()  # BudgetExhausted propagates: never retried
            if self._limiter is not None:
                try:
                    self._limiter.acquire(Provider.INDIANAPI,
                                          timeout=self._retry.rate_limit_timeout_s)  # fmt: skip
                except RateLimitTimeout as exc:
                    raise self._fail(ProviderError, f"{what}: {exc}") from None
            try:
                resp = self._http.get(url, params=params, headers={KEY_HEADER: self._key},
                                      timeout=self._cfg.request_timeout_s)  # fmt: skip
            except requests.RequestException as exc:
                last = self._fail(ProviderError, f"{what}: network error {type(exc).__name__}")
                logger.info("%s", last)
                continue
            status = resp.status_code
            if status in (401, 403):
                raise self._fail(Unauthorized, f"{what}: HTTP {status} (check INDIANAPI_KEY)")
            if status == 404:
                raise self._fail(VendorNotFound, f"{what}: HTTP 404 (no stock by that name)")
            if status == 429 or status >= 500:
                last = self._fail(ProviderError, f"{what}: HTTP {status}")
                logger.info("%s (try %d)", last, attempt)
                continue
            if status >= 400:
                raise self._fail(ProviderUnavailable, f"{what}: HTTP {status}")
            try:
                payload = json.loads(resp.content)
            except ValueError:
                raise self._fail(VendorFormatError, f"{what}: not JSON") from None
            if not isinstance(payload, dict) or not payload:
                raise self._fail(VendorFormatError, f"{what}: empty or not a JSON object")
            return VendorResponse(endpoint.path, params, status, resp.content, payload,
                                  self._clock(), attempt)  # fmt: skip
        assert last is not None
        raise last


def build_indianapi_client(
    cfg: IndianApiConfig,
    key: SecretStr | None,
    retry: RetryConfig,
    *,
    quota: Quota | None,
    limiter: Limiter | None,
) -> IndianApiClient:
    """Always built (so the UI can say why it is unused); ``configured`` tells callers."""
    return IndianApiClient(cfg, key, quota=quota, limiter=limiter, retry=retry)
