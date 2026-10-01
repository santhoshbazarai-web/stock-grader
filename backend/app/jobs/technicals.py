"""``technicals`` job (SPEC §10): weekly technical snapshots for the universe.

Two passes: analyse every symbol, then rank the latest Mansfield RS across the universe for
``rs_percentile``. Buy-zone columns stay NULL here: the buy zone needs valuation levels and is
written by ``valuation_scores`` (P11) using the same engine.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from app.data import prices
from app.db.enums import Timeframe
from app.db.models import TechnicalSnapshot
from app.db.upsert import upsert
from app.jobs.common import ensure_instruments, universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.technical.engine import TechnicalAnalysis, analyze, debug_payload
from app.technical.rs import percentile_rank, percentile_ranks

logger = logging.getLogger(__name__)

# The chart can always re-fetch full detail from the debug endpoint; the snapshot keeps a
# compact summary.
_DETAIL_KEYS = (
    "stage",
    "structure_events",
    "zones",
    "dealing_range",
    "volume_profile",
    "momentum",
    "participation",
    "rs",
    "supports",
)


def snapshot_detail(t: TechnicalAnalysis, symbol: str) -> dict[str, Any]:
    full = debug_payload(t, symbol)
    detail = {k: full[k] for k in _DETAIL_KEYS}
    detail["avwaps"] = [
        {k: a[k] for k in ("anchor", "anchor_time", "value")} for a in full["avwaps"]
    ]
    if detail["volume_profile"]:
        detail["volume_profile"] = {k: detail["volume_profile"][k] for k in ("poc", "vah", "val")}
    detail["rs"] = {k: v for k, v in detail["rs"].items() if k != "series_benchmark"}
    detail["zones"] = [z for z in full["zones"] if not z["broken"]]
    return detail


def technicals(ctx: JobContext, options: JobOptions) -> JobOutcome:
    cfg = ctx.config.technical
    session = ctx.session_factory()
    try:
        bench = prices.close_series(session, ctx.config.jobs.universe_index)
    finally:
        session.close()
    results: dict[str, TechnicalAnalysis] = {}
    failed: dict[str, str] = {}
    for symbol in universe(ctx, options):
        session = ctx.session_factory()
        try:
            daily = prices.adjusted_daily(session, symbol)
            rd = prices.last_results_date(session, symbol)
            results[symbol] = analyze(
                daily,
                cfg,
                benchmark_daily_close=bench,
                delivery_pct=prices.delivery_pct(session, symbol),
                last_results_date=pd.Timestamp(rd) if rd else None,
            )
        except (prices.NoPriceData, prices.UnadjustedPrices) as exc:
            failed[symbol] = str(exc)
        finally:
            session.close()

    latest_rs = {
        s: (float(t.rs_benchmark.iloc[-1]) if t.rs_benchmark is not None else None)
        for s, t in results.items()
    }
    ranks = percentile_ranks(latest_rs)
    rows = []
    session = ctx.session_factory()
    try:
        ids = ensure_instruments(session, results)
        for symbol, t in results.items():
            rows.append(
                {
                    "instrument_id": ids[symbol],
                    "as_of": t.as_of.date(),
                    "timeframe": Timeframe.WEEKLY,
                    "stage": t.stage.stage,
                    "rs_percentile": ranks.get(symbol),
                    "trend_state": t.structure.trend,
                    "buy_zone_low": None,
                    "buy_zone_high": None,
                    "invalidation": None,
                    "atr": t.atr_now,
                    "detail": snapshot_detail(t, symbol),
                    "reasons": t.reasons,
                }
            )
        written = upsert(session, TechnicalSnapshot, rows)
        session.commit()
    finally:
        session.close()
    stages: dict[str, int] = {}
    for t in results.values():
        key = str(t.stage.stage)
        stages[key] = stages.get(key, 0) + 1
    return JobOutcome(
        written,
        {
            "analysed": len(results),
            "failed": failed,
            "stages": stages,
            "universe_ranked": sum(v is not None for v in ranks.values()),
        },
    )


def technicals_for(ctx: JobContext, symbol: str) -> dict[str, Any]:
    """One stock's weekly snapshot (the on-demand pipeline, SPEC §3.7). Its RS percentile is
    ranked against the universe's latest stored snapshots (``rs_percentile_max_age_days``), so
    one stock is never ranked among itself alone. Raises NoPriceData / UnadjustedPrices."""
    cfg = ctx.config.technical
    session = ctx.session_factory()
    try:
        bench = prices.close_series(session, ctx.config.jobs.universe_index)
        daily = prices.adjusted_daily(session, symbol)
        rd = prices.last_results_date(session, symbol)
        t = analyze(daily, cfg, benchmark_daily_close=bench,
                    delivery_pct=prices.delivery_pct(session, symbol),
                    last_results_date=pd.Timestamp(rd) if rd else None)  # fmt: skip
        iid = ensure_instruments(session, [symbol])[symbol]
        since = t.as_of.date() - timedelta(days=cfg.rs_percentile_max_age_days)
        latest = (
            select(TechnicalSnapshot.instrument_id, func.max(TechnicalSnapshot.as_of).label("d"))
            .where(TechnicalSnapshot.timeframe == Timeframe.WEEKLY,
                   TechnicalSnapshot.as_of >= since, TechnicalSnapshot.instrument_id != iid)
            .group_by(TechnicalSnapshot.instrument_id)
            .subquery()
        )  # fmt: skip
        others: Sequence[Any] = session.scalars(
            select(TechnicalSnapshot.detail).join(
                latest, (TechnicalSnapshot.instrument_id == latest.c.instrument_id)
                & (TechnicalSnapshot.as_of == latest.c.d))
            .where(TechnicalSnapshot.timeframe == Timeframe.WEEKLY)
        ).all()  # fmt: skip
        values: dict[str, float | None] = {
            str(i): ((d or {}).get("rs") or {}).get("mansfield_benchmark")
            for i, d in enumerate(others)
        }
        own = float(t.rs_benchmark.iloc[-1]) if t.rs_benchmark is not None else None
        values[symbol] = own
        rank = percentile_rank(values, symbol) if len(values) > 1 else None
        upsert(session, TechnicalSnapshot, [{
            "instrument_id": iid, "as_of": t.as_of.date(), "timeframe": Timeframe.WEEKLY,
            "stage": t.stage.stage, "rs_percentile": rank, "trend_state": t.structure.trend,
            "buy_zone_low": None, "buy_zone_high": None, "invalidation": None,
            "atr": t.atr_now, "detail": snapshot_detail(t, symbol), "reasons": t.reasons,
        }])  # fmt: skip
        session.commit()
    finally:
        session.close()
    return {"stage": t.stage.stage, "rs_percentile": rank, "ranked_against": len(values) - 1}
