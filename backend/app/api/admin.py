"""Screener uploads, YAML config view/edit, backtest requests, job history (SPEC §8)."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select

from app.api.deps import ConfigDep, RedisDep, SessionDep, SettingsDep
from app.api.schemas import (
    SYMBOL_PATTERN,
    BacktestOut,
    BacktestRequest,
    ConfigFile,
    ConfigSaved,
    ConfigUpdate,
    ConfigView,
    Freshness,
    JobRunOut,
    JobsView,
    UploadSummary,
)
from app.core.config import ConfigError, get_config, load_config
from app.data.gaps import SessionGapRecorder
from app.data.providers.screener_import import ScreenerFormatError, import_screener
from app.db.enums import BacktestStatus, StatementType
from app.db.models import (
    Backtest,
    DataGap,
    DeliveryDaily,
    FinAnnual,
    JobRun,
    PriceDaily,
    Report,
    Shareholding,
    TechnicalSnapshot,
)
from app.jobs.common import ensure_instruments
from app.jobs.refresh import queued

router = APIRouter()

CONFIG_FILES = ("providers", "valuation", "sectors", "scoring", "technical", "jobs")


# ───────────────────────── uploads ─────────────────────────


@router.post(
    "/uploads/screener",
    tags=["uploads"],
    status_code=status.HTTP_201_CREATED,
    responses={413: {"description": "File too large"}, 422: {"description": "Not a Data Sheet"}},
)
def upload_screener(
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="Screener.in Excel export (.xlsx)")],
    symbol: Annotated[str, Form(pattern=SYMBOL_PATTERN)],
    statement_type: Annotated[Literal["consolidated", "standalone"], Form()],
) -> UploadSummary:
    """Import a Screener.in export into fin_annual / fin_quarterly / shareholding. The export
    does not say whether it is consolidated or standalone, so the uploader states it (rule 5).
    Fields the export cannot supply are recorded as data gaps."""
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "expected an .xlsx export")
    content = file.file.read(settings.upload_max_bytes + 1)
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"file larger than {settings.upload_max_bytes} bytes",
        )
    sym = symbol.upper()
    ensure_instruments(session, [sym])
    try:
        summary = import_screener(
            session,
            symbol=sym,
            content=content,
            statement_type=StatementType(statement_type),
            gaps=SessionGapRecorder(session),
        )
    except (ScreenerFormatError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    return UploadSummary(
        symbol=sym,
        statement_type=statement_type,
        annual_rows=summary.annual_rows,
        quarterly_rows=summary.quarterly_rows,
        shareholding_rows=summary.shareholding_rows,
        data_gaps=sorted({f"{g.dataset}: {g.field or '(all)'}" for g in summary.gaps}),
        warnings=summary.warnings,
    )


# ───────────────────────── config ─────────────────────────


@router.get("/config", tags=["config"])
def view_config(settings: SettingsDep, config: ConfigDep) -> ConfigView:
    """The YAML files as stored, plus the validated config in effect."""
    files = [
        ConfigFile(name=n, yaml=(Path(settings.config_dir) / f"{n}.yaml").read_text())  # type: ignore[arg-type]
        for n in CONFIG_FILES
    ]
    return ConfigView(files=files, parsed=config.model_dump(mode="json"))


@router.put(
    "/config",
    tags=["config"],
    responses={422: {"description": "YAML invalid or fails validation; nothing saved"}},
)
def update_config(body: ConfigUpdate, settings: SettingsDep) -> ConfigSaved:
    """Replace one config file. The new YAML is validated together with the other files
    exactly as at startup; only a fully valid config is written (atomically). The API uses it
    immediately; the worker picks it up on restart."""
    try:
        yaml.safe_load(body.yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"YAML error: {exc}") from exc
    live = Path(settings.config_dir)
    with tempfile.TemporaryDirectory() as tmp:
        for n in CONFIG_FILES:
            shutil.copy(live / f"{n}.yaml", Path(tmp) / f"{n}.yaml")
        (Path(tmp) / f"{body.name}.yaml").write_text(body.yaml)
        try:
            load_config(Path(tmp))
        except ConfigError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    target = live / f"{body.name}.yaml"
    fd, staged = tempfile.mkstemp(dir=live, prefix=f".{body.name}.", suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(body.yaml)
    os.replace(staged, target)
    get_config.cache_clear()
    return ConfigSaved(name=body.name, saved=True, note="applied to the API; restart the worker")


# ───────────────────────── backtests ─────────────────────────


def _backtest_out(b: Backtest) -> BacktestOut:
    return BacktestOut(
        id=b.id,
        status=b.status,
        params=b.params,
        results=b.results,
        error=b.error,
        created_at=b.created_at,
        updated_at=b.updated_at,
    )


@router.post("/backtests", tags=["backtests"], status_code=status.HTTP_202_ACCEPTED)
def create_backtest(body: BacktestRequest, session: SessionDep) -> BacktestOut:
    """Queue a backtest (grade set x zone set x holding period, SPEC §11). It runs point-in-time
    in the worker; poll ``GET /api/backtests/{id}`` for status and results. The backtest engine
    lands in P15: until then requests are stored and stay ``queued``."""
    b = Backtest(status=BacktestStatus.QUEUED, params=body.model_dump(mode="json"))
    session.add(b)
    session.commit()
    session.refresh(b)
    return _backtest_out(b)


@router.get(
    "/backtests/{backtest_id}", tags=["backtests"], responses={404: {"description": "Not found"}}
)
def get_backtest(backtest_id: int, session: SessionDep) -> BacktestOut:
    b = session.get(Backtest, backtest_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no backtest {backtest_id}")
    return _backtest_out(b)


# ───────────────────────── jobs ─────────────────────────


@router.get("/jobs", tags=["jobs"])
def jobs(
    session: SessionDep,
    redis: RedisDep,
    job: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> JobsView:
    """Job run history (newest first), data freshness, open data gaps, pending refreshes."""
    q = select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(limit)
    if job:
        q = q.where(JobRun.job_name == job)
    runs = [JobRunOut.model_validate(r) for r in session.scalars(q)]
    freshness = Freshness(
        prices=session.scalar(select(func.max(PriceDaily.date))),
        delivery=session.scalar(select(func.max(DeliveryDaily.date))),
        fundamentals_fetched=session.scalar(select(func.max(FinAnnual.fetched_at))),
        shareholding_period=session.scalar(select(func.max(Shareholding.period_end))),
        technicals=session.scalar(select(func.max(TechnicalSnapshot.as_of))),
        reports=session.scalar(select(func.max(Report.as_of))),
    )
    gaps = session.scalar(
        select(func.count()).select_from(DataGap).where(DataGap.resolved_at.is_(None))
    )
    return JobsView(
        runs=runs, freshness=freshness, open_data_gaps=gaps or 0, refresh_queue=queued(redis)
    )
