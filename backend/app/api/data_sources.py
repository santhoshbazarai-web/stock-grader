"""Settings → Data sources: the last nse-diagnose / bse-diagnose result per endpoint, and a
re-check that runs both in the background (``app/data/diagnose.py``)."""

from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, status
from pydantic import BaseModel, Field
from redis import Redis
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, RedisDep, SessionDep, SettingsDep
from app.core.config import AppConfig
from app.core.rate_limiter import RateLimiter
from app.core.settings import Settings
from app.data import diagnose as diag
from app.data.providers.indianapi import source_status
from app.data.providers.web_session import RedisMemory

router = APIRouter(prefix="/data-sources", tags=["admin"])

DEFAULT_SYMBOL = "HDFCBANK"  # per-symbol NSE APIs are probed with a large, always-listed stock
RUNNING_TTL_S = 900  # a crashed re-check frees the flag after this


class DiagRowOut(BaseModel):
    method: str
    endpoint: str
    url: str
    status: int | None
    server: str | None
    content_type: str | None
    cookie_names: list[str] = Field(description="Names only; values are never stored")
    length: int
    verdict: str


class SiteDiagOut(BaseModel):
    site: str
    checked_at: str
    methods: list[str]
    working_method: str | None
    summary: dict[str, dict[str, str]] = Field(
        description="endpoint → {verdict, method}: OK with the first method that got it"
    )
    rows: list[DiagRowOut]


class IndianApiOut(BaseModel):
    enabled: bool
    configured: bool = Field(description="Enabled and INDIANAPI_KEY set (the key is never shown)")
    used: int = Field(description="Calls this calendar month (IST)")
    budget: int
    stop_at: int = Field(description="Calls stop here (stop_at_fraction of the budget)")
    month: str
    message: str = Field(description='e.g. "Indian API: 123/500 calls this month"')


class DataSourcesOut(BaseModel):
    running: bool
    nse: SiteDiagOut | None
    bse: SiteDiagOut | None
    indianapi: IndianApiOut | None = None


def _site(raw: dict[str, Any] | None) -> SiteDiagOut | None:
    return SiteDiagOut.model_validate(raw) if raw else None


def _indianapi(session: Session, config: AppConfig, settings: Settings) -> IndianApiOut:
    st = source_status(session, config.providers.indianapi, settings.indianapi_key,
                       datetime.now(ZoneInfo(config.jobs.timezone)))  # fmt: skip
    return IndianApiOut.model_validate(asdict(st))


@router.get("")
def data_sources(redis: RedisDep, session: SessionDep, config: ConfigDep,
                 settings: SettingsDep) -> DataSourcesOut:  # fmt: skip
    """The last diagnose result per site (from the CLI or the Re-check button), and the
    Indian API's state and calls this month."""
    return DataSourcesOut(running=bool(redis.exists(diag.RUNNING_KEY)),
                          nse=_site(diag.load(redis, "nse")),
                          bse=_site(diag.load(redis, "bse")),
                          indianapi=_indianapi(session, config, settings))  # fmt: skip


def _recheck(redis: Redis, config: AppConfig) -> None:
    pc = config.providers
    tz = ZoneInfo(config.jobs.timezone)
    try:
        for site in ("nse", "bse"):
            now = datetime.now(tz)
            report = diag.run_site(
                site, pc, symbol=DEFAULT_SYMBOL, today=now.date(), now=now,
                limiter=RateLimiter(redis, pc.rate_limits),
                rate_limit_timeout_s=pc.retry.rate_limit_timeout_s, memory=RedisMemory(redis),
            )  # fmt: skip
            diag.store(redis, report)
    finally:
        redis.delete(diag.RUNNING_KEY)


@router.post("/check", status_code=status.HTTP_202_ACCEPTED)
def recheck(redis: RedisDep, config: ConfigDep, tasks: BackgroundTasks, session: SessionDep,
            settings: SettingsDep) -> DataSourcesOut:  # fmt: skip
    """Run nse-diagnose and bse-diagnose now (in the background, at the usual 1 request/s);
    poll ``GET /data-sources`` until ``running`` is false. Only one check runs at a time."""
    if redis.set(diag.RUNNING_KEY, "1", nx=True, ex=RUNNING_TTL_S):
        tasks.add_task(_recheck, redis, config)
    return data_sources(redis, session, config, settings)
