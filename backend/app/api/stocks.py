"""Stock endpoints (SPEC §8): search, report, refresh, DCF sensitivity, user overrides."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, delete, or_, select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, RedisDep, SessionDep
from app.api.schemas import (
    InstrumentOut,
    OverridesResponse,
    RefreshQueued,
    Sensitivity,
    Symbol,
)
from app.data import prices
from app.db.models import Instrument, UserOverride
from app.db.upsert import upsert
from app.jobs.refresh import enqueue_refresh
from app.reports.data import load_overrides
from app.reports.dto import StockReport
from app.reports.history import FundamentalsHistory, load_history
from app.reports.overrides import Overrides
from app.reports.service import latest_report, latest_sensitivity, refresh_report

router = APIRouter(prefix="/stocks", tags=["stocks"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown symbol or no stored prices"}
}


def _instrument_id(session: Session, symbol: str) -> int:
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
    if iid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return iid


def _rebuild(session: Session, symbol: str, config: ConfigDep) -> StockReport:
    try:
        report = refresh_report(session, symbol, config)
    except prices.NoPriceData as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except prices.UnadjustedPrices as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return report


@router.get("/search")
def search(
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=64, description="Symbol or name")],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    include_indices: bool = False,
) -> list[InstrumentOut]:
    """Symbol search: exact symbol first, then symbol prefix, then name contains."""
    term = q.strip()
    up = term.upper()
    like = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    stmt = (
        select(Instrument)
        .where(
            Instrument.is_active.is_(True),
            or_(Instrument.symbol.ilike(f"{like}%"), Instrument.name.ilike(f"%{like}%")),
        )
        .order_by(
            case((Instrument.symbol == up, 0), (Instrument.symbol.ilike(f"{like}%"), 1), else_=2),
            Instrument.symbol,
        )
        .limit(limit)
    )
    if not include_indices:
        stmt = stmt.where(Instrument.is_index.is_(False))
    return [InstrumentOut.model_validate(i) for i in session.scalars(stmt)]


@router.get(
    "/{symbol}/report",
    responses={**_NOT_FOUND, 409: {"description": "Prices not yet split/bonus adjusted"}},
)
def report(
    symbol: Symbol, session: SessionDep, config: ConfigDep, rebuild: bool = False
) -> StockReport:
    """The full StockReport DTO: the latest stored report, built now if there is none (or if
    ``rebuild`` is set). Building uses stored data only; ``POST /refresh`` fetches new data."""
    if not rebuild:
        stored = latest_report(session, symbol)
        if stored is not None:
            return stored
    return _rebuild(session, symbol, config)


@router.post("/{symbol}/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh(symbol: Symbol, redis: RedisDep) -> RefreshQueued:
    """Enqueue a data refresh. Within a minute the worker re-fetches prices, corporate actions,
    results and shareholding for the symbol and rebuilds its report. Unknown symbols are
    accepted, so this also adds a new stock."""
    queued, length = enqueue_refresh(redis, symbol)
    return RefreshQueued(symbol=symbol.upper(), queued=queued, queue_length=length)


@router.get("/{symbol}/valuation/sensitivity", responses=_NOT_FOUND)
def sensitivity(symbol: Symbol, session: SessionDep) -> Sensitivity:
    """DCF value per share over a WACC x terminal-growth grid (from the latest valuation)."""
    found = latest_sensitivity(session, symbol)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no valuation for {symbol.upper()} yet")
    as_of, grid = found
    if grid is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no DCF for this stock (financial-sector model, or DCF inputs missing)",
        )
    return Sensitivity(symbol=symbol.upper(), as_of=as_of, **grid)


@router.get("/{symbol}/overrides", responses=_NOT_FOUND)
def get_overrides(symbol: Symbol, session: SessionDep) -> Overrides:
    """User assumptions currently applied to this stock."""
    return load_overrides(session, _instrument_id(session, symbol))


@router.post("/{symbol}/overrides", responses={**_NOT_FOUND, 422: {"description": "Invalid"}})
def set_overrides(
    symbol: Symbol, body: Overrides, session: SessionDep, config: ConfigDep
) -> OverridesResponse:
    """Save user assumptions (g1, margins, WACC, sector model, manual inputs) and recompute the
    report. Fields sent with a value are saved; fields sent as ``null`` are cleared; fields not
    sent are left as they are."""
    iid = _instrument_id(session, symbol)
    if body.sector is not None and body.sector not in config.sectors.root:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown sector {body.sector!r}; choose one of {sorted(config.sectors.root)}",
        )
    values = body.model_dump(mode="json")
    sent = body.model_fields_set
    to_set = [
        {"instrument_id": iid, "key": k, "value": {"value": values[k]}}
        for k in sent
        if values[k] is not None
    ]
    to_clear = [k for k in sent if values[k] is None]
    upsert(session, UserOverride, to_set)
    if to_clear:
        session.execute(
            delete(UserOverride).where(
                UserOverride.instrument_id == iid, UserOverride.key.in_(to_clear)
            )
        )
    session.flush()
    saved = sorted(str(r["key"]) for r in to_set)
    reasons = [f"saved: {', '.join(saved) or 'nothing'}"]
    if to_clear:
        reasons.append(f"cleared: {', '.join(sorted(to_clear))}")
    report_out: StockReport | None
    try:
        report_out = refresh_report(session, symbol, config)
    except (prices.NoPriceData, prices.UnadjustedPrices) as exc:
        report_out = None
        reasons.append(f"report not rebuilt: {exc}")
    session.commit()
    return OverridesResponse(
        symbol=symbol.upper(),
        overrides=load_overrides(session, iid),
        report=report_out,
        reasons=reasons,
    )


@router.delete("/{symbol}/overrides", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
def clear_overrides(symbol: Symbol, session: SessionDep) -> None:
    """Remove every override for this stock (the next report uses computed assumptions)."""
    iid = _instrument_id(session, symbol)
    session.execute(delete(UserOverride).where(UserOverride.instrument_id == iid))
    session.commit()


@router.get("/{symbol}/fundamentals", responses=_NOT_FOUND)
def fundamentals(symbol: Symbol, session: SessionDep, config: ConfigDep) -> FundamentalsHistory:
    """Ten fiscal years of sales, EBITDA, PAT, CFO, FCF, ROCE and CCC, plus the shareholding
    trend, for the report page charts. Missing years are ``null``, never 0."""
    history = load_history(session, symbol, config)
    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown symbol {symbol.upper()}")
    return history
