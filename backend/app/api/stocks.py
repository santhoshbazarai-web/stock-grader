"""Stock endpoints (SPEC §8): search, aliases, report, refresh, DCF sensitivity, user
overrides."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import ConfigDep, SessionDep
from app.api.schemas import (
    AliasIn,
    AliasOut,
    OverridesResponse,
    RefreshQueued,
    SearchHitOut,
    Sensitivity,
    Symbol,
)
from app.data import prices
from app.data.search import search as symbol_search
from app.db.enums import AliasKind
from app.db.models import Instrument, PipelineRun, SymbolAlias, UserOverride
from app.db.models import Symbol as SymbolMaster
from app.db.upsert import upsert
from app.pipeline.runner import TERMINAL, active_symbols, start_run
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
    config: ConfigDep,
    q: Annotated[
        str,
        Query(
            min_length=1,
            max_length=64,
            description="NSE symbol, BSE code, ISIN, company name, or a former name",
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    include_indices: bool = False,
) -> list[SearchHitOut]:
    """Fuzzy symbol search (SPEC §3.5, pg_trgm): exact code matches first (NSE symbol, BSE
    code, ISIN, Fyers ticker, a former symbol), then Nifty 500 members, then the rest by
    similarity. Names and aliases (former names, BSE names, your own) match fuzzily."""
    hits = symbol_search(session, q, cfg=config.providers.symbols.search,
                         universe_index=config.jobs.universe_index, limit=limit,
                         include_indices=include_indices)  # fmt: skip
    return [SearchHitOut(**asdict(h)) for h in hits]


def _master(session: Session, symbol: str) -> SymbolMaster:
    master = session.scalar(
        select(SymbolMaster).join(Instrument, Instrument.id == SymbolMaster.instrument_id)
        .where(Instrument.symbol == symbol.upper())
    )  # fmt: skip
    if master is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"{symbol.upper()} is not in the symbol master yet (run the "
                            "symbol_master job)")  # fmt: skip
    return master


def _alias_out(a: SymbolAlias) -> AliasOut:
    return AliasOut(id=a.id, alias=a.alias, kind=a.kind.value, source=a.source,
                    valid_until=a.valid_until)  # fmt: skip


@router.get("/{symbol}/aliases", responses=_NOT_FOUND)
def list_aliases(symbol: Symbol, session: SessionDep) -> list[AliasOut]:
    """Names the stock is also found by: former symbols and names, BSE's, and your own."""
    master = _master(session, symbol)
    rows = session.scalars(select(SymbolAlias).where(SymbolAlias.symbol_id == master.id)
                           .order_by(SymbolAlias.kind, SymbolAlias.alias))  # fmt: skip
    return [_alias_out(a) for a in rows]


@router.post("/{symbol}/aliases", status_code=status.HTTP_201_CREATED, responses=_NOT_FOUND)
def add_alias(symbol: Symbol, body: AliasIn, session: SessionDep) -> AliasOut:
    """Add your own alias (e.g. a nickname); search finds the stock by it."""
    master = _master(session, symbol)
    alias = " ".join(body.alias.split())
    upsert(session, SymbolAlias, [{"symbol_id": master.id, "alias": alias,
                                   "kind": AliasKind.USER, "source": "user",
                                   "valid_until": None}], update=[])  # fmt: skip
    row = session.scalars(select(SymbolAlias).where(
        SymbolAlias.symbol_id == master.id, SymbolAlias.kind == AliasKind.USER,
        SymbolAlias.alias == alias)).one()  # fmt: skip
    session.commit()
    return _alias_out(row)


@router.delete(
    "/{symbol}/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Unknown stock or alias"},
        409: {"description": "Only your own aliases can be deleted"},
    },
)
def delete_alias(symbol: Symbol, alias_id: int, session: SessionDep) -> None:
    master = _master(session, symbol)
    row = session.get(SymbolAlias, alias_id)
    if row is None or row.symbol_id != master.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no alias {alias_id} for {symbol}")
    if row.kind is not AliasKind.USER:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "only aliases you added can be deleted; the others come from the "
                            "exchange files")  # fmt: skip
    session.delete(row)
    session.commit()


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
def refresh(symbol: Symbol, session: SessionDep, config: ConfigDep) -> RefreshQueued:
    """Re-fetch the stock's data and rebuild its report: starts (or joins) an on-demand
    pipeline run even when the stored report is fresh (SPEC §3.7). Unknown symbols are
    accepted when the symbol master knows them (or is not built yet), so this also adds a new
    stock."""
    existing = session.scalar(
        select(PipelineRun.id).where(PipelineRun.symbol == symbol.upper(),
                                     PipelineRun.status.not_in(TERMINAL))
    )  # fmt: skip
    run, _ = start_run(session, symbol, trigger="refresh", force=True,
                       cfg=config.jobs.pipeline, now=datetime.now(UTC))  # fmt: skip
    assert run is not None
    session.commit()
    return RefreshQueued(symbol=symbol.upper(), queued=existing is None,
                         queue_length=len(active_symbols(session)), run_id=run.id)  # fmt: skip


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
