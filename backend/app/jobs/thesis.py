"""``thesis`` job (SPEC §8a): write the LLM thesis for watchlist reports whose numbers changed.

Runs after the nightly reports. A thesis that already passed for the same fact sheet is reused
(no model call), so a quiet night costs nothing. Skipped while the generator is off or no local
model is configured.
"""

from sqlalchemy import select

from app.core.rate_limiter import RateLimiter
from app.core.settings import get_settings
from app.db.models import Instrument, WatchlistItem
from app.jobs.runner import JobContext, JobOptions, JobOutcome
from app.reports.thesis_service import build_model, disabled_reason, generate


def thesis(ctx: JobContext, options: JobOptions) -> JobOutcome:
    cfg = ctx.config.jobs.thesis
    model = ctx.thesis_model
    if model is None:
        settings = get_settings()
        reason = disabled_reason(settings, cfg)
        if reason is not None:
            return JobOutcome(skipped_reason=reason)
        limiter = RateLimiter(ctx.redis, ctx.config.providers.rate_limits)
        model = build_model(settings, cfg, limiter)
    if model is None:
        return JobOutcome(skipped_reason="no model")
    if options.symbols:
        symbols = [s.upper() for s in options.symbols]
    elif cfg.nightly_scope == "watchlist":
        with ctx.session_factory() as s:
            symbols = list(s.scalars(select(Instrument.symbol).join(
                WatchlistItem, WatchlistItem.instrument_id == Instrument.id
            ).order_by(Instrument.symbol)))  # fmt: skip
    else:
        return JobOutcome(skipped_reason="thesis.nightly_scope is none")
    counts: dict[str, int] = {}
    problems: dict[str, list[str]] = {}
    written = 0
    for symbol in symbols[: cfg.max_per_run]:
        session = ctx.session_factory()
        try:
            view = generate(session, symbol, cfg, model, now=ctx.clock())
        except LookupError:
            counts["no_report"] = counts.get("no_report", 0) + 1
            continue
        finally:
            session.close()
        counts[view.status] = counts.get(view.status, 0) + 1
        if view.status == "ok":
            written += 1
        else:
            problems[symbol] = view.problems[:3]
    return JobOutcome(written, {"model": model.name, "symbols": len(symbols),
                                "outcomes": counts, "problems": problems})  # fmt: skip
