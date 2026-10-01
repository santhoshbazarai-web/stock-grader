"""Worker process: APScheduler running the SPEC §10 jobs. Run with ``python -m app.jobs.worker``.

Triggers come from ``config/jobs.yaml``; jobs whose engine is not built yet are logged and not
scheduled. Each trigger goes through ``run_job`` (Redis lock + ``job_runs`` row), with
``coalesce`` and ``max_instances=1`` so a backlog never piles up.
"""

import logging
import threading
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.alerts.bot import build_bot
from app.core.config import AppConfig, JobName, get_config
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobContext, JobSpec, run_job
from app.pipeline.runner import worker_loop

logger = logging.getLogger(__name__)


def _trigger(spec: JobSpec, ctx: JobContext) -> None:
    run_job(spec, ctx)  # failures are recorded in job_runs; the scheduler keeps going


def build_scheduler(ctx: JobContext, config: AppConfig) -> BlockingScheduler:
    tz = ZoneInfo(config.jobs.timezone)
    scheduler = BlockingScheduler(timezone=tz)
    for name in JobName:
        spec = REGISTRY[name]
        if spec.fn is None:
            logger.info("not scheduling %s: pending %s", name, spec.pending_phase)
            continue
        scheduler.add_job(
            _trigger,
            CronTrigger.from_crontab(config.jobs.schedules[name], timezone=tz),
            args=[spec, ctx],
            id=str(name),
            name=spec.description,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=config.jobs.misfire_grace_s,
        )
    return scheduler


def main() -> None:
    from app.jobs.context import build_context

    settings = get_settings()
    configure_logging(settings.log_level)
    config = get_config()  # fail fast on invalid env or config/*.yaml
    ctx = build_context(settings, config)
    scheduler = build_scheduler(ctx, config)
    for job in scheduler.get_jobs():
        logger.info("scheduled %s: %s", job.id, job.trigger)
    stop = threading.Event()
    pipeline = threading.Thread(target=worker_loop, args=(ctx, stop), name="pipeline",
                                daemon=True)  # fmt: skip
    pipeline.start()  # on-demand pipeline runs (SPEC §3.7), picked up within poll_interval_s
    bot = build_bot(settings, config, ctx.session_factory, ctx.redis)
    if bot is not None:  # read-only Telegram bot (P23): long polling, owner's chat only
        threading.Thread(target=bot.run, args=(stop,), name="telegram-bot", daemon=True).start()
    else:
        logger.info("telegram bot not started (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)")
    try:
        scheduler.start()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
