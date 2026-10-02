"""Price anomalies of a stock (SPEC §3.2 "price adjustment"): moves that look like a missing
or doubled split / bonus, with the owner's one-click fix. A fix changes corporate_actions and
re-adjusts the prices at once; the report picks it up on its next build (Refresh)."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import PriceAnomaliesOut, PriceAnomalyOut, PriceFixOut
from app.data.price_anomalies import DISMISSED, OPEN, FixError, apply_fix, open_anomalies
from app.db.models import Instrument, PriceAnomaly
from app.jobs.common import readjust

router = APIRouter(tags=["stocks"])
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Unknown symbol or anomaly"}}


def _iid(session: Session, symbol: str) -> int:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return iid


def _out(a: PriceAnomaly) -> PriceAnomalyOut:
    return PriceAnomalyOut.model_validate(a, from_attributes=True)


def _anomaly(session: Session, symbol: str, anomaly_id: int) -> PriceAnomaly:
    iid = _iid(session, symbol)
    a = session.get(PriceAnomaly, anomaly_id)
    if a is None or a.instrument_id != iid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no anomaly {anomaly_id} for {symbol}")
    return a


@router.get("/stocks/{symbol}/price-anomalies", responses=_NOT_FOUND)
def price_anomalies(symbol: str, session: SessionDep) -> PriceAnomaliesOut:
    """Open anomalies, oldest first (found when the prices are re-adjusted)."""
    iid = _iid(session, symbol)
    return PriceAnomaliesOut(symbol=symbol.upper(),
                             open=[_out(a) for a in open_anomalies(session, iid)])  # fmt: skip


_CONFLICT: dict[int | str, dict[str, Any]] = {409: {"description": "Not open / no fix to apply"}}


@router.post("/stocks/{symbol}/price-anomalies/{anomaly_id}/apply",
             responses={**_NOT_FOUND, **_CONFLICT})  # fmt: skip
def apply_price_fix(symbol: str, anomaly_id: int, session: SessionDep,
                    config: ConfigDep) -> PriceFixOut:  # fmt: skip
    """Apply the suggested fix (add the missing split / bonus, or mark the action near the move
    as already in, or missing from, the source's prices) and re-adjust the prices."""
    a = _anomaly(session, symbol, anomaly_id)
    if not a.volume_confirmed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the volume does not confirm a share-count change: no fix offered",
        )
    adj = config.providers.adjustment
    try:
        done = apply_fix(session, a, window_days=adj.suspicious.action_window_days,
                         now=datetime.now(UTC))  # fmt: skip
    except FixError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.flush()
    result = readjust(session, a.instrument_id, adj)
    session.commit()
    remaining = [_out(x) for x in open_anomalies(session, a.instrument_id)]
    return PriceFixOut(
        anomaly=_out(a), done=done, readjusted_bars=result.changed, remaining=remaining
    )


@router.post("/stocks/{symbol}/price-anomalies/{anomaly_id}/dismiss", responses=_NOT_FOUND)
def dismiss_price_anomaly(symbol: str, anomaly_id: int, session: SessionDep) -> PriceAnomalyOut:
    """Dismiss an anomaly you have checked (a genuine move). It stays dismissed."""
    a = _anomaly(session, symbol, anomaly_id)
    if a.status == OPEN:
        a.status, a.resolved_at = DISMISSED, datetime.now(UTC)
        session.commit()
    return _out(a)
