"""``valuation_scores`` (SPEC §10) and ``refresh_queue`` jobs: build and store reports.

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
from app.jobs.refresh import pop_refresh
from app.jobs.runner import JobContext, JobOptions, JobOutcome, run_job
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
REFRESH_JOBS = ("corporate_actions", "eod_prices", "results_watch", "shareholding")


def refresh_queue(ctx: JobContext, options: JobOptions) -> JobOutcome:
    from app.core.config import JobName
    from app.jobs.registry import REGISTRY
    from app.reports.service import refresh_report

    done: list[str] = []
    failed: dict[str, str] = {}
    while (symbol := pop_refresh(ctx.redis)) is not None:
        sub = JobOptions(symbols=(symbol,), force=True)
        for name in REFRESH_JOBS:
            run_job(REGISTRY[JobName(name)], ctx, sub)
        session = ctx.session_factory()
        try:
            refresh_report(session, symbol, ctx.config)
            session.commit()
            done.append(symbol)
        except Exception as exc:
            session.rollback()
            logger.exception("refresh failed for %s", symbol)
            failed[symbol] = f"{type(exc).__name__}: {exc}"
        finally:
            session.close()
    if not done and not failed:
        return JobOutcome(0, {}, skipped_reason="queue empty")
    return JobOutcome(len(done), {"refreshed": done, "failed": failed})
