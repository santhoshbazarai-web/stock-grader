"""Job registry and runner (SPEC §10).

Every run:
1. takes a Redis lock ``job-lock:<name>`` (non-blocking; expires after ``jobs.lock_ttl_s`` so a
   crashed run cannot wedge the job). If another run holds it, a ``skipped`` row is written.
2. writes a ``running`` row to ``job_runs`` (committed immediately, so it is visible live);
3. runs the job, which must be idempotent (upserts only);
4. finishes the row as ``success`` / ``skipped`` / ``failed`` with rows written, a details
   summary and — on failure — the exception type and message.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from redis import Redis
from redis.exceptions import LockError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.alerts.telegram import TelegramNotifier
from app.core.config import AppConfig, JobName
from app.data.gaps import GapRecorder
from app.data.router import DataRouter
from app.db.enums import JobStatus
from app.db.models import JobRun

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 2000


@dataclass
class JobContext:
    config: AppConfig
    session_factory: Callable[[], Session]
    router: DataRouter
    redis: Redis
    gaps: GapRecorder
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    notifier: TelegramNotifier | None = None  # alerts: optional Telegram delivery

    def now(self) -> datetime:
        return self.clock().astimezone(ZoneInfo(self.config.jobs.timezone))

    def today(self) -> date:
        """Calendar date in the exchange timezone (IST)."""
        return self.now().date()


@dataclass(frozen=True)
class JobOptions:
    symbols: tuple[str, ...] | None = None  # restrict to these symbols (CLI --symbols)
    full: bool = False  # full-history backfill instead of the incremental window
    day: date | None = None  # for date-keyed jobs (bhavcopy); default: today
    force: bool = False  # run seasonal jobs outside their season

    def as_params(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols) if self.symbols else None,
            "full": self.full,
            "day": self.day.isoformat() if self.day else None,
            "force": self.force,
        }


@dataclass
class JobOutcome:
    rows_written: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    skipped_reason: str | None = None


JobFn = Callable[[JobContext, JobOptions], JobOutcome]


@dataclass(frozen=True)
class JobSpec:
    name: JobName
    description: str
    fn: JobFn | None = None
    pending_phase: str | None = None  # set while the job's engine is not built yet


class JobNotImplementedError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    status: JobStatus
    outcome: JobOutcome | None
    error: str | None = None


def _write_run(ctx: JobContext, **values: Any) -> int:
    session = ctx.session_factory()
    try:
        run = JobRun(**values)
        session.add(run)
        session.commit()
        return run.id
    finally:
        session.close()


def _finish_run(ctx: JobContext, run_id: int, **values: Any) -> None:
    session = ctx.session_factory()
    try:
        session.execute(update(JobRun).where(JobRun.id == run_id).values(**values))
        session.commit()
    finally:
        session.close()


def run_job(spec: JobSpec, ctx: JobContext, options: JobOptions | None = None) -> RunRecord:
    options = options or JobOptions()
    if spec.fn is None:
        raise JobNotImplementedError(
            f"{spec.name} is not implemented yet ({spec.pending_phase or 'later phase'})"
        )
    params = options.as_params()
    lock = ctx.redis.lock(
        f"job-lock:{spec.name}", timeout=ctx.config.jobs.lock_ttl_s, blocking=False
    )
    if not lock.acquire(blocking=False):
        logger.warning("%s already running; skipped", spec.name)
        run_id = _write_run(
            ctx,
            job_name=str(spec.name),
            status=JobStatus.SKIPPED,
            started_at=ctx.clock(),
            finished_at=ctx.clock(),
            params=params,
            rows_written=0,
            details={"reason": "another run holds the lock"},
        )
        return RunRecord(run_id, JobStatus.SKIPPED, None)

    try:
        run_id = _write_run(
            ctx,
            job_name=str(spec.name),
            status=JobStatus.RUNNING,
            started_at=ctx.clock(),
            params=params,
        )
        logger.info("%s started (run %s)", spec.name, run_id)
        try:
            outcome = spec.fn(ctx, options)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            logger.exception("%s failed (run %s)", spec.name, run_id)
            _finish_run(ctx, run_id, status=JobStatus.FAILED, finished_at=ctx.clock(), error=error)
            return RunRecord(run_id, JobStatus.FAILED, None, error)
        status = JobStatus.SKIPPED if outcome.skipped_reason else JobStatus.SUCCESS
        details = dict(outcome.details)
        if outcome.skipped_reason:
            details["reason"] = outcome.skipped_reason
        _finish_run(
            ctx,
            run_id,
            status=status,
            finished_at=ctx.clock(),
            rows_written=outcome.rows_written,
            details=details,
        )
        logger.info("%s %s: %s rows (run %s)", spec.name, status, outcome.rows_written, run_id)
        return RunRecord(run_id, status, outcome)
    finally:
        try:
            lock.release()
        except LockError:  # expired (ran longer than lock_ttl_s) or already gone
            logger.warning("%s lock expired before release", spec.name)
