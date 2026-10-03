"""``alerts_intraday`` (SPEC §10): check active price alerts against live prices.

Every run (cron in jobs.yaml, trimmed here to ``jobs.alerts.market_open`` to ``market_close`` on
weekdays unless ``--force``):
1. load active alerts and the latest report of each stock (levels: technical buy zone, FV,
   top band, invalidation);
2. fetch LTPs through the router — Fyers first, Kite for any symbol Fyers did not price
   (``providers.yaml`` → ``priority.ltp``);
3. evaluate each alert (``app.alerts.evaluate``: transitions, hysteresis, cooldown), store its
   state, and for each one that fires write an in-app notification (+ Telegram if configured).

Alerts are notifications only: nothing here places orders or broker GTTs (AGENTS.md rule 7).
NSE holidays are not modelled; on a holiday the LTP simply does not move, so nothing fires.
"""

import logging
from collections import Counter
from datetime import UTC
from typing import Any

from sqlalchemy import select

from app.alerts.evaluate import LEVEL_NAME, Levels, evaluate
from app.alerts.notify import notify
from app.alerts.results_dates import next_results_dates
from app.db.models import Alert, Instrument
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.reports.service import latest_payloads

logger = logging.getLogger(__name__)

TITLE = {
    "enters_buy_zone": "entered the buy zone",
    **{k: f"crossed {v}" for k, v in LEVEL_NAME.items()},
    "price_above": "price above target",
    "price_below": "price below target",
    "results_date": "results coming up",
}


def levels_from_report(payload: dict[str, Any] | None) -> Levels:
    if not payload:
        return Levels()
    lv = payload.get("levels") or {}
    bz = payload.get("buy_zone") or {}
    in_zone = bz.get("status") == "zone"
    return Levels(
        buy_zone_low=bz.get("low") if in_zone else None,
        buy_zone_high=bz.get("high") if in_zone else None,
        fair_value=lv.get("fair_value"),
        top_band=lv.get("top_band"),
        invalidation=payload.get("invalidation"),
    )


def alerts_intraday(ctx: JobContext, options: JobOptions) -> JobOutcome:
    cfg = ctx.config.jobs.alerts
    local = ctx.now()
    if not options.force:
        if local.weekday() >= 5:
            return JobOutcome(skipped_reason="weekend: market closed")
        if not cfg.market_open <= local.time() <= cfg.market_close:
            window = f"{cfg.market_open:%H:%M}-{cfg.market_close:%H:%M} IST"
            return JobOutcome(skipped_reason=f"outside market hours ({window})")

    session = ctx.session_factory()
    try:
        q = (
            select(Alert, Instrument.symbol)
            .join(Instrument, Instrument.id == Alert.instrument_id)
            .where(Alert.is_active.is_(True))
            .order_by(Instrument.symbol, Alert.id)
        )
        if options.symbols:
            q = q.where(Instrument.symbol.in_([s.upper() for s in options.symbols]))
        rows = session.execute(q).all()
        if not rows:
            return JobOutcome(skipped_reason="no active alerts")

        payloads = latest_payloads(session, sorted({a.instrument_id for a, _ in rows}))
        symbols = sorted({sym for _, sym in rows})
        prices, price_reasons = ctx.router.ltp_filled(symbols)
        now = ctx.clock().astimezone(UTC)
        results = next_results_dates(
            session, sorted({a.instrument_id for a, _ in rows}), ctx.today()
        )

        fired: list[dict[str, Any]] = []
        notes: dict[str, str] = {}
        for alert, symbol in rows:
            if symbol not in prices:
                notes[f"{symbol}:{alert.alert_type}"] = "no live price"
                continue
            price, source = prices[symbol]
            payload = payloads.get(alert.instrument_id)
            ev = evaluate(
                alert.alert_type,
                price,
                levels_from_report(payload),
                alert.state,
                cfg,
                now=now,
                last_fired_at=alert.last_triggered_at,
                threshold=alert.threshold,
                next_results=results.get(alert.instrument_id),
            )
            alert.state = {**ev.state, "source": str(source)}
            if not ev.fired:
                notes[f"{symbol}:{alert.alert_type}"] = ev.reasons[-1] if ev.reasons else ""
                continue
            alert.last_triggered_at = now
            alert.last_triggered_price = price
            as_of = payload.get("as_of") if payload else None
            n = notify(
                session,
                kind=str(alert.alert_type),
                title=f"{symbol} {TITLE.get(str(alert.alert_type), str(alert.alert_type))}",
                body=f"{symbol} {ev.message} (levels from the {as_of} report; price via {source})",
                notifier=ctx.notifier,
                symbol=symbol,
                price=price,
                alert_id=alert.id,
            )
            fired.append(
                {
                    "symbol": symbol,
                    "alert": str(alert.alert_type),
                    "price": price,
                    "telegram": n.telegram,
                }
            )
        session.commit()
    finally:
        session.close()

    return JobOutcome(
        len(fired),
        {
            "checked": len(rows),
            "fired": fired,
            "priced_by": dict(Counter(str(src) for _, src in prices.values())),
            "unpriced": [s for s in symbols if s not in prices],
            "price_reasons": price_reasons[-5:],
            "notes": notes,
        },
    )
