"""Catch-up on worker start (SPEC v0.2 §3.10, §10 ``catch_up``): a home machine that was off
or asleep misses scheduled jobs, and APScheduler (no persistent job store) does not replay
them. On start the worker finds, for every scheduled job, its latest fire time within
``jobs.catch_up.lookback_hours``; a job whose last ``job_runs`` row started before that time
(or that never ran) is missed. Missed jobs run once each, in the order they were due, so the
evening chain keeps its dependencies (corporate actions → prices → bhavcopy → technicals →
valuation). Jobs in ``catch_up.skip`` (high-frequency or on-demand queues) are left to their
next trigger. Each run goes through ``run_job``: the Redis lock stops a scheduled trigger and
the catch-up from running the same job twice, and seasonal jobs skip themselves off-season.
"""

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from app.core.config import JobName
from app.db.models import JobRun
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobContext, run_job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Missed:
    job: JobName
    due_at: datetime  # the latest fire time that has no run after it
    last_started: datetime | None


def last_fire_time(expr: str, tz: ZoneInfo, now: datetime, since: datetime) -> datetime | None:
    """The latest fire time of a crontab expression in (since, now], or None."""
    trigger = CronTrigger.from_crontab(expr, timezone=tz)
    last, t = None, trigger.get_next_fire_time(None, since)
    while t is not None and t <= now:
        last = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return last


def missed_jobs(
    schedules: Mapping[JobName, str],
    last_started: Mapping[str, datetime | None],
    *,
    now: datetime,
    tz: ZoneInfo,
    lookback: timedelta,
    skip: set[str],
) -> list[Missed]:
    """Pure: the jobs due within ``lookback`` that have not started since, oldest due first
    (ties in schedule order)."""
    order = {name: i for i, name in enumerate(schedules)}
    out = []
    for name, expr in schedules.items():
        if name.value in skip:
            continue
        due = last_fire_time(expr, tz, now, now - lookback)
        if due is None:
            continue
        started = last_started.get(name.value)
        if started is None or started < due:
            out.append(Missed(name, due, started))
    return sorted(out, key=lambda m: (m.due_at, order[m.job]))


def find_missed(ctx: JobContext) -> list[Missed]:
    cfg = ctx.config.jobs
    session = ctx.session_factory()
    try:
        rows = session.execute(
            select(JobRun.job_name, func.max(JobRun.started_at)).group_by(JobRun.job_name)
        ).all()
    finally:
        session.close()
    implemented = {n: e for n, e in cfg.schedules.items() if REGISTRY[n].fn is not None}
    return missed_jobs(implemented, dict(rows), now=ctx.now(), tz=ZoneInfo(cfg.timezone),
                       lookback=timedelta(hours=cfg.catch_up.lookback_hours),
                       skip=set(cfg.catch_up.skip))  # fmt: skip


def catch_up(ctx: JobContext, stop: threading.Event | None = None) -> list[tuple[JobName, str]]:
    """Run every missed job once, in due order. Returns (job, status) per job run."""
    if not ctx.config.jobs.catch_up.enabled:
        return []
    missed = find_missed(ctx)
    if missed:
        logger.info("catch-up: %d missed job(s): %s", len(missed),
                    ", ".join(f"{m.job} (due {m.due_at:%d %b %H:%M})" for m in missed))  # fmt: skip
    done = []
    for m in missed:
        if stop is not None and stop.is_set():
            break
        rec = run_job(REGISTRY[m.job], ctx)
        done.append((m.job, rec.status.value))
    return done
