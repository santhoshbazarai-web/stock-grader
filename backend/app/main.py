"""FastAPI application factory. Run with ``uvicorn app.main:app``.

OpenAPI docs: ``/api/docs`` (Swagger UI), ``/api/redoc``, schema at ``/api/openapi.json``.
Everything except ``/api/health``, ``/api/auth/login|logout`` and the broker OAuth callbacks
requires the single-user session (see ``app.core.auth``).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app import __version__
from app.api import (
    admin,
    annual_reports,
    auth,
    brokers,
    data_sources,
    events,
    health,
    notes,
    notifications,
    pipeline,
    preferences,
    price_anomalies,
    screener,
    screens,
    stock_tabs,
    stocks,
    technical,
    thesis,
    valuation_map,
    watchlist,
)
from app.api.deps import require_user
from app.core.config import get_config
from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

TAGS = [
    {"name": "auth", "description": "Single-user login (APP_PASSWORD) → session cookie / token"},
    {"name": "stocks", "description": "Search, StockReport, refresh, DCF sensitivity, overrides"},
    {"name": "technical", "description": "Chart-overlay debug output of the technical engine"},
    {"name": "screener", "description": "Filter and sort the latest reports"},
    {"name": "watchlist & alerts", "description": "Watchlist and in-app price alerts"},
    {"name": "notifications", "description": "Triggered alerts (in-app; Telegram optional)"},
    {"name": "uploads", "description": "Screener.in Excel exports"},
    {"name": "config", "description": "View / edit the validated YAML config"},
    {"name": "backtests", "description": "Point-in-time backtests (SPEC §11)"},
    {"name": "jobs", "description": "Job history and data freshness"},
    {"name": "brokers", "description": "Read-only broker connections (Fyers, Kite)"},
    {"name": "health", "description": "Liveness"},
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail fast: an invalid env or config/*.yaml aborts startup.
    get_config()
    logger.info("config loaded from %s", settings.config_dir)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Stock Grader API",
        version=__version__,
        description=(
            "Grades NSE stocks: valuation levels and zone, technical buy zone, six-pillar grade "
            "and action (SPEC §8). Authenticate with `POST /api/auth/login`; the session is "
            "sent as the `sg_session` cookie or `Authorization: Bearer <token>`."
        ),
        openapi_tags=TAGS,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    authed = [Depends(require_user)]
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(brokers.router, prefix="/api")  # per-route auth; callbacks use state
    for module in (
        stocks, technical, screener, watchlist, notifications, admin, annual_reports, pipeline,
        events, thesis, data_sources, price_anomalies, preferences, valuation_map, screens,
        notes, stock_tabs,
    ):  # fmt: skip
        app.include_router(
            module.router,
            prefix="/api",
            dependencies=authed,
            responses={401: {"description": "Not logged in"}},
        )
    return app


app = create_app()
