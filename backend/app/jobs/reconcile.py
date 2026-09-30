"""reconcile: cross-source checks of each stock's latest periods (SPEC v0.2 §3.9).

Runs as the pipeline's ``reconcile`` step for one stock, and nightly for the universe stocks
with a results filing or annual report stored since the last successful run (``--symbols``
or ``--force``: those / all universe stocks).
"""

import logging
from typing import Any

from sqlalchemy import func, select

from app.core.config import Dataset, JobName, Provider
from app.data.providers.base import FundamentalsProvider
from app.data.reconcile_store import (
    add_long_frame,
    add_wide_frame,
    collect_filed,
    save_findings,
)
from app.db.enums import JobStatus
from app.db.models import AnnualReport, Instrument, JobRun, ResultFiling
from app.fundamentals.reconcile import label, reconcile
from app.fundamentals.xbrl_map import get_xbrl_map
from app.jobs.common import ensure_instruments, universe
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)


def reconcile_symbol(ctx: JobContext, symbol: str) -> dict[str, Any]:
    """Collect every source's figures, compare, and store the issues (committed). Returns a
    summary: compared, open, new, resolved, the sources used and the ones unavailable."""
    cfg = ctx.config.jobs.reconciliation
    session = ctx.session_factory()
    try:
        iid = ensure_instruments(session, [symbol])[symbol]
        session.commit()
        got = collect_filed(session, iid, cfg, get_xbrl_map())
    finally:
        session.close()
    unavailable: dict[str, str] = {}
    if not got.periods:
        return {"compared": 0, "open": 0, "new": 0, "resolved": 0, "sources": [],
                "unavailable": {}, "note": "no exchange-filed figures to check"}  # fmt: skip

    excluded: dict[str, list[str]] = {str(k): v for k, v in cfg.exclude.items()}

    def items_for(source: str) -> list[str]:
        return [i for i in cfg.items if i not in excluded.get(source, [])]

    if "yfinance" in cfg.sources:
        for group, call in (("year", "annual"), ("quarter", "quarterly")):
            if group == "quarter" and not cfg.quarters:
                continue
            res = ctx.router.fetch(
                Dataset.FIN_ANNUAL if group == "year" else Dataset.FIN_QUARTERLY,
                FundamentalsProvider,
                lambda p, c=call: getattr(p, c)(symbol),  # type: ignore[misc]
                symbol=symbol, providers=[Provider.YFINANCE], record_gap=False,
            )  # fmt: skip
            if res.data is None:
                unavailable[f"yfinance {call}"] = "; ".join(res.reasons)[:300]
            else:
                add_wide_frame(got, res.data, "yfinance", group, items_for("yfinance"))
    if "market_lens" in cfg.sources and ctx.config.providers.market_lens.enabled:
        res = ctx.router.reference_financials(Provider.MARKET_LENS, symbol)
        if res.data is None:
            unavailable["market_lens"] = "; ".join(res.reasons)[:300]
        else:
            add_long_frame(got, res.data, "market_lens", items_for("market_lens"))

    result = reconcile(
        got.values, sources=cfg.sources, tolerance_rel=cfg.tolerance_rel,
        min_diff_inr=cfg.min_diff_inr, unit_factors=cfg.unit_factors,
        other_basis=got.other_basis, earlier_versions=got.earlier_versions,
        basis=got.basis.value,
    )  # fmt: skip
    session = ctx.session_factory()
    try:
        counts = save_findings(session, iid, got.basis, result, ctx.now())
        session.commit()
    finally:
        session.close()
    used = sorted({s for per in got.values.values() for s in per})
    return {"compared": len(result.compared), **counts, "sources": [label(s) for s in used],
            "unavailable": unavailable}  # fmt: skip


def _changed_since_last_run(ctx: JobContext, symbols: list[str]) -> list[str]:
    session = ctx.session_factory()
    try:
        last = session.scalar(
            select(func.max(JobRun.started_at)).where(JobRun.job_name == JobName.RECONCILE.value,
                                                      JobRun.status == JobStatus.SUCCESS)
        )  # fmt: skip
        if last is None:
            return symbols
        changed = set(session.scalars(
            select(Instrument.symbol)
            .join(ResultFiling, ResultFiling.instrument_id == Instrument.id)
            .where(ResultFiling.parsed_at >= last)
        )) | set(session.scalars(
            select(Instrument.symbol)
            .join(AnnualReport, AnnualReport.instrument_id == Instrument.id)
            .where(AnnualReport.parsed_at >= last)
        ))  # fmt: skip
    finally:
        session.close()
    return [s for s in symbols if s in changed]


def reconcile_job(ctx: JobContext, options: JobOptions) -> JobOutcome:
    symbols = universe(ctx, options)
    if not options.symbols and not options.force:
        symbols = _changed_since_last_run(ctx, symbols)
    summary: dict[str, Any] = {"symbols": len(symbols), "with_open_issues": [],
                               "failed": {}, "unavailable": {}}  # fmt: skip
    total_new = 0
    for symbol in symbols:
        try:
            out = reconcile_symbol(ctx, symbol)
        except Exception as exc:  # one stock's bad data must not stop the rest
            logger.exception("reconcile %s failed", symbol)
            summary["failed"][symbol] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        total_new += out["new"]
        if out["open"]:
            summary["with_open_issues"].append(symbol)
        if out["unavailable"]:
            summary["unavailable"][symbol] = list(out["unavailable"])
    summary["with_open_issues"] = summary["with_open_issues"][:100]
    summary["unavailable"] = dict(list(summary["unavailable"].items())[:50])
    return JobOutcome(total_new, summary)
