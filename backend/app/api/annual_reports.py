"""Annual-report PDFs (SPEC v0.2 §3.6 step 3): upload, the ledger, the review queue; and the
coverage grid of the stock page (§3.6 step 4)."""

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, SessionDep, SettingsDep
from app.api.schemas import (
    SYMBOL_PATTERN,
    AnnualReportOut,
    CoverageBasis,
    CoverageCellOut,
    CoverageGridOut,
    PdfCandidateOut,
    ReviewRequest,
    ReviewSummary,
)
from app.core.config import SectorModel
from app.data.annual_report import AnnualReportError, unpack
from app.data.annual_report_store import CR, ingest_report, review
from app.data.coverage import coverage_grid, files_as_bank
from app.data.raw_store import RawStore, RawStoreError
from app.data.results_ingest import cache_raw, upload_document_id
from app.data.results_store import _fy_end_month
from app.db.enums import FilingStatus, ReviewStatus
from app.db.models import AnnualReport, FinAnnual, Instrument, PdfLineCandidate
from app.fundamentals.pdf_labels import get_pdf_labels
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.common import ensure_instruments

router = APIRouter()


def _counts(session: Session, report_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in report_ids}
    for rid, st, n in session.execute(
        select(PdfLineCandidate.annual_report_id, PdfLineCandidate.status, func.count())
        .where(PdfLineCandidate.annual_report_id.in_(report_ids))
        .group_by(PdfLineCandidate.annual_report_id, PdfLineCandidate.status)
    ).all():
        out[rid][st.value] = n
    return out


def _report_out(r: AnnualReport, symbol: str, counts: dict[str, int]) -> AnnualReportOut:
    return AnnualReportOut(
        id=r.id, symbol=symbol, exchange=r.exchange, document=r.document,
        fiscal_year=r.fiscal_year, disseminated_at=r.disseminated_at, usable_from=r.usable_from,
        status=r.status, attempts=r.attempts, error=r.error, page_count=r.page_count,
        statements=r.statements, warnings=r.warnings, has_document=r.raw_path is not None,
        candidates=counts, parsed_at=r.parsed_at, updated_at=r.updated_at,
    )  # fmt: skip


def _one_report_out(session: Session, r: AnnualReport) -> AnnualReportOut:
    symbol = session.scalar(select(Instrument.symbol).where(Instrument.id == r.instrument_id))
    return _report_out(r, symbol or "", _counts(session, [r.id])[r.id])


def _candidate_out(c: PdfLineCandidate, symbol: str, fiscal_year: int | None) -> PdfCandidateOut:
    return PdfCandidateOut(
        id=c.id, symbol=symbol, annual_report_id=c.annual_report_id, fiscal_year=fiscal_year,
        statement=c.statement.value, basis=c.basis.value,  # type: ignore[arg-type]
        period_end=c.period_end, item_code=c.item_code,
        value_cr=None if c.value_inr is None else c.value_inr / CR,
        corrected_value_cr=None if c.corrected_value_inr is None else c.corrected_value_inr / CR,
        raw_value=c.raw_value, raw_label=c.raw_label, pages=[int(p) for p in c.pages],
        method=c.method, confidence=c.confidence, reasons=list(c.reasons), status=c.status,
        stored=c.stored, note=c.note, reviewed_at=c.reviewed_at,
    )  # fmt: skip


# ───────────── upload and ledger ─────────────


