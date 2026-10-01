"""Every scheduled job (SPEC §10). Schedules live in config/jobs.yaml."""

from app.core.config import JobName
from app.jobs.alerts import alerts_intraday
from app.jobs.annual_reports import annual_reports
from app.jobs.backtests import backtests
from app.jobs.brokers import bhavcopy_history, broker_token_check
from app.jobs.events import events, results_watch
from app.jobs.fundamentals import results_backfill, shareholding
from app.jobs.market import corporate_actions, eod_prices, index_constituents, nse_bhavcopy
from app.jobs.reconcile import reconcile_job
from app.jobs.reports import refresh_queue, valuation_scores
from app.jobs.runner import JobSpec
from app.jobs.symbols import symbol_master
from app.jobs.technicals import technicals
from app.jobs.thesis import thesis

REGISTRY: dict[JobName, JobSpec] = {
    spec.name: spec
    for spec in (
        JobSpec(
            JobName.CORPORATE_ACTIONS,
            "Splits/bonuses/dividends; re-adjust prices",
            corporate_actions,
        ),
        JobSpec(JobName.EOD_PRICES, "Daily OHLCV for universe + indices; adjust", eod_prices),
        JobSpec(JobName.NSE_BHAVCOPY, "Delivery %, ASM/GSM, F&O ban", nse_bhavcopy),
        JobSpec(JobName.TECHNICALS, "Weekly technical snapshots + RS percentile", technicals),
        JobSpec(JobName.VALUATION_SCORES, "Valuations, scores, reports", valuation_scores),
        JobSpec(JobName.ALERTS_INTRADAY, "Evaluate price alerts (market hours)", alerts_intraday),
        JobSpec(JobName.SHAREHOLDING, "Shareholding filings (in season)", shareholding),
        JobSpec(JobName.INDEX_CONSTITUENTS, "Index membership + instruments", index_constituents),
        JobSpec(
            JobName.RESULTS_BACKFILL,
            "Results XBRL filings: list + download (backfill)",
            results_backfill,
        ),
        JobSpec(JobName.REFRESH_QUEUE, "Queued on-demand pipeline runs (fallback)", refresh_queue),
        JobSpec(JobName.BACKTESTS, "Run queued backtests (SPEC §11)", backtests),
        JobSpec(
            JobName.ANNUAL_REPORTS,
            "Annual-report PDFs for BS/CF gaps (§3.6 step 3)",
            annual_reports,
        ),
        JobSpec(JobName.SYMBOL_MASTER, "NSE/BSE/Fyers symbol master + aliases", symbol_master),
        JobSpec(
            JobName.RESULTS_WATCH,
            "Results filings in the NSE/BSE feeds → pipelines + change notifications (§3.8)",
            results_watch,
        ),
        JobSpec(
            JobName.EVENTS,
            "Announcements, pledge, SAST, insider trades, bulk/block deals",
            events,
        ),
        JobSpec(
            JobName.BHAVCOPY_HISTORY,
            "NSE bhavcopy OHLCV history backfill (price fallback)",
            bhavcopy_history,
        ),
        JobSpec(
            JobName.BROKER_TOKEN_CHECK,
            "Morning reminder when a broker token has expired",
            broker_token_check,
        ),
        JobSpec(
            JobName.RECONCILE, "Cross-source checks of the latest periods (§3.9)", reconcile_job
        ),
        JobSpec(JobName.THESIS, "LLM thesis for watchlist reports (local model, §8a)", thesis),
    )
}
