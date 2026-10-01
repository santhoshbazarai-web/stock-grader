"""Catch-up on worker start (SPEC §3.10): jobs missed while the machine was off run once, in
the order they were due."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.config import JobName
from app.db.enums import JobStatus
from app.db.models import JobRun
from app.jobs.catch_up import catch_up, find_missed, last_fire_time, missed_jobs
from tests.jobs_support import NOW, Env

IST = ZoneInfo("Asia/Kolkata")
SCHEDULES = {
    JobName.CORPORATE_ACTIONS: "0 18 * * mon-fri",
    JobName.EOD_PRICES: "15 18 * * mon-fri",
    JobName.TECHNICALS: "15 19 * * mon-fri",
    JobName.VALUATION_SCORES: "30 23 * * *",
    JobName.INDEX_CONSTITUENTS: "0 7 1 * *",
    JobName.ALERTS_INTRADAY: "*/5 9-15 * * mon-fri",
}
# Monday 17 Jun 2024 09:00 IST: the machine slept from Friday evening
MONDAY = datetime(2024, 6, 17, 9, 0, tzinfo=IST)


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2024, 6, day, hh, mm, tzinfo=IST)


def test_last_fire_time() -> None:
    since = MONDAY - timedelta(hours=72)
    assert last_fire_time("15 18 * * mon-fri", IST, MONDAY, since) == at(14, 18, 15)  # Friday
    assert last_fire_time("30 23 * * *", IST, MONDAY, since) == at(16, 23, 30)  # Sunday
    assert last_fire_time("0 7 1 * *", IST, MONDAY, since) is None  # 1 Jun: too old


def test_missed_jobs_in_due_order() -> None:
    last = {
        "corporate_actions": at(14, 18, 0),  # ran on Friday: fine
        "eod_prices": at(13, 18, 15),  # Thursday's run only: Friday's missed
        "technicals": None,  # never ran
        "valuation_scores": at(15, 23, 30),  # Saturday's ran, Sunday's missed
    }
    missed = missed_jobs(SCHEDULES, last, now=MONDAY, tz=IST, lookback=timedelta(hours=72),
                         skip={"alerts_intraday"})  # fmt: skip
    assert [(m.job.value, m.due_at) for m in missed] == [
        ("eod_prices", at(14, 18, 15)),
        ("technicals", at(14, 19, 15)),
        ("valuation_scores", at(16, 23, 30)),
    ]
    assert missed[0].last_started == at(13, 18, 15)


def test_short_lookback_ignores_older_misses() -> None:
    # Sunday 23:30 is 9.5 h before Monday 09:00; Friday's jobs are older
    missed = missed_jobs(SCHEDULES, {}, now=MONDAY, tz=IST, lookback=timedelta(hours=12),
                         skip={"alerts_intraday"})  # fmt: skip
    assert [m.job.value for m in missed] == ["valuation_scores"]
    missed = missed_jobs(SCHEDULES, {}, now=MONDAY, tz=IST, lookback=timedelta(hours=9),
                         skip={"alerts_intraday"})  # fmt: skip
    assert missed == []


def test_catch_up_runs_missed_jobs_once(env: Env) -> None:
    c = env.ctx.config
    keep = {"broker_token_check", "valuation_scores"}
    cu = c.jobs.catch_up.model_copy(
        update={"skip": [j.value for j in JobName if j.value not in keep]}
    )
    env.ctx.config = c.model_copy(update={"jobs": c.jobs.model_copy(update={"catch_up": cu})})
    # NOW is Friday 14 Jun 18:30 IST: broker_token_check (08:45) and valuation_scores
    # (Thursday 23:30) are due; valuation_scores ran after its due time → only the reminder
    with env.session() as s:
        s.add(
            JobRun(
                job_name="valuation_scores",
                status=JobStatus.SUCCESS,
                started_at=datetime(2024, 6, 13, 18, 5, tzinfo=UTC),
            )
        )  # 23:35 IST
        s.commit()
    assert [m.job for m in find_missed(env.ctx)] == [JobName.BROKER_TOKEN_CHECK]
    assert catch_up(env.ctx) == [(JobName.BROKER_TOKEN_CHECK, "success")]
    assert find_missed(env.ctx) == []  # the run now counts
    assert catch_up(env.ctx) == []
    with env.session() as s:
        names = list(s.scalars(select(JobRun.job_name).order_by(JobRun.id)))
    assert names == ["valuation_scores", "broker_token_check"]
    assert NOW.astimezone(IST).hour == 18


def test_catch_up_disabled(env: Env) -> None:
    c = env.ctx.config
    cu = c.jobs.catch_up.model_copy(update={"enabled": False})
    env.ctx.config = c.model_copy(update={"jobs": c.jobs.model_copy(update={"catch_up": cu})})
    assert catch_up(env.ctx) == []
