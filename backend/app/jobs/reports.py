"""``valuation_scores`` (SPEC §10; the nightly precompute) and ``refresh_queue`` (queued
on-demand pipeline runs, SPEC §3.7) jobs: build and store reports.

``valuation_scores`` runs in two passes so relative valuation can use this run's peers:
1. load every symbol and build its report without peers, collecting each stock's
   ``peer_stats`` (multiples, ROCE/ROE, EPS growth);
2. rebuild each report with its sector peers from pass 1 and store it (one commit per symbol,
   so one bad stock cannot lose the rest).
"""

import logging
from collections import defaultdict

from app.data import prices
from app.jobs.common import universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.reports.build import build_report
from app.reports.data import StockData, load_stock_data
from app.reports.dto import PeerStats
from app.reports.service import persist

logger = logging.getLogger(__name__)


def valuation_scores(ctx: JobContext, options: JobOptions) -> JobOutcome:
    config = ctx.config
    symbols = universe(ctx, options)
    session = ctx.session_factory()
    try:
        bench = prices.close_series(session, config.jobs.universe_index)
    finally:
        session.close()

    loaded: dict[str, StockData] = {}
    failed: dict[str, str] = {}
    by_sector: dict[str, list[PeerStats]] = defaultdict(list)
    for symbol in symbols:
        session = ctx.session_factory()
        try:
            data = load_stock_data(session, symbol, config, peers=[], benchmark_close=bench)
            stats = build_report(data, config).run.peer
            loaded[symbol] = data
            by_sector[stats.sector].append(stats)
        except (prices.NoPriceData, prices.UnadjustedPrices) as exc:
            failed[symbol] = str(exc)
        except Exception as exc:  # one stock's bad data must not stop the universe
            logger.exception("valuation_scores pass 1 failed for %s", symbol)
            failed[symbol] = f"{type(exc).__name__}: {exc}"
        finally:
            session.close()

    written = 0
    actions: dict[str, int] = defaultdict(int)
    grades: dict[str, int] = defaultdict(int)
    for symbol, data in loaded.items():
        session = ctx.session_factory()
        try:
            sector = data.overrides.sector or data.sector or "default"
            key = sector if sector in config.sectors.root else "default"
            data.peers = [p for p in by_sector.get(key, []) if p.symbol != symbol]
            built = build_report(data, config)
            persist(session, built)
            session.commit()
            written += 1
            actions[built.report.action or "none"] += 1
            grades[built.report.grade or "none"] += 1
        except Exception as exc:
            session.rollback()
            logger.exception("valuation_scores failed for %s", symbol)
            failed[symbol] = f"{type(exc).__name__}: {exc}"
        finally:
            session.close()
    return JobOutcome(
        written,
        {"reports": written, "failed": failed, "grades": dict(grades), "actions": dict(actions)},
    )


# Per-symbol data jobs re-run by an on-demand refresh, in order.
def refresh_queue(ctx: JobContext, options: JobOptions) -> JobOutcome:
    """Run queued on-demand pipeline runs (SPEC §3.7). The worker's pipeline thread normally
    takes them within a second; this scheduled job is the fallback (and runs them from
    ``python -m app.jobs run refresh_queue``)."""
    from app.pipeline.runner import run_pending

    done = run_pending(ctx)
    if not done:
        return JobOutcome(0, {}, skipped_reason="no queued pipeline runs")
    return JobOutcome(sum(1 for _, st in done if st.value == "done"),
                      {"runs": {str(i): st.value for i, st in done}})  # fmt: skip
