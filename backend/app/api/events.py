"""A stock's corporate events and cross-source reconciliation (SPEC v0.2 §3.8-3.9): the events
card and the reconciliation banner of the stock page."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import EventOut, IssueOut, ReconciliationOut, StockEventsOut
from app.db.enums import EventKind, IssueStatus
from app.db.models import Event, Instrument, ReconciliationIssue

router = APIRouter(tags=["stocks"])
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Unknown symbol or issue"}}


def _iid(session: Session, symbol: str) -> int:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return iid


def _event_out(e: Event) -> EventOut:
    return EventOut(id=e.id, exchange=e.exchange, kind=e.kind, category=e.category,
                    red_flag=e.red_flag, title=e.title, detail=e.detail, event_date=e.event_date,
                    disseminated_at=e.disseminated_at, url=e.url, data=e.data)  # fmt: skip


@router.get("/stocks/{symbol}/events", responses=_NOT_FOUND)
def stock_events(
    symbol: str,
    session: SessionDep,
    kinds: Annotated[list[EventKind] | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=3650)] = 365,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> StockEventsOut:
    """Announcements, results filings, board meetings, pledge / SAST / insider-trading
    disclosures and bulk / block deals linked to the stock, from the ``events`` and
    ``results_watch`` feeds."""
    iid = _iid(session, symbol)
    today = datetime.now(UTC).date()
    base = select(Event).where(Event.instrument_id == iid)
    if kinds:
        base = base.where(Event.kind.in_(kinds))
    upcoming = session.scalars(
        base.where(Event.kind == EventKind.BOARD_MEETING, Event.event_date >= today)
        .order_by(Event.event_date).limit(20)
    ).all()  # fmt: skip
    since = today - timedelta(days=days)
    rows = session.scalars(
        base.where(func.coalesce(Event.event_date, func.date(Event.disseminated_at)) >= since,
                   ~Event.id.in_([u.id for u in upcoming]))
        .order_by(Event.event_date.desc().nulls_last(), Event.disseminated_at.desc().nulls_last(),
                  Event.id.desc())
        .limit(limit)
    ).all()  # fmt: skip
    return StockEventsOut(symbol=symbol.upper(), upcoming=[_event_out(e) for e in upcoming],
                          events=[_event_out(e) for e in rows])  # fmt: skip


def _issue_out(i: ReconciliationIssue) -> IssueOut:
    cause = i.cause if i.cause in ("units", "basis", "restatement") else None
    return IssueOut(
        id=i.id, period_end=i.period_end, period_type=i.period_type.value, basis=i.basis.value,
        item_code=i.item_code, source=i.source, reference_source=i.reference_source,
        value_inr=i.value_inr, reference_value_inr=i.reference_value_inr, diff_rel=i.diff_rel,
        values={k: float(v) for k, v in (i.values or {}).items()}, cause=cause,  # type: ignore[arg-type]
        reasons=i.reasons, status=i.status, detected_at=i.detected_at, checked_at=i.checked_at,
        resolved_at=i.resolved_at,
    )  # fmt: skip


@router.get("/stocks/{symbol}/reconciliation", responses=_NOT_FOUND)
def stock_reconciliation(symbol: str, session: SessionDep, config: ConfigDep) -> ReconciliationOut:
    """Open differences between sources (they lower the valuation confidence and show the
    stock-page banner) and the latest resolved / ignored ones."""
    iid = _iid(session, symbol)
    rows = session.scalars(
        select(ReconciliationIssue).where(ReconciliationIssue.instrument_id == iid)
        .order_by(ReconciliationIssue.period_end.desc(), ReconciliationIssue.item_code)
    ).all()  # fmt: skip
    closed = sorted((i for i in rows if i.status is not IssueStatus.OPEN),
                    key=lambda i: i.resolved_at or i.updated_at, reverse=True)[:20]  # fmt: skip
    return ReconciliationOut(
        symbol=symbol.upper(), tolerance_rel=config.jobs.reconciliation.tolerance_rel,
        checked_at=max((i.checked_at for i in rows), default=None),
        open=[_issue_out(i) for i in rows if i.status is IssueStatus.OPEN],
        closed=[_issue_out(i) for i in closed],
    )  # fmt: skip


def _set_status(session: Session, symbol: str, issue_id: int, new: IssueStatus) -> IssueOut:
    iid = _iid(session, symbol)
    issue = session.get(ReconciliationIssue, issue_id)
    if issue is None or issue.instrument_id != iid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no issue {issue_id} for {symbol}")
    issue.status = new
    issue.resolved_at = datetime.now(UTC) if new is IssueStatus.IGNORED else None
    session.commit()
    return _issue_out(issue)


@router.post("/stocks/{symbol}/reconciliation/{issue_id}/ignore", responses=_NOT_FOUND)
def ignore_issue(symbol: str, issue_id: int, session: SessionDep) -> IssueOut:
    """Dismiss a difference you have explained. It stops lowering the confidence from the next
    report build, and stays ignored while the figures are unchanged."""
    return _set_status(session, symbol, issue_id, IssueStatus.IGNORED)


@router.post("/stocks/{symbol}/reconciliation/{issue_id}/reopen", responses=_NOT_FOUND)
def reopen_issue(symbol: str, issue_id: int, session: SessionDep) -> IssueOut:
    return _set_status(session, symbol, issue_id, IssueStatus.OPEN)
