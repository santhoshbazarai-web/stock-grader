"""Dataset router: priority + fallback + staleness (SPEC §3.1-3.2).

For each dataset the router walks ``providers.yaml`` ``priority`` in order and returns the first
provider's result that is non-empty and fresh. Per provider it takes a rate-limit token before
every try and retries transient :class:`ProviderError` with exponential backoff; it falls through
on :class:`ProviderUnavailable`, exhausted retries, empty data, stale data, a rate-limit timeout,
or a provider that is not configured / does not implement the needed protocol.

If every provider fails:
- and at least one returned *stale* data → the freshest stale result is returned, flagged
  ``stale=True`` (stale data is not missing data);
- otherwise → ``data is None`` and a ``DataGap`` is recorded (AGENTS.md rule 1).

Every result carries ``source`` (the provider that served it), ``fetched_at`` and
``reasons`` explaining the route taken.

Freshness (``as_of``):
- A DataFrame's ``attrs["as_of"]`` (tz-aware ``datetime``) always wins.
- OHLCV datasets otherwise use the last bar's date at the NSE close (15:30 IST).
- Anything else with no ``as_of`` is treated as fresh (freshness unknown).
Note: without a trading calendar, OHLCV fetched over a weekend/holiday is flagged stale.
"""

import logging
import time as _time
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.config import Dataset, Provider, ProvidersConfig
from app.core.rate_limiter import Limiter, RateLimitTimeout
from app.data.gaps import GapRecord, GapRecorder
from app.data.providers.base import (
    AnnualReportsProvider,
    BhavcopyHistoryProvider,
    ConstituentsProvider,
    CorporateActionsProvider,
    DeliveryProvider,
    EventsProvider,
    FundamentalsProvider,
    IndexPriceProvider,
    PriceProvider,
    ProviderError,
    ProviderUnavailable,
    ReferenceFinancialsProvider,
    ResultsFilingsProvider,
    ShareholdingProvider,
    SurveillanceProvider,
)
from app.db.enums import EventKind

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
NSE_CLOSE = time(15, 30)
_BAR_DATASETS = frozenset({Dataset.DAILY_OHLCV, Dataset.INDEX_OHLCV})


class Outcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    STALE = "stale"
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Attempt:
    provider: Provider
    outcome: Outcome
    detail: str | None = None
    tries: int = 0

    def describe(self) -> str:
        text = f"{self.provider}: {self.outcome}"
        if self.tries > 1:
            text += f" after {self.tries} tries"
        return f"{text} ({self.detail})" if self.detail else text


@dataclass(frozen=True)
class RouteResult[T]:
    dataset: Dataset
    symbol: str | None
    data: T | None
    source: Provider | None
    fetched_at: datetime
    as_of: datetime | None
    stale: bool
    attempts: tuple[Attempt, ...]

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def reasons(self) -> list[str]:
        out = [a.describe() for a in self.attempts]
        if self.data is None:
            out.append(f"no provider returned {self.dataset}; recorded as a data gap")
        elif self.stale:
            out.append(f"all providers stale; using freshest ({self.source}, as of {self.as_of})")
        else:
            out.append(f"served by {self.source}")
        if isinstance(self.data, pd.DataFrame):
            out += [f"{self.source}: {w}" for w in self.data.attrs.get("warnings", [])]
        return out


def _is_empty(data: object) -> bool:
    if data is None:
        return True
    if isinstance(data, pd.DataFrame | pd.Series):
        return data.empty
    if isinstance(data, Sized):
        return len(data) == 0
    return False


def _as_of(dataset: Dataset, data: object) -> datetime | None:
    if isinstance(data, pd.DataFrame):
        explicit = data.attrs.get("as_of")
        if isinstance(explicit, datetime):
            return explicit if explicit.tzinfo else explicit.replace(tzinfo=UTC)
        if dataset in _BAR_DATASETS and isinstance(data.index, pd.DatetimeIndex):
            last: date = data.index.max().date()
            return datetime.combine(last, NSE_CLOSE, tzinfo=IST)
    return None


