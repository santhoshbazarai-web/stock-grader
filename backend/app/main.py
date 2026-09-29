"""FastAPI application factory. Run with ``uvicorn app.main:app``."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import brokers, health
from app.core.config import get_config
from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail fast: an invalid env or config/*.yaml aborts startup.
    get_config()
    logger.info("config loaded from %s", settings.config_dir)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Stock Grader API", version=__version__, lifespan=lifespan)
    app.include_router(health.router, prefix="/api")
    app.include_router(brokers.router, prefix="/api")
    return app


app = create_app()
