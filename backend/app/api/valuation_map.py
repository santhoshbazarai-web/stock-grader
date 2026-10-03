"""Valuation map (UI): every stored latest report as one row, for the treemap / zone views.
Reads stored reports only (no recomputation); cached 5 minutes in Redis."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import RedisDep, SessionDep
from app.db.models import IndexMembership
from app.reports.service import latest_payloads

router = APIRouter(tags=["valuation map"])

CACHE_TTL_S = 300  # the spec'd 5 minutes
DEPTH_RANK = {"technical_only": 0, "provisional": 1, "full": 2}


class MapRow(BaseModel):
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap_cr: float | None
    price: float
    day_change_pct: float | None
    fair_value: float
    discount_pct: float  # price / fair value - 1 (negative = below fair value)
    zone: str | None
    grade: str | None
    depth: str | None
    confidence: str | None
    durability: str | None = None  # strong | moderate | weak (a proxy, not a moat rating)
    as_of: date


class ValuationMap(BaseModel):
    universe: str
    universe_note: str | None
    rows: list[MapRow]
    excluded: int  # below provisional depth, or no fair value
    total: int


def _norm(name: str) -> str:
    return "".join(name.upper().split())


def build_map(payloads: list[dict[str, Any]], universe: str, note: str | None) -> ValuationMap:
    rows, excluded = [], 0
    for p in payloads:
        depth = (p.get("data_depth") or {}).get("level")
        fv = (p.get("levels") or {}).get("fair_value")
        if depth is None or DEPTH_RANK.get(depth, 0) < DEPTH_RANK["provisional"] or not fv:
            excluded += 1
            continue
        val = p.get("valuation") or {}
        rows.append(MapRow(
            symbol=p["symbol"], name=p.get("name"), sector=p.get("sector") or val.get("sector"),
            industry=p.get("industry"), market_cap_cr=val.get("market_cap_cr"), price=p["cmp"],
            day_change_pct=p.get("day_change_pct"), fair_value=fv,
            discount_pct=p["cmp"] / fv - 1, zone=p.get("zone"), grade=p.get("grade_label"),
            depth=depth, confidence=(p.get("levels") or {}).get("confidence"),
            durability=(p.get("durability") or {}).get("rating"), as_of=p["as_of"],
        ))  # fmt: skip
    rows.sort(key=lambda r: r.symbol)
    return ValuationMap(universe=universe, universe_note=note, rows=rows, excluded=excluded,
                        total=len(rows) + excluded)  # fmt: skip


@router.get("/valuation-map")
def valuation_map(
    session: SessionDep,
    redis: RedisDep,
    universe: Annotated[str, Query(max_length=32)] = "NIFTY500",
) -> ValuationMap:
    """One row per stock of the universe (index members; every stored report when the index has
    no membership on file), from the latest stored reports."""
    key = f"valuation-map:{_norm(universe)}"
    cached = redis.get(key)
    if cached:
        return ValuationMap.model_validate_json(cached)
    members = {
        iid
        for iid, name in session.execute(
            select(IndexMembership.instrument_id, IndexMembership.index_name).where(
                IndexMembership.effective_to.is_(None)
            )
        )
        if _norm(name) == _norm(universe)
    }
    note = None
    payloads = latest_payloads(session, sorted(members) if members else None)
    if not members:
        note = f"no {universe} membership on file: all stored reports"
    out = build_map(list(payloads.values()), universe, note)
    redis.set(key, out.model_dump_json(), ex=CACHE_TTL_S)
    return out
