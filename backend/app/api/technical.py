"""Technical debug endpoint: everything the SPEC §6 engine finds, as JSON for chart overlays."""

from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep
from app.data import prices
from app.db.session import get_session
from app.technical.engine import ValuationLevels, analyze, debug_payload

router = APIRouter(prefix="/stocks", tags=["technical"])

SessionDep = Annotated[Session, Depends(get_session)]
GRADES = ("A_plus", "A", "B", "C", "D")


@router.get("/{symbol}/technical/debug")
def technical_debug(
    symbol: str,
    session: SessionDep,
    config: ConfigDep,
    baseline: float | None = None,
    fair_value: float | None = None,
    top_band: float | None = None,
    grade: Annotated[str | None, Query(pattern="^(A_plus|A|B|C|D)$")] = None,
    mos: Annotated[float | None, Query(ge=0, lt=1)] = None,
) -> dict[str, Any]:
    """Weekly bars, 30-week SMA, swings, BOS/CHoCH, demand/supply zones, FVGs, dealing range,
    AVWAPs, volume profile, RS, momentum, participation and the stage.

    Pass ``fair_value`` (plus ``baseline`` / ``top_band`` and ``grade`` or ``mos``) to also get
    the SPEC §6 buy zone for those valuation levels. ``mos`` defaults to the grade's configured
    margin of safety.
    """
    try:
        daily = prices.adjusted_daily(session, symbol)
    except prices.NoPriceData as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except prices.UnadjustedPrices as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    levels = None
    if fair_value is not None:
        g = grade or "B"
        m = mos if mos is not None else float(getattr(config.valuation.mos_by_grade, g))
        levels = ValuationLevels(baseline, fair_value, top_band, m, g)
    results = prices.last_results_date(session, symbol)
    analysis = analyze(
        daily,
        config.technical,
        benchmark_daily_close=prices.close_series(session, config.jobs.universe_index),
        delivery_pct=prices.delivery_pct(session, symbol),
        last_results_date=pd.Timestamp(results) if results else None,
        levels=levels,
    )
    payload = debug_payload(analysis, symbol.upper())
    payload["valuation_levels"] = (
        None
        if levels is None
        else {
            "baseline": baseline,
            "fair_value": fair_value,
            "top_band": top_band,
            "mos": levels.mos,
            "grade": levels.grade,
        }
    )
    return payload
