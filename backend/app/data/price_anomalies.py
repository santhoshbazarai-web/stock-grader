"""Price anomalies (SPEC §3.2 "price adjustment"): storage of ``suspicious_moves`` and the
owner's one-click fixes. Never applied by a job.

Fixes, by kind:
- ``missing_action``: add the suggested split (``ratio_new`` shares for ``ratio_old``) on the
  move's date (``corporate_actions.source = 'owner'``);
- ``double_adjusted``: mark the action near it as already in the price source's bars
  (``price_adjusted_by_source = true``: never applied);
- ``not_applied``: mark it as raw in the bars (``false``: always applied).
Then the instrument's prices are re-adjusted.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.adjust import ADJUSTING, SuspiciousMove
from app.db.enums import CorporateActionType
from app.db.models import CorporateAction, PriceAnomaly
from app.db.upsert import upsert

OPEN, APPLIED, DISMISSED, RESOLVED = "open", "applied", "dismissed", "resolved"


def save_anomalies(session: Session, instrument_id: int, moves: Iterable[SuspiciousMove],
                   now: datetime) -> int:  # fmt: skip
    """Upsert open anomalies; an open one no longer detected is resolved. Applied or dismissed
    ones keep their status. Returns how many are open."""
    found = {m.day: m for m in moves}
    existing = {a.day: a for a in session.scalars(
        select(PriceAnomaly).where(PriceAnomaly.instrument_id == instrument_id))}  # fmt: skip
    rows = []
    for day, m in found.items():
        old = existing.get(day)
        if old is not None and old.status in (APPLIED, DISMISSED):
            continue
        rows.append({
            "instrument_id": instrument_id, "day": day, "ratio": m.ratio,
            "candidate": m.candidate, "volume_ratio": m.volume_ratio,
            "volume_confirmed": m.volume_confirmed, "kind": m.kind,
            "action_ex_date": m.action_ex_date, "ratio_old": m.ratio_old,
            "ratio_new": m.ratio_new, "text": m.text, "status": OPEN,
            "detected_at": old.detected_at if old is not None else now, "resolved_at": None,
        })  # fmt: skip
    if rows:
        upsert(session, PriceAnomaly, rows)
    for day, a in existing.items():
        if day not in found and a.status == OPEN:
            a.status, a.resolved_at = RESOLVED, now
    return len(rows)


def open_anomalies(session: Session, instrument_id: int) -> list[PriceAnomaly]:
    return list(session.scalars(
        select(PriceAnomaly)
        .where(PriceAnomaly.instrument_id == instrument_id, PriceAnomaly.status == OPEN)
        .order_by(PriceAnomaly.day)
    ))  # fmt: skip


class FixError(ValueError):
    pass


def apply_fix(session: Session, anomaly: PriceAnomaly, *, window_days: int,
              now: datetime) -> str:  # fmt: skip
    """Make the corporate-actions change the anomaly suggests (the caller re-adjusts and
    commits). Returns what was done."""
    if anomaly.status != OPEN:
        raise FixError(f"anomaly is {anomaly.status}, not open")
    if anomaly.kind == "missing_action":
        if not anomaly.ratio_old or not anomaly.ratio_new:
            raise FixError("no suggested ratio")
        session.add(CorporateAction(
            instrument_id=anomaly.instrument_id, ex_date=anomaly.day,
            action_type=CorporateActionType.SPLIT, ratio_old=anomaly.ratio_old,
            ratio_new=anomaly.ratio_new, source="owner", fetched_at=now,
            description=f"owner's fix from the price anomaly: {anomaly.text}"[:2000],
        ))  # fmt: skip
        done = (f"added a {anomaly.ratio_old}:{anomaly.ratio_new} split/bonus ex "
                f"{anomaly.day}")  # fmt: skip
    else:
        ex = anomaly.action_ex_date
        if ex is None:
            raise FixError("no corporate action near the move")
        win = timedelta(days=window_days)
        actions = [a for a in session.scalars(select(CorporateAction).where(
            CorporateAction.instrument_id == anomaly.instrument_id,
            CorporateAction.ex_date.between(ex - win, ex + win)))
            if a.action_type.value in ADJUSTING]  # fmt: skip
        if not actions:
            raise FixError(f"no split/bonus on record near {ex}")
        flag = anomaly.kind == "double_adjusted"
        for a in actions:
            a.price_adjusted_by_source = flag
        done = f"marked the action(s) ex {ex} as " + (
            "already in the source's prices" if flag else "raw in the prices: applied"
        )
    anomaly.status, anomaly.resolved_at = APPLIED, now
    return done
