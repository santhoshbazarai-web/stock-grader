"""Watchlist and price alerts (SPEC §8). Alerts are in-app notifications evaluated by the
``alerts_intraday`` job (P14); they never place orders or broker GTTs (AGENTS.md rule 7)."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from app.api.deps import SessionDep
from app.api.schemas import AlertIn, AlertOut, Symbol, WatchlistIn, WatchlistOut
from app.db.models import Alert, Instrument, WatchlistItem
from app.db.upsert import upsert
from app.jobs.common import ensure_instruments
from app.reports.service import latest_payloads

router = APIRouter(tags=["watchlist & alerts"])


@router.get("/watchlist")
def list_watchlist(session: SessionDep) -> list[WatchlistOut]:
    rows = session.execute(
        select(WatchlistItem, Instrument)
        .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
        .order_by(Instrument.symbol)
    ).all()
    payloads = latest_payloads(session, [w.instrument_id for w, _ in rows])
    out = []
    for w, inst in rows:
        p = payloads.get(w.instrument_id) or {}
        out.append(
            WatchlistOut(
                symbol=inst.symbol,
                name=inst.name,
                notes=w.notes,
                added_at=w.created_at,
                grade=p.get("grade"),
                zone=p.get("zone"),
                action=p.get("action"),
                cmp=p.get("cmp"),
            )
        )
    return out


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
def add_to_watchlist(body: WatchlistIn, session: SessionDep) -> WatchlistOut:
    """Add (or update the notes of) a stock. Unknown symbols are created and picked up by the
    next data jobs, since the job universe includes the watchlist."""
    symbol = body.symbol.upper()
    iid = ensure_instruments(session, [symbol])[symbol]
    upsert(session, WatchlistItem, [{"instrument_id": iid, "notes": body.notes}])
    session.commit()
    return next(w for w in list_watchlist(session) if w.symbol == symbol)


@router.delete(
    "/watchlist/{symbol}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Not on the watchlist"}},
)
def remove_from_watchlist(symbol: Symbol, session: SessionDep) -> None:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    result = session.execute(delete(WatchlistItem).where(WatchlistItem.instrument_id == iid))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol.upper()} is not on the watchlist")
    session.commit()


def _alert_out(a: Alert, symbol: str) -> AlertOut:
    return AlertOut(
        id=a.id,
        symbol=symbol,
        alert_type=a.alert_type,
        is_active=a.is_active,
        last_triggered_at=a.last_triggered_at,
        last_triggered_price=a.last_triggered_price,
        created_at=a.created_at,
    )


@router.get("/alerts")
def list_alerts(session: SessionDep) -> list[AlertOut]:
    rows = session.execute(
        select(Alert, Instrument.symbol)
        .join(Instrument, Instrument.id == Alert.instrument_id)
        .order_by(Instrument.symbol, Alert.alert_type)
    ).all()
    return [_alert_out(a, s) for a, s in rows]


@router.post("/alerts", status_code=status.HTTP_201_CREATED)
def create_alert(body: AlertIn, session: SessionDep) -> AlertOut:
    """Create or re-activate an alert: price enters the buy zone, crosses FV, crosses the top
    band, or crosses the invalidation level. One alert per stock and type."""
    symbol = body.symbol.upper()
    iid = ensure_instruments(session, [symbol])[symbol]
    upsert(
        session,
        Alert,
        [{"instrument_id": iid, "alert_type": body.alert_type, "is_active": body.is_active}],
    )
    session.commit()
    a = session.scalar(
        select(Alert).where(Alert.instrument_id == iid, Alert.alert_type == body.alert_type)
    )
    assert a is not None
    session.refresh(a)
    return _alert_out(a, symbol)


@router.delete(
    "/alerts/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such alert"}},
)
def delete_alert(alert_id: int, session: SessionDep) -> None:
    result = session.execute(delete(Alert).where(Alert.id == alert_id))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no alert {alert_id}")
    session.commit()
