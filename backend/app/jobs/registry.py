"""Every scheduled job (SPEC §10). Schedules live in config/jobs.yaml."""

from app.core.config import JobName
from app.jobs.fundamentals import results_watch, shareholding
from app.jobs.market import corporate_actions, eod_prices, index_constituents, nse_bhavcopy
from app.jobs.runner import JobSpec
from app.jobs.technicals import technicals

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
        JobSpec(JobName.VALUATION_SCORES, "Valuations, scores, reports", pending_phase="P10/P11"),
        JobSpec(JobName.ALERTS_INTRADAY, "Evaluate price alerts", pending_phase="P14"),
        JobSpec(JobName.SHAREHOLDING, "Shareholding filings (in season)", shareholding),
        JobSpec(JobName.INDEX_CONSTITUENTS, "Index membership + instruments", index_constituents),
        JobSpec(JobName.RESULTS_WATCH, "Flag new quarterly results (in season)", results_watch),
    )
}
