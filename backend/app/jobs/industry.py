"""industry_classification: each universe stock's industry → its sector model (SPEC §5,
``config/industries.yaml``). Unclassified stocks first, then the stalest, up to
``max_per_run`` a run."""

from datetime import timedelta

from sqlalchemy import or_, select

from app.data.industry_store import classify_symbol
from app.db.models import Instrument
from app.jobs.common import universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome


def industry_classification(ctx: JobContext, options: JobOptions) -> JobOutcome:
    cfg = ctx.config.jobs.industry_classification
    symbols = universe(ctx, options)
    now = ctx.clock()
    session = ctx.session_factory()
    try:
        if not options.symbols:
            stale_before = now - timedelta(days=cfg.refresh_days)
            due = session.execute(
                select(Instrument.symbol)
                .where(Instrument.symbol.in_(symbols),
                       or_(Instrument.classified_at.is_(None),
                           Instrument.classified_at < stale_before))
                .order_by(Instrument.classified_at.asc().nulls_first(), Instrument.symbol)
                .limit(cfg.max_per_run)
            ).scalars().all()  # fmt: skip
            symbols = list(due)
        mapped: dict[str, str] = {}
        unmapped: dict[str, str] = {}
        failed: dict[str, str] = {}
        for sym in symbols:
            out = classify_symbol(session, ctx.router, sym, ctx.config.industries, now=now)
            session.commit()
            if not out.fetched:
                failed[sym] = out.message
            elif out.sector is None:
                unmapped[sym] = f"{out.source}: {out.label}"
            else:
                mapped[sym] = out.sector
    finally:
        session.close()
    return JobOutcome(len(mapped), {"mapped": mapped, "unmapped": unmapped,
                                    "failed": dict(list(failed.items())[:50])})  # fmt: skip
