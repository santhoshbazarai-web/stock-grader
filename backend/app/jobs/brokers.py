"""Broker jobs (SPEC §3.3, §10).

- ``bhavcopy_history`` (nightly): builds the NSE bhavcopy OHLCV history backwards, newest
  missing day first, ``bhavcopy.backfill_days_per_run`` files per run (``--full``: all), so
  the price router's NSE fallback reaches back ``providers.history_years`` after a few nights.
- ``broker_token_check`` (08:45 weekdays): for each enabled broker with API credentials and
  ``morning_reminder`` on, an in-app (+ Telegram) notification when its token has expired, so
  it can be reconnected before the market opens. Read-only: nothing is sent to the broker.
"""

from datetime import timedelta
from typing import Any

from app.alerts.notify import notify
from app.core.security import get_cipher
from app.core.settings import get_settings
from app.data.bhavcopy_store import BhavcopyStore
from app.data.broker_tokens import BrokerTokenStore, broker_configured
from app.db.enums import Broker
from app.jobs.runner import JobContext, JobOptions, JobOutcome

NAMES = {Broker.FYERS: "Fyers", Broker.KITE: "Zerodha Kite"}
_CHUNK = 25  # days per router call (each call's first request pays the router's token)


def bhavcopy_history(ctx: JobContext, options: JobOptions) -> JobOutcome:
    # market-wide files: --symbols does not narrow them; --full lifts the per-run budget
    cfg = ctx.config.providers.bhavcopy
    today = ctx.today()
    start = today.replace(year=today.year - ctx.config.providers.history_years)
    missing = BhavcopyStore(ctx.session_factory).missing(start, today - timedelta(days=1))
    todo = list(reversed(missing))  # newest first: recent history is useful soonest
    if not options.full:
        todo = todo[: cfg.backfill_days_per_run]
    out: dict[str, list[str]] = {"loaded": [], "holiday": [], "not_out": []}
    failed: list[str] = []
    for i in range(0, len(todo), _CHUNK):
        res = ctx.router.bhavcopy_backfill(todo[i : i + _CHUNK])
        if res.data is None:
            failed = ["; ".join(res.reasons)[:500]]
            break  # NSE unreachable: stop, the next run resumes
        for k, v in res.data.items():
            out[k] += v
    details: dict[str, Any] = {
        "missing_before": len(missing),
        "loaded": len(out["loaded"]),
        "holidays": out["holiday"][:50],
        "not_out": out["not_out"],
        "remaining": len(missing) - len(out["loaded"]) - len(out["holiday"]),
        "oldest_loaded": min(out["loaded"], default=None),
        "failed": failed,
    }
    return JobOutcome(len(out["loaded"]), details)


def broker_token_check(ctx: JobContext, options: JobOptions) -> JobOutcome:
    del options
    settings = get_settings()
    store = BrokerTokenStore(ctx.session_factory, get_cipher())
    checked: dict[str, str] = {}
    reminded = []
    session = ctx.session_factory()
    try:
        for broker in Broker:
            cfg = ctx.config.providers.brokers[broker]
            if not cfg.enabled:
                checked[broker.value] = "disabled"
                continue
            if not broker_configured(broker, settings):
                checked[broker.value] = "not configured"
                continue
            status = store.status(broker)
            checked[broker.value] = status.reason
            if status.connected or not cfg.morning_reminder:
                continue
            name = NAMES[broker]
            notify(
                session, kind="broker_token", notifier=ctx.notifier,
                title=f"{name} token {'expired' if status.expires_at else 'missing'}: reconnect",
                body=(f"{name} is {status.reason}. Prices fall back to the NSE bhavcopy and "
                      "yfinance until you reconnect in Settings → Brokers (read-only login)."),
            )  # fmt: skip
            reminded.append(broker.value)
        session.commit()
    finally:
        session.close()
    return JobOutcome(len(reminded), {"brokers": checked, "reminded": reminded})
