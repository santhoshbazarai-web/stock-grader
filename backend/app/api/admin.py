"""Screener and XBRL uploads, the results filings ledger, YAML config view/edit, backtest
requests, job history (SPEC §8)."""

import os
import shutil
import tempfile
from datetime import UTC, datetime
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
    BacktestSummary,
    ConfigFile,
    ConfigSaved,
    ConfigUpdate,
    ConfigView,
    FilingsSummary,
    Freshness,
    JobRunOut,
    JobsView,
    ResultFilingOut,
    UploadedDataset,
    UploadSummary,
    XbrlFileResult,
    XbrlUploadSummary,
)
from app.core.config import ConfigError, get_config, load_config
from app.data.gaps import SessionGapRecorder
from app.data.providers.screener_import import ScreenerFormatError, import_screener
from app.data.raw_store import RawStore, RawStoreError
from app.data.results_ingest import cache_raw, ensure_uploaded_row, ingest
from app.db.enums import BacktestStatus, FilingStatus, StatementType
from app.db.models import (
    Backtest,
    DataGap,
    DeliveryDaily,
    FinAnnual,
    FinQuarterly,
    Instrument,
    JobRun,
    PriceDaily,
    Report,
    ResultFiling,
    Shareholding,
    TechnicalSnapshot,
)
from app.jobs.common import ensure_instruments
from app.jobs.refresh import queued

router = APIRouter()

XBRL_UPLOAD_MAX_FILES = 80  # 20 years of quarterly filings in one request

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


@router.post(
    "/uploads/xbrl",
    tags=["uploads"],
    status_code=status.HTTP_201_CREATED,
    responses={
        413: {"description": "File too large"},
        422: {"description": "Not XML"},
        503: {"description": "Raw-file cache not writable; nothing parsed"},
    },
)
def upload_xbrl(
    session: SessionDep,
    settings: SettingsDep,
    config: ConfigDep,
    files: Annotated[
        list[UploadFile],
        File(description="Results XBRL documents (.xml) from NSE or BSE, one per filing"),
    ],
    symbol: Annotated[str, Form(pattern=SYMBOL_PATTERN)],
) -> XbrlUploadSummary:
    """Import exchange results filings by hand, for filings the ``results_watch`` job cannot
    download (e.g. BSE-only companies, or while NSE is unreachable). Each document is parsed
    like a downloaded one; its basis (standalone / consolidated) and period come from the
    document. With no dissemination time, the announcement date is the board-meeting date +
    1 day. A document naming another company is refused. Each file is reported separately."""
    if not 1 <= len(files) <= XBRL_UPLOAD_MAX_FILES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"send 1 to {XBRL_UPLOAD_MAX_FILES} files"
        )
    sym = symbol.upper()
    iid = ensure_instruments(session, [sym])[sym]
    store = RawStore(settings.raw_data_dir)
    results = []
    for f in files:
        name = f.filename or "upload.xml"
        if not name.lower().endswith(".xml"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: expected .xml")
        content = f.file.read(settings.upload_max_bytes + 1)
        if len(content) > settings.upload_max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{name}: larger than {settings.upload_max_bytes} bytes",
            )
        row = ensure_uploaded_row(session, instrument_id=iid, content=content)
        try:  # cache the raw file before parsing it (SPEC §3.2a)
            row.raw_path = cache_raw(store, "upload", f"{sym}_{row.document[7:23]}.xml", content)
        except RawStoreError as exc:
            session.rollback()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        ingest(session, row, symbol=sym, content=content, cfg=config.providers.nse.results,
               gaps=SessionGapRecorder(session), now=datetime.now(UTC))  # fmt: skip
        session.commit()
        results.append(
            XbrlFileResult(
                filename=name,
                status=row.status,
                periods=row.periods or [],
                statement_type=row.statement_type.value if row.statement_type else None,
                announcement_date=row.announcement_date,
                warnings=row.warnings or [],
                error=row.error,
            )
        )
    return XbrlUploadSummary(symbol=sym, files=results)


