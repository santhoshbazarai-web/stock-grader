"""Screener (SPEC §8, §9): filter and sort the latest report of every stock."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.schemas import ScreenerRow
from app.core.config import GradeKey, ZoneKey
from app.reports.service import latest_payloads

router = APIRouter(tags=["screener"])

SortKey = Literal[
    "symbol", "total_score", "pct_to_buy_zone", "earned_premium", "rs_percentile",
    "market_cap_cr", "cmp",
]  # fmt: skip


def distance_to_buy_zone(cmp: float, low: float | None, high: float | None) -> float | None:
    """0 inside the zone; above it, the fall needed as a fraction of CMP (> 0); below it, the
    rise back to the zone's low (< 0)."""
    if low is None or high is None or cmp <= 0:
        return None
    if cmp > high:
        return (cmp - high) / cmp
    if cmp < low:
        return (cmp - low) / cmp
    return 0.0


def row(payload: dict[str, Any]) -> ScreenerRow:
    bz = payload.get("buy_zone") or {}
    in_zone = bz.get("status") == "zone"
    low, high = (bz.get("low"), bz.get("high")) if in_zone else (None, None)
    val = payload["valuation"]
    return ScreenerRow(
        symbol=payload["symbol"],
        name=payload.get("name"),
        sector=val["sector"],
        as_of=payload["as_of"],
        cmp=payload["cmp"],
        grade=payload.get("grade"),
        grade_label=payload.get("grade_label"),
        zone=payload.get("zone"),
        action=payload.get("action"),
        total_score=payload["scores"].get("total"),
        fair_value=payload["levels"].get("fair_value"),
        buy_zone_low=low,
        buy_zone_high=high,
        pct_to_buy_zone=distance_to_buy_zone(payload["cmp"], low, high),
        earned_premium=payload.get("earned_premium"),
        rs_percentile=payload["technical"].get("rs_percentile"),
        market_cap_cr=val.get("market_cap_cr"),
    )


@router.get("/screener")
def screener(
    session: SessionDep,
    grade: Annotated[list[GradeKey] | None, Query()] = None,
    zone: Annotated[list[ZoneKey] | None, Query()] = None,
    sector: Annotated[list[str] | None, Query()] = None,
    action: Annotated[list[str] | None, Query()] = None,
    min_earned_premium: Annotated[int | None, Query(ge=0, le=8)] = None,
    max_distance_to_buy_zone: Annotated[
        float | None, Query(description="Keep stocks within this fraction above their buy zone")
    ] = None,
    min_mcap_cr: Annotated[float | None, Query(ge=0)] = None,
    max_mcap_cr: Annotated[float | None, Query(ge=0)] = None,
    sort: SortKey = "total_score",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[ScreenerRow]:
    """Filters: grade, zone, sector, action, earned-premium score, distance to buy zone,
    market cap. Stocks missing the sorted field sort last."""
    rows = [row(p) for p in latest_payloads(session).values()]

    def keep(r: ScreenerRow) -> bool:
        if grade and r.grade not in grade:
            return False
        if zone and r.zone not in zone:
            return False
        if sector and r.sector not in sector:
            return False
        if action and r.action not in action:
            return False
        if min_earned_premium is not None and (r.earned_premium or 0) < min_earned_premium:
            return False
        if max_distance_to_buy_zone is not None and (
            r.pct_to_buy_zone is None or r.pct_to_buy_zone > max_distance_to_buy_zone
        ):
            return False
        if min_mcap_cr is not None and (r.market_cap_cr is None or r.market_cap_cr < min_mcap_cr):
            return False
        return not (
            max_mcap_cr is not None and (r.market_cap_cr is None or r.market_cap_cr > max_mcap_cr)
        )

    kept = [r for r in rows if keep(r)]
    present = [r for r in kept if getattr(r, sort) is not None]
    missing = [r for r in kept if getattr(r, sort) is None]
    present.sort(key=lambda r: getattr(r, sort), reverse=order == "desc")
    return (present + sorted(missing, key=lambda r: r.symbol))[:limit]
