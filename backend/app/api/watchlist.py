"""Watchlists and alerts (SPEC §8, §9). Alerts are in-app + Telegram notifications evaluated by
the ``alerts_intraday`` job; they never place orders or broker GTTs (AGENTS.md rule 7)."""

import csv
import io
import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.alerts.results_dates import next_results_dates
from app.api.deps import SessionDep
from app.api.schemas import (
    SYMBOL_PATTERN,
    AlertIn,
    AlertOut,
    Symbol,
    WatchlistImportIn,
    WatchlistImportOut,
    WatchlistIn,
    WatchlistListIn,
    WatchlistListOut,
    WatchlistOut,
)
from app.db.enums import AlertType
from app.db.models import Alert, Instrument, Report, Watchlist, WatchlistItem
from app.db.upsert import upsert
from app.jobs.alerts import levels_from_report
from app.jobs.common import ensure_instruments
from app.reports.service import latest_payloads

router = APIRouter(tags=["watchlist & alerts"])
DEFAULT_LIST = "Default"
_SYMBOL = re.compile(SYMBOL_PATTERN)


# ───────────────────────── watchlists ─────────────────────────


def _default_list(session: Session) -> Watchlist:
    wl = session.scalar(select(Watchlist).where(Watchlist.name == DEFAULT_LIST))
    if wl is None:
        wl = Watchlist(name=DEFAULT_LIST)
        session.add(wl)
        session.flush()
    return wl