@router.post(
    "/uploads/annual-report",
    tags=["uploads"],
    status_code=status.HTTP_201_CREATED,
    responses={
        413: {"description": "File too large"},
        422: {"description": "Not a PDF or ZIP"},
        503: {"description": "Raw-file cache not writable; nothing read"},
    },
)
def upload_annual_report(
    session: SessionDep,
    settings: SettingsDep,
    config: ConfigDep,
    file: Annotated[UploadFile, File(description="Annual report (.pdf, or the exchange's .zip)")],
    symbol: Annotated[str, Form(pattern=SYMBOL_PATTERN)],
    fiscal_year: Annotated[int, Form(ge=1990, le=2100, description="FY ending in this year")],
    published_on: Annotated[
        date | None,
        Form(
            description="When the report became public (rule 4); without it, backtests "
            "ignore its values"
        ),
    ] = None,
) -> AnnualReportOut:
    """Read an annual report by hand (e.g. from BSE, or while NSE is unreachable). Values go to
    the review queue; confident ones straight into the fundamentals, where the exchange XBRL
    has no figure."""
    cfg = config.providers.nse
    name = file.filename or "report.pdf"
    if not name.lower().endswith((".pdf", ".zip")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: expected .pdf/.zip")
    cap = cfg.annual_reports.max_bytes
    content = file.file.read(cap + 1)
    if len(content) > cap:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"{name}: larger than {cap} bytes")
    if not content.startswith((b"%PDF", b"PK")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: not a PDF or ZIP")
    sym = symbol.upper()
    iid = ensure_instruments(session, [sym])[sym]
    document = upload_document_id(content)
    report = session.scalar(
        select(AnnualReport).where(AnnualReport.instrument_id == iid,
                                   AnnualReport.document == document)
    )  # fmt: skip
    if report is None:
        report = AnnualReport(instrument_id=iid, exchange="upload", document=document,
                              status=FilingStatus.PENDING)  # fmt: skip
        session.add(report)
    report.fiscal_year = fiscal_year
    report.usable_from = published_on
    try:  # cache the raw file before reading it (SPEC §3.2a)
        ext = ".zip" if content.startswith(b"PK") else ".pdf"
        report.raw_path = cache_raw(RawStore(settings.raw_data_dir), "upload",
                                    f"{sym}_AR_FY{fiscal_year}{ext}", content)  # fmt: skip
    except RawStoreError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    session.flush()
    ingest_report(session, report, content=content, nse=cfg, labels=get_pdf_labels(),
                  xmap=get_xbrl_map(), now=datetime.now(UTC))  # fmt: skip
    if published_on is None and report.status is FilingStatus.PARSED:
        report.warnings = [*(report.warnings or []),
                           "no publication date: backtests ignore these values"]  # fmt: skip
    session.commit()
    return _one_report_out(session, report)


@router.get("/annual-reports", tags=["uploads"])
def list_annual_reports(
    session: SessionDep,
    symbol: Annotated[str | None, Query(pattern=SYMBOL_PATTERN)] = None,
    status_: Annotated[FilingStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AnnualReportOut]:
    """The annual-report ledger (the ``annual_reports`` job and uploads), newest first."""
    q = (
        select(AnnualReport, Instrument.symbol)
        .join(Instrument, Instrument.id == AnnualReport.instrument_id)
        .order_by(AnnualReport.updated_at.desc(), AnnualReport.id.desc())
        .limit(limit)
    )
    if symbol:
        q = q.where(Instrument.symbol == symbol.upper())
    if status_:
        q = q.where(AnnualReport.status == status_)
    rows = session.execute(q).all()
    counts = _counts(session, [r.id for r, _ in rows])
    return [_report_out(r, sym, counts[r.id]) for r, sym in rows]


def _report(session: Session, report_id: int) -> AnnualReport:
    report = session.get(AnnualReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no annual report {report_id}")
    return report


def _cached(settings: Any, report: AnnualReport) -> bytes:
    if report.raw_path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "the report is not cached")
    try:
        return RawStore(settings.raw_data_dir).read(report.raw_path)
    except (RawStoreError, OSError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cached file unreadable: {exc}") from exc


@router.post("/annual-reports/{report_id}/reparse", tags=["uploads"],
             responses={404: {"description": "Unknown report or not cached"}})  # fmt: skip
def reparse_annual_report(
    session: SessionDep, settings: SettingsDep, config: ConfigDep, report_id: int
) -> AnnualReportOut:
    """Read the cached report again (after a pdf_labels.yaml change). The owner's review
    decisions are kept."""
    report = _report(session, report_id)
    content = _cached(settings, report)
    ingest_report(session, report, content=content, nse=config.providers.nse,
                  labels=get_pdf_labels(), xmap=get_xbrl_map(), now=datetime.now(UTC))  # fmt: skip
    session.commit()
    return _one_report_out(session, report)


@router.get(
    "/annual-reports/{report_id}/document",
    tags=["uploads"],
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}},
               404: {"description": "Unknown report or not cached"}},
)  # fmt: skip
def annual_report_document(
    session: SessionDep, settings: SettingsDep, config: ConfigDep, report_id: int
) -> Response:
    """The cached PDF (unpacked from a ZIP), to check a value on its page (``#page=N``)."""
    report = _report(session, report_id)
    try:
        pdf = unpack(_cached(settings, report), config.providers.nse.annual_reports.max_bytes)
    except AnnualReportError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="report-{report_id}.pdf"',
                             "Cache-Control": "private, max-age=3600"})  # fmt: skip


# ───────────── review queue ─────────────


