"""Worker process: APScheduler running the SPEC §10 jobs. Run with ``python -m app.jobs.worker``."""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.config import get_config
from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Kolkata"


def build_scheduler() -> BlockingScheduler:
    # Jobs are registered here from P6 onwards.
    return BlockingScheduler(timezone=TIMEZONE)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail fast: an invalid env or config/*.yaml aborts startup.
    get_config()
    scheduler = build_scheduler()
    logger.info("worker started with %d job(s)", len(scheduler.get_jobs()))
    scheduler.start()


if __name__ == "__main__":
    main()