def _list_id(session: Session, list_id: int | None) -> int:
    if list_id is None:
        return _default_list(session).id
    if session.get(Watchlist, list_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no watchlist {list_id}")
    return list_id


@router.get("/watchlists")
def list_watchlists(session: SessionDep) -> list[WatchlistListOut]:
    _default_list(session)
    session.commit()
    rows = session.execute(
        select(Watchlist.id, Watchlist.name, func.count(WatchlistItem.id))
        .outerjoin(WatchlistItem, WatchlistItem.watchlist_id == Watchlist.id)
        .group_by(Watchlist.id, Watchlist.name)
        .order_by(Watchlist.id)
    ).all()
    return [WatchlistListOut(id=i, name=n, count=c) for i, n, c in rows]


@router.post("/watchlists", status_code=status.HTTP_201_CREATED)
def create_watchlist(body: WatchlistListIn, session: SessionDep) -> WatchlistListOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is empty")
    if session.scalar(select(Watchlist.id).where(Watchlist.name == name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a watchlist named {name!r} exists")
    wl = Watchlist(name=name)
    session.add(wl)
    session.commit()
    return WatchlistListOut(id=wl.id, name=wl.name, count=0)


@router.delete(
    "/watchlists/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such list"}, 409: {"description": "The Default list"}},
)
def delete_watchlist(list_id: int, session: SessionDep) -> None:
    wl = session.get(Watchlist, list_id)
    if wl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no watchlist {list_id}")
    if wl.name == DEFAULT_LIST:
        raise HTTPException(status.HTTP_409_CONFLICT, "the Default watchlist cannot be deleted")
    session.delete(wl)
    session.commit()


def _since_last_report(latest: dict[str, Any], prev: dict[str, Any] | None) -> list[str]:
    if not prev:
        return []
    out = []
    for key, label in (("grade", "Grade"), ("zone", "Zone"), ("action", "Action")):
        a, b = prev.get(key), latest.get(key)
        if a != b and (a or b):
            out.append(f"{label}: {a or '-'} → {b or '-'}")
    fa = (prev.get("levels") or {}).get("fair_value")
    fb = (latest.get("levels") or {}).get("fair_value")
    if fa and fb and abs(fb / fa - 1) >= 0.005:
        out.append(f"Fair value: ₹{fa:,.0f} → ₹{fb:,.0f} ({(fb / fa - 1) * 100:+.1f}%)")
    return out


def _previous_payloads(session: Session, ids: list[int]) -> dict[int, dict[str, Any]]:
    """The second-latest stored report per instrument."""
    if not ids:
        return {}
    rn = (
        func.row_number()
        .over(partition_by=Report.instrument_id, order_by=Report.as_of.desc())
        .label("rn")
    )
    sub = (
        select(Report.instrument_id, Report.payload, rn)
        .where(Report.instrument_id.in_(ids))
        .subquery()
    )
    rows = session.execute(select(sub.c.instrument_id, sub.c.payload).where(sub.c.rn == 2)).all()
    return {iid: payload for iid, payload in rows}


@router.get("/watchlist")
def list_watchlist(
    session: SessionDep,
    list_id: int | None = Query(None, description="Watchlist id; Default when omitted"),
) -> list[WatchlistOut]:
    lid = _list_id(session, list_id)
    rows = session.execute(
        select(WatchlistItem, Instrument)
        .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
        .where(WatchlistItem.watchlist_id == lid)
        .order_by(Instrument.symbol)
    ).all()
    ids = [w.instrument_id for w, _ in rows]
    payloads = latest_payloads(session, ids)
    prevs = _previous_payloads(session, ids)
    today = date.today()
    results = next_results_dates(session, ids, today)
    alert_counts = {
        iid: n
        for iid, n in session.execute(
            select(Alert.instrument_id, func.count())
            .where(Alert.instrument_id.in_(ids), Alert.is_active.is_(True))
            .group_by(Alert.instrument_id)
        ).all()
    }
    out = []
    for w, inst in rows:
        p = payloads.get(w.instrument_id) or {}
        cmp, fv = p.get("cmp"), (p.get("levels") or {}).get("fair_value")
        out.append(
            WatchlistOut(
                symbol=inst.symbol,
                name=inst.name,
                notes=w.notes,
                added_at=w.created_at,
                grade=p.get("grade"),
                zone=p.get("zone"),
                action=p.get("action"),
                cmp=cmp,
                discount_pct=(cmp / fv - 1) * 100 if cmp and fv else None,
                next_results_date=results.get(w.instrument_id),
                report_as_of=p.get("as_of"),
                since_last_report=_since_last_report(p, prevs.get(w.instrument_id)),
                active_alerts=alert_counts.get(w.instrument_id, 0),
            )
        )
    return out


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
def add_to_watchlist(body: WatchlistIn, session: SessionDep) -> WatchlistOut:
    """Add (or update the notes of) a stock. Unknown symbols are created and picked up by the
    next data jobs, since the job universe includes the watchlist."""
    symbol = body.symbol.upper()
    lid = _list_id(session, body.list_id)
    iid = ensure_instruments(session, [symbol])[symbol]
    upsert(
        session, WatchlistItem, [{"watchlist_id": lid, "instrument_id": iid, "notes": body.notes}]
    )
    session.commit()
    return next(w for w in list_watchlist(session, lid) if w.symbol == symbol)


@router.post("/watchlist/import")
def import_watchlist(body: WatchlistImportIn, session: SessionDep) -> WatchlistImportOut:
    """Add the symbols in a CSV (first column, or a ``symbol`` column) to a list."""
    lid = _list_id(session, body.list_id)
    rows = [r for r in csv.reader(io.StringIO(body.csv)) if r and any(c.strip() for c in r)]
    col = 0
    if rows and any(c.strip().lower() in ("symbol", "ticker", "nse symbol") for c in rows[0]):
        col = next(
            i
            for i, c in enumerate(rows[0])
            if c.strip().lower() in ("symbol", "ticker", "nse symbol")
        )
        rows = rows[1:]
    seen: set[str] = set()
    valid: list[str] = []
    invalid: list[str] = []
    for r in rows:
        raw = (
            (r[col] if col < len(r) else "")
            .strip()
            .upper()
            .removeprefix("NSE:")
            .removesuffix("-EQ")
        )
        if not raw or raw in seen:
            continue
        seen.add(raw)
        (valid if _SYMBOL.fullmatch(raw) else invalid).append(raw)
    existing = set(
        session.scalars(
            select(Instrument.symbol)
            .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
            .where(WatchlistItem.watchlist_id == lid, Instrument.symbol.in_(valid or [""]))
        )
    )
    new = [s for s in valid if s not in existing]
    if new:
        ids = ensure_instruments(session, new)
        upsert(
            session,
            WatchlistItem,
            [{"watchlist_id": lid, "instrument_id": ids[s], "notes": None} for s in new],
        )
        session.commit()
    return WatchlistImportOut(added=new, already_there=sorted(existing), invalid=invalid)


@router.delete(
    "/watchlist/{symbol}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Not on the watchlist"}},
)
def remove_from_watchlist(symbol: Symbol, session: SessionDep, list_id: int | None = None) -> None:
    lid = _list_id(session, list_id)
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    result = session.execute(
        delete(WatchlistItem).where(
            WatchlistItem.instrument_id == iid, WatchlistItem.watchlist_id == lid
        )
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol.upper()} is not on the watchlist")
    session.commit()