@router.get("/review/annual-reports", tags=["review"])
def review_queue(
    session: SessionDep,
    symbol: Annotated[str | None, Query(pattern=SYMBOL_PATTERN)] = None,
    status_: Annotated[ReviewStatus | None, Query(alias="status")] = ReviewStatus.PENDING,
    report_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[PdfCandidateOut]:
    """Values read from annual reports; by default the low-confidence ones waiting for review,
    lowest confidence first."""
    q = (
        select(PdfLineCandidate, Instrument.symbol, AnnualReport.fiscal_year)
        .join(Instrument, Instrument.id == PdfLineCandidate.instrument_id)
        .join(AnnualReport, AnnualReport.id == PdfLineCandidate.annual_report_id)
        .order_by(PdfLineCandidate.confidence, Instrument.symbol,
                  PdfLineCandidate.period_end.desc(), PdfLineCandidate.id)
        .limit(limit)
    )  # fmt: skip
    if symbol:
        q = q.where(Instrument.symbol == symbol.upper())
    if status_:
        q = q.where(PdfLineCandidate.status == status_)
    if report_id is not None:
        q = q.where(PdfLineCandidate.annual_report_id == report_id)
    return [_candidate_out(c, sym, fy) for c, sym, fy in session.execute(q).all()]


@router.get("/review/annual-reports/summary", tags=["review"])
def review_summary(session: SessionDep) -> ReviewSummary:
    pending = session.scalar(
        select(func.count()).select_from(PdfLineCandidate)
        .where(PdfLineCandidate.status == ReviewStatus.PENDING)
    )  # fmt: skip
    by_status = dict(
        session.execute(
            select(AnnualReport.status, func.count()).group_by(AnnualReport.status)
        ).all()
    )
    return ReviewSummary(pending=pending or 0,
                         reports_parsed=by_status.get(FilingStatus.PARSED, 0),
                         reports_failed=by_status.get(FilingStatus.FAILED, 0))  # fmt: skip


@router.post("/review/annual-reports/{candidate_id}", tags=["review"],
             responses={404: {"description": "Unknown value"},
                        422: {"description": "Invalid decision"}})  # fmt: skip
def review_value(
    session: SessionDep, config: ConfigDep, candidate_id: int, body: ReviewRequest
) -> PdfCandidateOut:
    """Accept a value as read, correct it (₹ crore; optionally to another item of the same
    statement), or reject it. The fundamentals are updated at once."""
    c = session.get(PdfLineCandidate, candidate_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no value {candidate_id}")
    try:
        review(session, c, action=body.action, value_cr=body.value_cr, item_code=body.item_code,
               results_cfg=config.providers.nse.results, xmap=get_xbrl_map(),
               labels_version=get_pdf_labels().version, now=datetime.now(UTC))  # fmt: skip
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    row = session.execute(
        select(Instrument.symbol, AnnualReport.fiscal_year)
        .join(AnnualReport, AnnualReport.instrument_id == Instrument.id)
        .where(AnnualReport.id == c.annual_report_id)
    ).one()
    return _candidate_out(c, row[0], row[1])


# ───────────── coverage grid ─────────────


@router.get("/stocks/{symbol}/coverage", tags=["stocks"],
            responses={404: {"description": "Unknown symbol"}})  # fmt: skip
def stock_coverage(session: SessionDep, config: ConfigDep, symbol: str) -> CoverageGridOut:
    """Fiscal years by statement (P&L / BS / CF) with the source of each cell: exchange XBRL,
    annual-report PDF, derived from quarters, or a Screener / yfinance row; empty = a gap
    (SPEC §3.6 step 4). The last ``providers.history_years`` completed fiscal years, or from
    the earliest stored year when that is older."""
    sym = symbol.upper()
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == sym))
    if iid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {sym}")
    month = _fy_end_month(session, iid, config.providers.nse.results)
    today = datetime.now(UTC).date()
    last = today.year if today.month > month else today.year - 1
    first = last - config.providers.history_years + 1
    stored = session.scalar(select(func.min(FinAnnual.fiscal_year))
                            .where(FinAnnual.instrument_id == iid))  # fmt: skip
    if isinstance(stored, int) and stored < first:  # e.g. 12 years from the Indian API
        first = stored
    years = list(range(first, last + 1))
    sector = session.scalar(select(Instrument.sector).where(Instrument.id == iid))
    model = config.sectors.for_sector(sector).model if sector in config.sectors.root else None
    bank = model in (SectorModel.BANK, SectorModel.INSURANCE) or files_as_bank(session, iid)
    cf_from = config.providers.nse.results.cash_flow_from_fy
    grids = coverage_grid(session, iid, years, month, bank=bank, cash_flow_from_fy=cf_from)
    return CoverageGridOut(
        symbol=sym, years=years, fy_end_month=month,
        bases=[CoverageBasis(
            basis=g.basis.value,
            cells=[CoverageCellOut(fiscal_year=c.fiscal_year, statement=c.statement,
                                   sources=c.sources, items=c.items,
                                   pending_review=c.pending_review, note=c.note)
                   for c in g.cells],
        ) for g in grids],
    )  # fmt: skip