class DataRouter:
    def __init__(
        self,
        providers: Mapping[Provider, object],
        config: ProvidersConfig,
        *,
        limiter: Limiter,
        gaps: GapRecorder,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self._providers = dict(providers)
        self._config = config
        self._limiter = limiter
        self._gaps = gaps
        self._clock = clock
        self._sleep = sleep

    # ───────────── typed entry points ─────────────

    def daily_ohlcv(self, symbol: str, start: date, end: date) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.DAILY_OHLCV,
            PriceProvider,
            lambda p: p.daily_ohlcv(symbol, start, end),
            symbol=symbol,
        )

    def index_ohlcv(self, index: str, start: date, end: date) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.INDEX_OHLCV,
            IndexPriceProvider,
            lambda p: p.index_ohlcv(index, start, end),
            symbol=None,
        )

    def ltp(self, symbols: list[str]) -> RouteResult[dict[str, float]]:
        return self.fetch(
            Dataset.LTP,
            PriceProvider,
            lambda p: p.ltp(symbols),
            symbol=symbols[0] if len(symbols) == 1 else None,
        )

    def fin_annual(self, symbol: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.FIN_ANNUAL, FundamentalsProvider, lambda p: p.annual(symbol), symbol=symbol
        )

    def fin_quarterly(self, symbol: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.FIN_QUARTERLY,
            FundamentalsProvider,
            lambda p: p.quarterly(symbol),
            symbol=symbol,
        )

    def delivery(self, day: date) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.DELIVERY, DeliveryProvider, lambda p: p.delivery(day), symbol=None
        )

    def index_constituents(self, index: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.INDEX_CONSTITUENTS,
            ConstituentsProvider,
            lambda p: p.index_constituents(index),
            symbol=None,
        )

    def surveillance(self) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.SURVEILLANCE, SurveillanceProvider, lambda p: p.surveillance(), symbol=None
        )

    def corporate_actions(self, symbol: str, start: date, end: date) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.CORPORATE_ACTIONS,
            CorporateActionsProvider,
            lambda p: p.corporate_actions(symbol, start, end),
            symbol=symbol,
        )

    def shareholding(self, symbol: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.SHAREHOLDING,
            ShareholdingProvider,
            lambda p: p.shareholding(symbol),
            symbol=symbol,
        )

    def results_filings(self, symbol: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.RESULTS_FILINGS,
            ResultsFilingsProvider,
            lambda p: p.results_filings(symbol),
            symbol=symbol,
        )

    def annual_reports(self, symbol: str) -> RouteResult[pd.DataFrame]:
        return self.fetch(
            Dataset.ANNUAL_REPORTS,
            AnnualReportsProvider,
            lambda p: p.annual_reports(symbol),
            symbol=symbol,
        )

    def annual_report_document(self, url: str, symbol: str) -> RouteResult[bytes]:
        """One annual report. No data gap on failure: the caller keeps a per-report ledger."""
        return self.fetch(
            Dataset.ANNUAL_REPORTS,
            AnnualReportsProvider,
            lambda p: p.annual_report_document(url),
            symbol=symbol,
            record_gap=False,
        )

    def results_document(self, url: str, symbol: str) -> RouteResult[bytes]:
        """One XBRL document. No data gap on failure: the caller keeps a per-filing ledger."""
        return self.fetch(
            Dataset.RESULTS_FILINGS,
            ResultsFilingsProvider,
            lambda p: p.results_document(url),
            symbol=symbol,
            record_gap=False,
        )

    def events(
        self, provider: Provider, kind: EventKind, start: date, end: date
    ) -> RouteResult[pd.DataFrame]:
        """One exchange feed from one provider (every provider's feeds are read, so this is
        not a fallback chain). No data gap: the events / results_watch jobs report failures."""
        return self.fetch(
            Dataset.EVENTS,
            EventsProvider,
            lambda p: p.events(kind, start, end),
            symbol=None,
            providers=[provider],
            record_gap=False,
        )

    def bhavcopy_backfill(self, days: list[date]) -> RouteResult[dict[str, list[str]]]:
        """Fetch NSE bhavcopy files for ``days`` into the history (bhavcopy_history job)."""
        return self.fetch(
            Dataset.DAILY_OHLCV,
            BhavcopyHistoryProvider,
            lambda p: p.load_bhavcopy_days(days),
            symbol=None,
            providers=[Provider.NSE],
            record_gap=False,
        )

    def reference_financials(self, provider: Provider, symbol: str) -> RouteResult[pd.DataFrame]:
        """A reconciliation-only source's figures (SPEC §3.9); failures are reported by the
        reconciliation, not recorded as data gaps."""
        return self.fetch(
            Dataset.REFERENCE_FINANCIALS,
            ReferenceFinancialsProvider,
            lambda p: p.reference_financials(symbol),
            symbol=symbol,
            providers=[provider],
            record_gap=False,
        )

    # ───────────── generic routing ─────────────

    def ltp_filled(self, symbols: list[str]) -> tuple[dict[str, tuple[float, Provider]], list[str]]:
        """LTP for every symbol it can get: the first provider in priority answers what it can,
        the next is asked only for the symbols still missing, and so on (a broker that rejects
        one symbol must not cost the rest their prices). Returns ({symbol: (price, source)},
        reasons). Symbols no provider priced are recorded as one data gap."""
        prices: dict[str, tuple[float, Provider]] = {}
        reasons: list[str] = []
        for name in self._config.priority[Dataset.LTP]:
            missing = [s for s in symbols if s not in prices]
            if not missing:
                break
            res = self.fetch(
                Dataset.LTP,
                PriceProvider,
                lambda p, m=missing: p.ltp(m),  # type: ignore[misc]
                symbol=None,
                providers=[name],
                record_gap=False,
            )
            reasons += [a.describe() for a in res.attempts]
            for sym, price in (res.data or {}).items():
                if sym in missing and price and price > 0:
                    prices[sym] = (float(price), name)
        unpriced = [s for s in symbols if s not in prices]
        if unpriced:
            reason = f"no LTP for {', '.join(unpriced)} ({'; '.join(reasons) or 'no providers'})"
            reasons.append(reason)
            self._gaps.record(
                GapRecord(
                    dataset=Dataset.LTP,
                    symbol=unpriced[0] if len(unpriced) == 1 else None,
                    reason=reason[:2000],
                    providers_tried=list(self._config.priority[Dataset.LTP]),
                )
            )
        return prices, reasons

    def fetch[T](
        self,
        dataset: Dataset,
        protocol: type,
        call: Callable[[Any], T],
        *,
        symbol: str | None,
        providers: list[Provider] | None = None,
        record_gap: bool = True,
    ) -> RouteResult[T]:
        now = self._clock()
        hours = self._config.staleness_hours.get(dataset)
        max_age = timedelta(hours=hours) if hours is not None else None
        attempts: list[Attempt] = []
        freshest_stale: tuple[Provider, T, datetime] | None = None

        def result(
            data: T | None, source: Provider | None, as_of: datetime | None, stale: bool
        ) -> RouteResult[T]:
            return RouteResult(
                dataset=dataset,
                symbol=symbol,
                data=data,
                source=source,
                fetched_at=now,
                as_of=as_of,
                stale=stale,
                attempts=tuple(attempts),
            )

        for name in providers if providers is not None else self._config.priority[dataset]:
            impl = self._providers.get(name)
            if impl is None:
                attempts.append(Attempt(name, Outcome.NOT_CONFIGURED))
                continue
            if not isinstance(impl, protocol):  # runtime_checkable Protocol
                attempts.append(Attempt(name, Outcome.UNSUPPORTED, protocol.__name__))
                continue

            outcome, data, detail, tries = self._call(name, impl, call)
            if outcome is not Outcome.OK:
                attempts.append(Attempt(name, outcome, detail, tries))
                continue
            assert data is not None  # _call returns data only with Outcome.OK

            as_of = _as_of(dataset, data)
            if max_age is not None and as_of is not None and now - as_of > max_age:
                attempts.append(Attempt(name, Outcome.STALE, f"as of {as_of.isoformat()}", tries))
                if freshest_stale is None or as_of > freshest_stale[2]:
                    freshest_stale = (name, data, as_of)
                continue

            attempts.append(Attempt(name, Outcome.OK, tries=tries))
            self._gaps.resolve(dataset, symbol)
            return result(data, name, as_of, stale=False)

        if freshest_stale is not None:
            name, data, as_of = freshest_stale
            logger.warning("%s %s: all providers stale, using %s", dataset, symbol, name)
            return result(data, name, as_of, stale=True)

        res: RouteResult[T] = result(None, None, None, stale=False)
        logger.warning("%s %s: no provider succeeded: %s", dataset, symbol, res.reasons)
        if not record_gap:
            return res
        self._gaps.record(
            GapRecord(
                dataset=dataset,
                symbol=symbol,
                reason="; ".join(a.describe() for a in attempts) or "no providers in priority",
                providers_tried=[str(a.provider) for a in attempts],
            )
        )
        return res

    def _call[T](
        self, name: Provider, impl: Any, call: Callable[[Any], T]
    ) -> tuple[Outcome, T | None, str | None, int]:
        """One provider with rate limiting and retries → (outcome, data, detail, tries)."""
        retry = self._config.retry
        for attempt in range(1, retry.max_attempts + 1):
            try:
                self._limiter.acquire(name, timeout=retry.rate_limit_timeout_s)
            except RateLimitTimeout as exc:
                return Outcome.RATE_LIMITED, None, str(exc), attempt - 1
            try:
                data = call(impl)
            except ProviderUnavailable as exc:
                return Outcome.UNAVAILABLE, None, str(exc) or type(exc).__name__, attempt
            except ProviderError as exc:
                detail = str(exc) or type(exc).__name__
                if attempt == retry.max_attempts:
                    return Outcome.ERROR, None, detail, attempt
                delay = min(retry.backoff_max_s, retry.backoff_base_s * 2 ** (attempt - 1))
                logger.info(
                    "%s try %d failed (%s); retrying in %.2fs", name, attempt, detail, delay
                )
                self._sleep(delay)
                continue
            except Exception as exc:  # unexpected: don't retry, but don't abort the route
                logger.exception("%s raised unexpectedly", name)
                return Outcome.ERROR, None, f"{type(exc).__name__}: {exc}", attempt
            if _is_empty(data):
                return Outcome.EMPTY, None, None, attempt
            return Outcome.OK, data, None, attempt
        raise AssertionError("unreachable")  # pragma: no cover
