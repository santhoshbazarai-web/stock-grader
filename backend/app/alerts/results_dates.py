"""Next results date per stock, from the board-meeting calendar (``events``)."""

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.enums import EventKind
from app.db.models import Event


def next_results_dates(session: Session, instrument_ids: list[int], today: date) -> dict[int, date]:
    """Earliest board meeting on or after ``today`` whose purpose mentions results."""
    if not instrument_ids:
        return {}
    like = "%result%"
    rows = session.execute(
        select(Event.instrument_id, func.min(Event.event_date))
        .where(
            Event.kind == EventKind.BOARD_MEETING,
            Event.instrument_id.in_(instrument_ids),
            Event.event_date >= today,
            or_(Event.title.ilike(like), Event.detail.ilike(like), Event.category.ilike(like)),
        )
        .group_by(Event.instrument_id)
    ).all()
    return {iid: d for iid, d in rows if iid is not None and d is not None}
