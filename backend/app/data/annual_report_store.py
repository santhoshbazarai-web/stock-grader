"""Annual-report PDF values → the review queue (pdf_line_candidates) → fin_line_items (SPEC
v0.2 §3.6 step 3).

- Every value read becomes a candidate. Confidence at or above
  ``nse.annual_reports.confidence.auto_accept`` (and a stated unit) → ``auto_accepted``;
  otherwise ``pending`` in the review queue.
- Accepted values (auto, accepted, corrected) are stored as line items with
  ``source=annual_report_pdf``, the report, its ``usable_from`` (rule 4) and the confidence
  (1.0 once the owner accepted or corrected it). They only fill gaps: a key the exchange XBRL
  already has is left alone (the candidate says so), and an XBRL figure arriving later
  removes them (see ``results_store._apply_versions``).
- Versions work as for XBRL: the same period read from two reports (this year's column and
  next year's comparative) is one version if equal, else a restatement.
- The wide fin_annual row is updated when it exists (it needs the year's P&L to be created);
  its ``source`` is kept.

Re-reading a report replaces its unreviewed candidates; the owner's decisions are kept.
"""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.core.config import NseAnnualReportsConfig, NseConfig, NseResultsConfig
from app.data.annual_report import (
    AnnualReportError,
    AnnualReportExtraction,
    extract_annual_report,
)
from app.data.results_store import (
    PDF_SOURCE,
    VENDOR_SOURCE,
    _apply_versions,
    _as_dict,
    _fy_end_month,
    _quarter_ends,
    rebuild_wide,
)
from app.data.xbrl import announcement_date
from app.db.enums import FilingStatus, LineStatement, PeriodType, ReviewStatus, StatementType
from app.db.models import AnnualReport, FinLineItem, Instrument, PdfLineCandidate
from app.db.upsert import upsert
from app.fundamentals.pdf_labels import PdfLabels
from app.fundamentals.xbrl_map import XbrlMap

CR = 1e7  # rupees per crore: the review queue shows and takes ₹ crore
STORED_STATUSES = (ReviewStatus.AUTO_ACCEPTED, ReviewStatus.ACCEPTED, ReviewStatus.CORRECTED)
DECIDED = (ReviewStatus.ACCEPTED, ReviewStatus.CORRECTED, ReviewStatus.REJECTED)

Key = tuple[Any, ...]  # (period_end, period_type, statement, item_code)


def best_per_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One candidate per conflict key (basis, statement, period end, item): a report can show
    the same line twice (e.g. the balance sheet and a schedule), and Postgres refuses an
    INSERT ... ON CONFLICT that touches one row twice (CardinalityViolation). The highest
    confidence wins; at equal confidence, a value beats none, then the first one read."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        key = (r["basis"], r["statement"], r["period_end"], r["item_code"])
        cur = best.get(key)
        rank = (r["confidence"], r["value_inr"] is not None)
        if cur is None or rank > (cur["confidence"], cur["value_inr"] is not None):
            best[key] = r
    return list(best.values())