# ───────────────────────── alerts ─────────────────────────


def _inr(v: float) -> str:
    return f"₹{v:,.2f}"


def _condition(a: Alert, payload: dict[str, Any] | None) -> str:
    lv = levels_from_report(payload)
    t = a.alert_type
    if t == AlertType.ENTERS_BUY_ZONE:
        if lv.buy_zone_low is not None and lv.buy_zone_high is not None:
            return f"Price enters the buy zone {_inr(lv.buy_zone_low)} to {_inr(lv.buy_zone_high)}"
        return "Price enters the buy zone"
    named = {
        AlertType.CROSSES_FV: ("fair value", lv.fair_value),
        AlertType.CROSSES_TOP_BAND: ("the top band", lv.top_band),
        AlertType.CROSSES_INVALIDATION: ("the invalidation level", lv.invalidation),
    }
    if t in named:
        name, level = named[t]
        return f"Price crosses {name}" + (f" ({_inr(level)})" if level is not None else "")
    if t in (AlertType.PRICE_ABOVE, AlertType.PRICE_BELOW):
        word = "rises to or above" if t == AlertType.PRICE_ABOVE else "falls to or below"
        return (
            f"Price {word} {_inr(a.threshold)}" if a.threshold else f"Price {word} (no price set)"
        )
    days = f"{int(a.threshold)} days before" if a.threshold else "ahead of"
    return f"Results date: notify {days} the board meeting"


def _status(a: Alert) -> Literal["active", "triggered", "paused"]:
    if not a.is_active:
        return "paused"
    return "triggered" if a.last_triggered_at else "active"


def _alert_out(a: Alert, inst: Instrument, payload: dict[str, Any] | None) -> AlertOut:
    return AlertOut(
        id=a.id,
        symbol=inst.symbol,
        company=inst.name,
        alert_type=a.alert_type,
        condition=_condition(a, payload),
        threshold=a.threshold,
        is_active=a.is_active,
        status=_status(a),
        last_triggered_at=a.last_triggered_at,
        last_triggered_price=a.last_triggered_price,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.get("/alerts")
def list_alerts(
    session: SessionDep,
    symbol: str | None = Query(None, description="Only this symbol"),
    alert_type: AlertType | None = None,
    alert_status: Literal["active", "triggered", "paused"] | None = Query(None, alias="status"),
) -> list[AlertOut]:
    q = (
        select(Alert, Instrument)
        .join(Instrument, Instrument.id == Alert.instrument_id)
        .order_by(Instrument.symbol, Alert.alert_type)
    )
    if symbol:
        q = q.where(Instrument.symbol == symbol.upper())
    if alert_type:
        q = q.where(Alert.alert_type == alert_type)
    rows = session.execute(q).all()
    payloads = latest_payloads(session, [a.instrument_id for a, _ in rows])
    out = [_alert_out(a, i, payloads.get(a.instrument_id)) for a, i in rows]
    return [o for o in out if alert_status is None or o.status == alert_status]


@router.post("/alerts", status_code=status.HTTP_201_CREATED)
def create_alert(body: AlertIn, session: SessionDep) -> AlertOut:
    """Create or update an alert (one per stock and type; a price alert's target is replaced).
    Alerts only notify (in-app and Telegram); they never place orders."""
    if body.alert_type in (AlertType.PRICE_ABOVE, AlertType.PRICE_BELOW) and body.threshold is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a price alert needs a price")
    threshold = body.threshold if body.alert_type in (
        AlertType.PRICE_ABOVE, AlertType.PRICE_BELOW, AlertType.RESULTS_DATE
    ) else None  # fmt: skip
    symbol = body.symbol.upper()
    iid = ensure_instruments(session, [symbol])[symbol]
    upsert(
        session,
        Alert,
        [
            {
                "instrument_id": iid,
                "alert_type": body.alert_type,
                "is_active": body.is_active,
                "threshold": threshold,
                "state": None,
            }
        ],
    )
    session.commit()
    a = session.scalar(
        select(Alert).where(Alert.instrument_id == iid, Alert.alert_type == body.alert_type)
    )
    assert a is not None
    session.refresh(a)
    inst = session.get(Instrument, iid)
    assert inst is not None
    return _alert_out(a, inst, latest_payloads(session, [iid]).get(iid))


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
