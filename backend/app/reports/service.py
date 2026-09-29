"""Load → build → persist a stock report. The only place ``reports/`` writes to the database.

Writes, keyed by the report date (the last price date):
- ``valuation_snapshots``: levels, zone, methods, reverse DCF, sensitivity grid, inputs used
- ``technical_snapshots`` (weekly): refreshed with the buy zone and invalidation
- ``scores``: pillars, grades, knock-outs, earned premium, action
- ``reports``: the StockReport payload
- ``data_gaps``: every missing input the report ran without (rule 1)
Commits nothing; the caller owns the transaction.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.db.enums import Timeframe
from app.db.models import (
    DataGap,
    Instrument,
    Report,
    Score,
    TechnicalSnapshot,
    ValuationSnapshot,
)
from app.db.upsert import upsert
from app.jobs.technicals import snapshot_detail
from app.reports.build import Built, build_report
from app.reports.data import load_stock_data
from app.reports.dto import PeerStats, StockReport
from app.scoring.common import Pillar


def build_for(
    session: Session,
    symbol: str,
    config: AppConfig,
    *,
    peers: list[PeerStats] | None = None,
    benchmark_close: pd.Series | None = None,
) -> Built:
    data = load_stock_data(session, symbol, config, peers=peers, benchmark_close=benchmark_close)
    return build_report(data, config)


def _sensitivity(built: Built) -> dict[str, Any] | None:
    grid, base = built.run.sensitivity, built.run.base
    if grid is None or base is None:
        return None
    return {
        "waccs": grid.waccs,
        "terminal_growths": grid.terminal_growths,
        "values": grid.values,
        "base_wacc": base.wacc,
        "base_g_terminal": base.g_terminal,
    }


def persist(session: Session, built: Built) -> None:
    r = built.report
    iid = session.scalar(select(Instrument.id).where(Instrument.symbol == r.symbol))
    if iid is None:
        raise LookupError(f"{r.symbol}: unknown instrument")
    now = datetime.now(UTC)
    run = built.run
    upsert(
        session,
        ValuationSnapshot,
        [
            {
                "instrument_id": iid,
                "as_of": r.as_of,
                "cmp": r.cmp,
                "baseline": r.levels.baseline,
                "fair_value": r.levels.fair_value,
                "top_band": r.levels.top_band,
                "mos_pct": r.levels.mos_pct,
                "zone": r.zone,
                "confidence": r.levels.confidence,
                "methods": [m.model_dump(mode="json") for m in r.valuation.methods],
                "reverse_dcf": r.valuation.reverse_dcf.model_dump(mode="json")
                if r.valuation.reverse_dcf
                else None,
                "sensitivity": _sensitivity(built),
                "inputs": {
                    "sector": run.sector_key,
                    "dcf_base": asdict(run.base) if run.base else None,
                    "wacc": run.wacc,
                    "beta": run.beta,
                    "overrides": r.overrides,
                },
                "reasons": r.valuation.reasons,
                "computed_at": now,
            }
        ],
    )
    t = built.technical
    upsert(
        session,
        TechnicalSnapshot,
        [
            {
                "instrument_id": iid,
                "as_of": t.as_of.date(),
                "timeframe": Timeframe.WEEKLY,
                "stage": t.stage.stage,
                "rs_percentile": r.technical.rs_percentile,
                "trend_state": t.structure.trend,
                "buy_zone_low": built.buy_zone.low,
                "buy_zone_high": built.buy_zone.high,
                "invalidation": built.buy_zone.invalidation,
                "atr": t.atr_now,
                "detail": snapshot_detail(t, r.symbol),
                "reasons": t.reasons + built.buy_zone.reasons,
                "computed_at": now,
            }
        ],
    )
    pillars = built.grading.pillars
    upsert(
        session,
        Score,
        [
            {
                "instrument_id": iid,
                "as_of": r.as_of,
                **{p.value: (pillars[p].score if p in pillars else None) for p in Pillar},
                "total": r.scores.total,
                "provisional_grade": r.provisional_grade,
                "grade": r.grade,
                "knockouts": r.knockouts.triggered,
                "earned_premium": r.earned_premium,
                "action": r.action,
                "sub_scores": {
                    p.pillar: [s.model_dump(mode="json") for s in p.subs] for p in r.pillars
                },
                "reasons": r.reasons,
                "computed_at": now,
            }
        ],
    )
    upsert(
        session,
        Report,
        [
            {
                "instrument_id": iid,
                "as_of": r.as_of,
                "payload": r.model_dump(mode="json"),
                "sources": r.sources,
                "thesis": r.thesis,
                "computed_at": now,
            }
        ],
    )
    gaps = {(g.dataset, g.field): g for g in built.gaps}  # dedupe within one report
    upsert(
        session,
        DataGap,
        [
            {
                "instrument_id": iid,
                "dataset": str(g.dataset),
                "field": g.field,
                "period": None,
                "reason": g.reason,
                "providers_tried": g.providers_tried,
                "resolved_at": None,
            }
            for g in gaps.values()
        ],
    )


def refresh_report(session: Session, symbol: str, config: AppConfig) -> StockReport:
    built = build_for(session, symbol, config)
    persist(session, built)
    return built.report


def latest_report(session: Session, symbol: str) -> StockReport | None:
    payload = session.scalar(
        select(Report.payload)
        .join(Instrument, Instrument.id == Report.instrument_id)
        .where(Instrument.symbol == symbol.upper())
        .order_by(Report.as_of.desc())
        .limit(1)
    )
    return StockReport.model_validate(payload) if payload is not None else None


def latest_sensitivity(session: Session, symbol: str) -> tuple[Any, dict[str, Any] | None] | None:
    row = session.execute(
        select(ValuationSnapshot.as_of, ValuationSnapshot.sensitivity)
        .join(Instrument, Instrument.id == ValuationSnapshot.instrument_id)
        .where(Instrument.symbol == symbol.upper())
        .order_by(ValuationSnapshot.as_of.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row is not None else None


def latest_payloads(
    session: Session, instrument_ids: list[int] | None = None
) -> dict[int, dict[str, Any]]:
    """Latest stored report payload per instrument (optionally only these instruments)."""
    latest = select(Report.instrument_id, func.max(Report.as_of).label("as_of")).group_by(
        Report.instrument_id
    )
    if instrument_ids is not None:
        latest = latest.where(Report.instrument_id.in_(instrument_ids))
    sub = latest.subquery()
    rows = session.execute(
        select(Report.instrument_id, Report.payload).join(
            sub, (sub.c.instrument_id == Report.instrument_id) & (sub.c.as_of == Report.as_of)
        )
    ).all()
    return {iid: payload for iid, payload in rows}