@router.get("/filings", tags=["uploads"])
def list_filings(
    session: SessionDep,
    symbol: Annotated[str | None, Query(pattern=SYMBOL_PATTERN)] = None,
    status_: Annotated[FilingStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ResultFilingOut]:
    """The exchange results filings ledger (``results_watch`` and XBRL uploads), most
    recently updated first."""
    q = (
        select(ResultFiling, Instrument.symbol)
        .join(Instrument, Instrument.id == ResultFiling.instrument_id)
        .order_by(ResultFiling.updated_at.desc(), ResultFiling.id.desc())
        .limit(limit)
    )
    if symbol:
        q = q.where(Instrument.symbol == symbol.upper())
    if status_:
        q = q.where(ResultFiling.status == status_)
    return [_filing_out(r, sym) for r, sym in session.execute(q).all()]


def _filing_out(r: ResultFiling, symbol: str) -> ResultFilingOut:
    return ResultFilingOut(
        id=r.id,
        symbol=symbol,
        exchange=r.exchange,
        document=r.document,
        period_start=r.period_start,
        period_end=r.period_end,
        statement_type=r.statement_type.value if r.statement_type else None,
        audited=r.audited,
        is_bank=r.is_bank,
        disseminated_at=r.disseminated_at,
        announcement_date=r.announcement_date,
        status=r.status,
        attempts=r.attempts,
        error=r.error,
        periods=r.periods,
        warnings=r.warnings,
        parsed_at=r.parsed_at,
        updated_at=r.updated_at,
    )


@router.get("/filings/summary", tags=["uploads"])
def filings_summary(session: SessionDep) -> FilingsSummary:
    counts = dict(
        session.execute(
            select(ResultFiling.status, func.count()).group_by(ResultFiling.status)
        ).all()
    )
    return FilingsSummary(
        pending=counts.get(FilingStatus.PENDING, 0),
        parsed=counts.get(FilingStatus.PARSED, 0),
        failed=counts.get(FilingStatus.FAILED, 0),
        symbols=session.scalar(
            select(func.count(func.distinct(ResultFiling.instrument_id))).where(
                ResultFiling.status == FilingStatus.PARSED
            )
        )
        or 0,
        last_parsed_at=session.scalar(select(func.max(ResultFiling.parsed_at))),
    )


@router.post(
    "/filings/{filing_id}/retry",
    tags=["uploads"],
    responses={404: {"description": "Not found"}, 409: {"description": "Uploaded file"}},
)
def retry_filing(filing_id: int, session: SessionDep) -> ResultFilingOut:
    """Put a failed exchange filing back in the queue: ``results_watch`` downloads it again on
    its next run (attempts restart at 0). Uploaded files cannot be re-downloaded: upload again."""
    row = session.get(ResultFiling, filing_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no filing {filing_id}")
    if row.exchange == "upload":
        raise HTTPException(status.HTTP_409_CONFLICT, "uploaded file: upload it again instead")
    row.status, row.attempts, row.error = FilingStatus.PENDING, 0, None
    session.commit()
    session.refresh(row)
    symbol = session.scalar(select(Instrument.symbol).where(Instrument.id == row.instrument_id))
    return _filing_out(row, symbol or "")


@router.get("/uploads/screener", tags=["uploads"])
def list_uploads(session: SessionDep) -> list[UploadedDataset]:
    """Stocks with uploaded Screener fundamentals, newest upload first."""
    annual = (
        select(
            FinAnnual.instrument_id,
            FinAnnual.statement_type,
            func.count().label("years"),
            func.min(FinAnnual.fiscal_year).label("first"),
            func.max(FinAnnual.fiscal_year).label("last"),
            func.max(FinAnnual.fetched_at).label("uploaded"),
        )
        .where(FinAnnual.source == "screener")
        .group_by(FinAnnual.instrument_id, FinAnnual.statement_type)
        .subquery()
    )
    quarters = (
        select(
            FinQuarterly.instrument_id,
            FinQuarterly.statement_type,
            func.count().label("quarters"),
        )
        .where(FinQuarterly.source == "screener")
        .group_by(FinQuarterly.instrument_id, FinQuarterly.statement_type)
        .subquery()
    )
    rows = session.execute(
        select(
            Instrument.symbol,
            Instrument.name,
            annual.c.statement_type,
            annual.c.years,
            annual.c.first,
            annual.c.last,
            func.coalesce(quarters.c.quarters, 0),
            annual.c.uploaded,
        )
        .join(annual, annual.c.instrument_id == Instrument.id)
        .outerjoin(
            quarters,
            (quarters.c.instrument_id == annual.c.instrument_id)
            & (quarters.c.statement_type == annual.c.statement_type),
        )
        .order_by(annual.c.uploaded.desc(), Instrument.symbol)
    ).all()
    return [
        UploadedDataset(
            symbol=sym,
            name=name,
            statement_type=str(basis),  # type: ignore[arg-type]
            annual_years=years,
            first_fiscal_year=first,
            last_fiscal_year=last,
            quarters=q,
            uploaded_at=uploaded,
        )
        for sym, name, basis, years, first, last, q, uploaded in rows
    ]


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
    responses={
        422: {"description": "YAML invalid or fails validation; nothing saved"},
        503: {"description": "Config directory not writable; nothing saved"},
    },
)
def update_config(body: ConfigUpdate, settings: SettingsDep, dry_run: bool = False) -> ConfigSaved:
    """Replace one config file. The new YAML is validated together with the other files
    exactly as at startup; only a fully valid config is written (atomically). The API uses it
    immediately; the worker picks it up on restart. ``dry_run`` validates without saving."""
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
            # The message names the scratch directory; point at the edited file instead.
            detail = str(exc).replace(f"invalid config in {tmp}:", f"{body.name}.yaml is invalid:")
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail.replace(tmp, "config")
            ) from exc
    if dry_run:
        return ConfigSaved(name=body.name, saved=False, note="valid (not saved: dry run)")
    target = live / f"{body.name}.yaml"
    try:
        fd, staged = tempfile.mkstemp(dir=live, prefix=f".{body.name}.", suffix=".yaml")
        with os.fdopen(fd, "w") as fh:
            fh.write(body.yaml)
        # mkstemp creates 0600; keep the file's existing permissions (git, the worker, backups).
        os.chmod(staged, target.stat().st_mode & 0o777 if target.exists() else 0o644)
        os.replace(staged, target)
    except OSError as exc:  # e.g. a read-only mount, or the directory owned by another user
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"config directory is not writable by the API ({exc.strerror}); nothing saved",
        ) from exc
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
    in the worker (``backtests`` job, within a minute); poll ``GET /api/backtests/{id}`` for
    progress and results."""
    b = Backtest(
        status=BacktestStatus.QUEUED, params=body.model_dump(mode="json", exclude_none=True)
    )
    session.add(b)
    session.commit()
    session.refresh(b)
    return _backtest_out(b)


@router.get("/backtests", tags=["backtests"])
def list_backtests(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[BacktestSummary]:
    """Recent backtests, newest first, without the full results."""
    rows = session.scalars(select(Backtest).order_by(Backtest.id.desc()).limit(limit))
    out = []
    for b in rows:
        r = b.results or {}
        out.append(
            BacktestSummary(
                id=b.id,
                status=b.status,
                params=b.params,
                created_at=b.created_at,
                updated_at=b.updated_at,
                progress=r.get("progress"),
                trades=(r.get("portfolio") or {}).get("trades"),
                cagr=(r.get("portfolio") or {}).get("cagr"),
                benchmark_cagr=(r.get("benchmark") or {}).get("cagr"),
                error=b.error,
            )
        )
    return out


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