def save_candidates(
    session: Session,
    report: AnnualReport,
    extraction: AnnualReportExtraction,
    cfg: NseAnnualReportsConfig,
) -> dict[str, int]:
    """Upsert the report's candidates; drop its unreviewed ones no longer read. Returns counts
    per status of the candidates written."""
    decided = {
        (c.basis, c.statement, c.period_end, c.item_code)
        for c in session.scalars(
            select(PdfLineCandidate).where(
                PdfLineCandidate.annual_report_id == report.id,
                PdfLineCandidate.status.in_(DECIDED),
            )
        )
    }
    rows = []
    for v in extraction.values:
        key = (StatementType(v.basis), LineStatement(v.statement), v.period_end, v.item_code)
        if key in decided:
            continue
        auto = v.value_inr is not None and v.confidence >= cfg.confidence.auto_accept
        rows.append({
            "annual_report_id": report.id, "instrument_id": report.instrument_id,
            "basis": key[0], "statement": key[1], "period_end": v.period_end,
            "period_type": PeriodType(v.period_type), "item_code": v.item_code,
            "value_inr": v.value_inr, "raw_value": v.raw_value, "raw_label": v.raw_label[:512],
            "pages": v.pages, "method": v.method, "confidence": v.confidence,
            "reasons": v.reasons,
            "status": ReviewStatus.AUTO_ACCEPTED if auto else ReviewStatus.PENDING,
            "corrected_value_inr": None, "stored": False, "note": None, "reviewed_at": None,
        })  # fmt: skip
    rows = best_per_key(rows)
    keep = {(r["basis"], r["statement"], r["period_end"], r["item_code"]) for r in rows}
    for c in session.scalars(
        select(PdfLineCandidate).where(
            PdfLineCandidate.annual_report_id == report.id,
            PdfLineCandidate.status.not_in(DECIDED),
        )
    ).all():
        if (c.basis, c.statement, c.period_end, c.item_code) not in keep:
            session.delete(c)
    session.flush()
    if rows:
        upsert(session, PdfLineCandidate, rows)
    counts: dict[str, int] = {}
    for r in rows:
        status = str(r["status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def _filed_keys(
    session: Session, instrument_id: int, basis: StatementType, keys: Iterable[Key]
) -> set[Key]:
    """Keys the exchange filings (XBRL, not derived) already cover."""
    keys = set(keys)
    ends = {k[0] for k in keys}
    if not ends:
        return set()
    rows = session.execute(
        select(FinLineItem.period_end, FinLineItem.period_type, FinLineItem.statement,
               FinLineItem.item_code)
        .where(
            FinLineItem.instrument_id == instrument_id,
            FinLineItem.basis == basis,
            FinLineItem.period_end.in_(ends),
            FinLineItem.source.not_in((PDF_SOURCE, VENDOR_SOURCE)),  # PDF beats vendor figures
            FinLineItem.derived.is_(False),
        )
    ).all()  # fmt: skip
    return {tuple(r) for r in rows} & keys


def _remove(
    session: Session, instrument_id: int, basis: StatementType, report_id: int, keys: set[Key]
) -> None:
    """Remove the report's figures for ``keys``; the other versions are renumbered."""
    for period_end, period_type, statement, code in keys:
        rows = session.scalars(
            select(FinLineItem)
            .where(
                FinLineItem.instrument_id == instrument_id,
                FinLineItem.basis == basis,
                FinLineItem.period_end == period_end,
                FinLineItem.period_type == period_type,
                FinLineItem.statement == statement,
                FinLineItem.item_code == code,
            )
            .order_by(FinLineItem.version)
        ).all()
        others = [_as_dict(r) for r in rows if r.annual_report_id != report_id]
        if len(others) == len(rows):
            continue
        session.execute(delete(FinLineItem).where(FinLineItem.id.in_([r.id for r in rows])))
        for version, r in enumerate(others, start=1):
            r["version"] = version
        if others:
            session.execute(insert(FinLineItem), others)


def sync_report(
    session: Session,
    report: AnnualReport,
    *,
    results_cfg: NseResultsConfig,
    xmap: XbrlMap,
    labels_version: int,
    now: datetime,
) -> list[str]:
    """Make fin_line_items hold exactly the report's accepted values (where the exchange has
    none), then rebuild the wide rows of the periods touched. Returns those periods."""
    isin = session.scalar(select(Instrument.isin).where(Instrument.id == report.instrument_id))
    candidates = session.scalars(
        select(PdfLineCandidate).where(PdfLineCandidate.annual_report_id == report.id)
    ).all()
    stored_now = {
        (r.basis, (r.period_end, r.period_type, r.statement, r.item_code))
        for r in session.scalars(
            select(FinLineItem).where(FinLineItem.annual_report_id == report.id)
        )
    }
    written: list[str] = []
    for basis in (StatementType.CONSOLIDATED, StatementType.STANDALONE):
        mine = [c for c in candidates if c.basis == basis]
        wanted = {
            (c.period_end, c.period_type, c.statement, c.item_code): c
            for c in mine
            if c.status in STORED_STATUSES and _value(c) is not None
        }
        filed = _filed_keys(session, report.instrument_id, basis, wanted)
        observations: dict[Key, dict[str, Any]] = {}
        for key, c in wanted.items():
            if key in filed:
                continue
            reviewed = c.status in (ReviewStatus.ACCEPTED, ReviewStatus.CORRECTED)
            observations[key] = {
                "instrument_id": report.instrument_id, "isin": isin, "period_end": key[0],
                "period_type": key[1], "statement": key[2], "basis": basis, "item_code": key[3],
                "value_inr": _value(c), "unit": "amount", "source": PDF_SOURCE,
                "filing_id": None, "announced_at": report.disseminated_at,
                "usable_from": report.usable_from, "derived": False,
                "tag": f"pdf p.{','.join(str(p) for p in c.pages)}: {c.raw_label}"[:512],
                "map_version": labels_version, "annual_report_id": report.id,
                "confidence": 1.0 if reviewed else c.confidence, "vendor_reclassified": False,
            }  # fmt: skip
        stale = {k for b, k in stored_now if b == basis} - set(observations)
        _remove(session, report.instrument_id, basis, report.id, stale)
        touched = _apply_versions(session, report.instrument_id, basis, observations,
                                  results_cfg)  # fmt: skip
        touched |= {(k[0], k[1]) for k in stale}
        for c in mine:
            key = (c.period_end, c.period_type, c.statement, c.item_code)
            c.stored = key in observations
            c.note = (
                "not stored: the exchange XBRL already has this figure" if key in filed
                else None if c.stored or c.status not in STORED_STATUSES
                else "not stored: no value (the page states no unit)"
            )  # fmt: skip
        clear = frozenset(
            col
            for _, _, _, code in stale
            for col in (code, *(("book_value_per_share",) if code == "total_equity" else ()))
            if xmap.items[code].target == "canonical" or col == "book_value_per_share"
        )
        written += rebuild_wide(
            session, instrument_id=report.instrument_id, basis=basis, touched=touched,
            xmap=xmap, fetched_at=now, source=None, clear=clear,
            fy_end_month=_fy_end_month(session, report.instrument_id, results_cfg),
        )  # fmt: skip
    return written


def _value(c: PdfLineCandidate) -> float | None:
    return c.corrected_value_inr if c.status == ReviewStatus.CORRECTED else c.value_inr


def review(
    session: Session,
    candidate: PdfLineCandidate,
    *,
    action: str,
    value_cr: float | None,
    item_code: str | None,
    results_cfg: NseResultsConfig,
    xmap: XbrlMap,
    labels_version: int,
    now: datetime,
) -> None:
    """The owner's decision on one candidate: ``accept`` (as read), ``correct`` (with
    ``value_cr`` in ₹ crore, and optionally another ``item_code`` of the same statement) or
    ``reject``. Then the report's line items are synced. Raises ValueError on a bad request."""
    if action == "accept":
        if candidate.value_inr is None:
            raise ValueError("no value was read (the page states no unit): correct it instead")
        candidate.status = ReviewStatus.ACCEPTED
    elif action == "correct":
        if value_cr is None:
            raise ValueError("a corrected value (₹ crore) is required")
        if item_code is not None and item_code != candidate.item_code:
            spec = xmap.items.get(item_code)
            if spec is None or spec.statement != candidate.statement.value \
                    or spec.unit != "amount":  # fmt: skip
                raise ValueError(f"{item_code} is not a {candidate.statement.value} amount item")
            clash = session.scalar(
                select(PdfLineCandidate.id).where(
                    PdfLineCandidate.annual_report_id == candidate.annual_report_id,
                    PdfLineCandidate.basis == candidate.basis,
                    PdfLineCandidate.statement == candidate.statement,
                    PdfLineCandidate.period_end == candidate.period_end,
                    PdfLineCandidate.item_code == item_code,
                )
            )
            if clash is not None:
                raise ValueError(f"this report already has a {item_code} value for "
                                 f"{candidate.period_end}; correct that one")  # fmt: skip
            candidate.item_code = item_code
        value = value_cr * CR
        if xmap.items[candidate.item_code].magnitude:
            value = abs(value)
        candidate.status = ReviewStatus.CORRECTED
        candidate.corrected_value_inr = value
    elif action == "reject":
        candidate.status = ReviewStatus.REJECTED
    else:
        raise ValueError(f"unknown action {action!r}")
    candidate.reviewed_at = now
    session.flush()
    report = session.get(AnnualReport, candidate.annual_report_id)
    assert report is not None
    sync_report(session, report, results_cfg=results_cfg, xmap=xmap,
                labels_version=labels_version, now=now)  # fmt: skip


def fiscal_year_end(
    session: Session, instrument_id: int, fiscal_year: int, cfg: NseResultsConfig
) -> date:
    """The last day of fiscal year ``fiscal_year`` (ending in that calendar year) for the
    company's filed year-end month (else ``default_fy_end_month``)."""
    month = _fy_end_month(session, instrument_id, cfg)
    return _quarter_ends(date(fiscal_year, month, 1))[0]


def ingest_report(
    session: Session,
    report: AnnualReport,
    *,
    content: bytes,
    nse: NseConfig,
    labels: PdfLabels,
    xmap: XbrlMap,
    now: datetime,
) -> None:
    """Read ``content`` (the PDF) into the review queue and fin_line_items; the ledger row
    records the outcome either way (the caller commits). ``report.usable_from`` must be set by
    the caller when the exchange gave no dissemination time (an upload)."""
    report.attempts += 1
    fy_end = (
        fiscal_year_end(session, report.instrument_id, report.fiscal_year, nse.results)
        if report.fiscal_year else None
    )  # fmt: skip
    try:
        out = extract_annual_report(
            content, cfg=nse.annual_reports, rounding_levels=nse.results.rounding_levels,
            labels=labels, xmap=xmap, fiscal_year_end=fy_end,
        )  # fmt: skip
    except AnnualReportError as exc:
        report.status, report.error = FilingStatus.FAILED, str(exc)[:2000]
        return
    report.page_count = out.page_count
    report.labels_version = out.labels_version
    report.warnings = out.warnings or None
    report.statements = [
        {"statement": s.statement, "basis": s.basis, "pages": s.pages, "method": s.method,
         "unit": s.unit_text, "checks": s.checks,
         "column_dates": [d.isoformat() if d else None for d in s.column_dates]}
        for s in out.statements
    ]  # fmt: skip
    if not out.values:
        report.status = FilingStatus.FAILED
        report.error = (
            "no balance sheet or cash flow values read: "
            + ("; ".join(out.warnings) or "no statement pages found")[:1900]
        )
        return
    if report.disseminated_at is not None:
        report.usable_from = announcement_date(report.disseminated_at, None,
                                               nse.results.available_after_ist)  # fmt: skip
    save_candidates(session, report, out, nse.annual_reports)
    sync_report(session, report, results_cfg=nse.results, xmap=xmap,
                labels_version=out.labels_version, now=now)  # fmt: skip
    report.status, report.error, report.parsed_at = FilingStatus.PARSED, None, now
