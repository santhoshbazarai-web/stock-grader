"""Command line for jobs.

    python -m app.jobs list
    python -m app.jobs run eod_prices --symbols TCS,INFY [--full]
    python -m app.jobs run nse_bhavcopy --date 2024-03-28
    python -m app.jobs run shareholding --force
    python -m app.jobs verify-adjustment --symbol INFY
    python -m app.jobs xbrl-inspect path/to/results.xml   # check the XBRL element mapping
    python -m app.jobs xbrl-reparse [--symbols TCS]       # re-apply xbrl_map.yaml to cached files
    python -m app.jobs xbrl-coverage --symbols TCS,INFY   # years parsed per statement
    python -m app.jobs pdf-inspect report.pdf [--fy 2014] # what the annual-report reader finds
    python -m app.jobs pdf-reparse [--symbols TCS]        # re-read cached annual reports
    python -m app.jobs pipeline-worker                    # only the on-demand pipeline loop

Exit codes: 0 success/skipped, 1 failed, 2 unknown or not-yet-implemented job.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.core.config import JobName
from app.data.adjust import largest_overnight_gap
from app.db.enums import JobStatus
from app.db.models import CorporateAction, Instrument, PriceDaily
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobContext, JobNotImplementedError, JobOptions, run_job

ContextFactory = Callable[[], JobContext]


def _default_context() -> JobContext:
    from app.core.config import get_config
    from app.core.logging import configure_logging
    from app.core.settings import get_settings
    from app.jobs.context import build_context

    settings = get_settings()
    configure_logging(settings.log_level)
    return build_context(settings, get_config())


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.jobs", description="Stock Grader jobs")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list jobs, schedules and status")
    run = sub.add_parser("run", help="run one job now")
    run.add_argument("job", help="job name (see `list`)")
    run.add_argument("--symbols", action="append", default=None, help="comma-separated; repeatable")
    run.add_argument("--full", action="store_true", help="full-history backfill")
    run.add_argument("--date", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    run.add_argument("--force", action="store_true", help="ignore filing-season windows")
    verify = sub.add_parser(
        "verify-adjustment", help="check stored prices of a symbol around its splits/bonuses"
    )
    verify.add_argument("--symbol", required=True)
    inspect = sub.add_parser(
        "xbrl-inspect", help="show what the results XBRL parser reads from a filing (no DB)"
    )
    inspect.add_argument("path", type=Path, help="an NSE/BSE results XBRL document (.xml)")
    reparse = sub.add_parser(
        "xbrl-reparse",
        help="re-parse cached results XBRL documents with the current xbrl_map.yaml (no network)",
    )
    reparse.add_argument("--symbols", action="append", default=None, help="comma-separated")
    cov = sub.add_parser(
        "xbrl-coverage", help="earliest / latest fiscal year parsed per statement (P&L, BS, CF)"
    )
    cov.add_argument("--symbols", action="append", required=True, help="comma-separated")
    pdf = sub.add_parser(
        "pdf-inspect", help="show what the annual-report reader finds in a PDF (no DB)"
    )
    pdf.add_argument("path", type=Path, help="an annual report (.pdf or the exchange's .zip)")
    pdf.add_argument(
        "--fy", type=int, default=None, help="the report's fiscal year (dates undated columns)"
    )
    pdf_re = sub.add_parser(
        "pdf-reparse",
        help="re-read cached annual reports with the current pdf_labels.yaml (no network)",
    )
    pdf_re.add_argument("--symbols", action="append", default=None, help="comma-separated")
    sub.add_parser(
        "pipeline-worker",
        help="run queued on-demand pipeline runs as they arrive (the worker does this too)",
    )
    return p


def xbrl_coverage(ctx: JobContext, symbols: Sequence[str]) -> int:
    from app.data.coverage import coverage
    from app.jobs.common import normalise_symbols

    session = ctx.session_factory()
    try:
        rows = coverage(session, normalise_symbols(symbols),
                        ctx.config.providers.nse.results.default_fy_end_month)  # fmt: skip
    finally:
        session.close()
    print(f"{'symbol':<14} {'basis':<13} {'stmt':<4} {'years':<14} count")
    for r in rows:
        print(r.row())
    return 0


def xbrl_reparse(ctx: JobContext, symbols: Sequence[str] | None) -> int:
    """Re-apply the current tag map to every cached document (``result_filings.raw_path``)."""
    from app.data.raw_store import RawStoreError
    from app.data.results_ingest import ingest
    from app.db.enums import FilingStatus
    from app.db.models import ResultFiling
    from app.jobs.common import normalise_symbols

    if ctx.raw_store is None:
        print("no raw-file cache configured", file=sys.stderr)
        return 1
    cfg = ctx.config.providers.nse.results
    session = ctx.session_factory()
    try:
        q = (
            select(ResultFiling, Instrument.symbol)
            .join(Instrument, Instrument.id == ResultFiling.instrument_id)
            .where(ResultFiling.raw_path.is_not(None))
            .order_by(ResultFiling.period_end.nulls_first(), ResultFiling.id)
        )
        if symbols:
            q = q.where(Instrument.symbol.in_(normalise_symbols(symbols)))
        rows = session.execute(q).all()
        counts = {FilingStatus.PARSED: 0, FilingStatus.FAILED: 0}
        for row, symbol in rows:
            try:
                content = ctx.raw_store.read(row.raw_path or "")
            except (OSError, RawStoreError) as exc:
                print(f"{symbol} {row.document}: cached file unreadable ({exc})", file=sys.stderr)
                counts[FilingStatus.FAILED] += 1
                continue
            ingest(session, row, symbol=symbol, content=content, cfg=cfg, gaps=ctx.gaps,
                   now=ctx.now())  # fmt: skip
            counts[row.status] = counts.get(row.status, 0) + 1
            if row.status is FilingStatus.FAILED:
                print(f"{symbol} {row.document}: {row.error}", file=sys.stderr)
            session.commit()
    finally:
        session.close()
    parsed, failed = counts[FilingStatus.PARSED], counts[FilingStatus.FAILED]
    print(f"re-parsed {len(rows)} cached filing(s): {parsed} stored, {failed} failed")
    return 1 if failed else 0


def xbrl_inspect(path: Path) -> int:
    from app.core.config import load_config
    from app.core.settings import config_dir_from_env
    from app.data.xbrl import XbrlFormatError, describe

    try:
        cfg = load_config(config_dir_from_env()).providers.nse.results
        print(describe(path.read_bytes(), cfg))
    except (OSError, XbrlFormatError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    return 0


def pdf_inspect(path: Path, fy: int | None) -> int:
    from app.core.config import load_config
    from app.core.settings import config_dir_from_env
    from app.data.annual_report import AnnualReportError, describe, extract_annual_report
    from app.fundamentals.pdf_labels import get_pdf_labels
    from app.fundamentals.xbrl_map import get_xbrl_map

    try:
        nse = load_config(config_dir_from_env()).providers.nse
        fy_end = date(fy, nse.results.default_fy_end_month, 1) if fy else None
        if fy_end is not None:
            fy_end = (pd.Timestamp(fy_end) + pd.offsets.MonthEnd(0)).date()
        out = extract_annual_report(
            path.read_bytes(), cfg=nse.annual_reports, rounding_levels=nse.results.rounding_levels,
            labels=get_pdf_labels(), xmap=get_xbrl_map(), fiscal_year_end=fy_end,
        )  # fmt: skip
    except (OSError, AnnualReportError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    print(describe(out, nse.annual_reports.confidence.auto_accept))
    return 0


def pdf_reparse(ctx: JobContext, symbols: Sequence[str] | None) -> int:
    """Re-read every cached annual report (``annual_reports.raw_path``); review decisions are
    kept."""
    from app.data.annual_report_store import ingest_report
    from app.data.raw_store import RawStoreError
    from app.db.enums import FilingStatus
    from app.db.models import AnnualReport
    from app.fundamentals.pdf_labels import get_pdf_labels
    from app.fundamentals.xbrl_map import get_xbrl_map
    from app.jobs.common import normalise_symbols

    if ctx.raw_store is None:
        print("no raw-file cache configured", file=sys.stderr)
        return 1
    session = ctx.session_factory()
    failed = 0
    try:
        q = (
            select(AnnualReport, Instrument.symbol)
            .join(Instrument, Instrument.id == AnnualReport.instrument_id)
            .where(AnnualReport.raw_path.is_not(None))
            .order_by(AnnualReport.fiscal_year.nulls_first(), AnnualReport.id)
        )
        if symbols:
            q = q.where(Instrument.symbol.in_(normalise_symbols(symbols)))
        rows = session.execute(q).all()
        for row, symbol in rows:
            try:
                content = ctx.raw_store.read(row.raw_path or "")
            except (OSError, RawStoreError) as exc:
                print(f"{symbol} {row.document}: cached file unreadable ({exc})", file=sys.stderr)
                failed += 1
                continue
            ingest_report(session, row, content=content, nse=ctx.config.providers.nse,
                          labels=get_pdf_labels(), xmap=get_xbrl_map(), now=ctx.now())  # fmt: skip
            if row.status is FilingStatus.FAILED:
                failed += 1
                print(f"{symbol} {row.document}: {row.error}", file=sys.stderr)
            session.commit()
    finally:
        session.close()
    print(f"re-read {len(rows)} cached annual report(s): {len(rows) - failed} read, "
          f"{failed} failed")  # fmt: skip
    return 1 if failed else 0


def pipeline_worker(ctx: JobContext) -> int:
    """The worker's pipeline thread on its own (development, e2e): Ctrl+C stops it."""
    import threading

    from app.pipeline.runner import worker_loop

    stop = threading.Event()
    print("pipeline worker: waiting for runs (Ctrl+C to stop)", flush=True)
    try:
        worker_loop(ctx, stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def _list(ctx: JobContext) -> int:
    for name in JobName:
        spec = REGISTRY[name]
        state = "ready" if spec.fn else f"pending {spec.pending_phase}"
        print(f"{name:<20} {ctx.config.jobs.schedules[name]:<22} {state:<14} {spec.description}")
    return 0


def _run(ctx: JobContext, args: argparse.Namespace) -> int:
    try:
        name = JobName(args.job)
    except ValueError:
        print(f"unknown job {args.job!r}; try `list`", file=sys.stderr)
        return 2
    options = JobOptions(
        symbols=tuple(args.symbols) if args.symbols else None,
        full=args.full,
        day=args.date,
        force=args.force,
    )
    try:
        record = run_job(REGISTRY[name], ctx, options)
    except JobNotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"{name}: {record.status} (run {record.run_id})")
    if record.outcome is not None:
        print(f"  rows written: {record.outcome.rows_written}")
        for key, value in record.outcome.details.items():
            print(f"  {key}: {value}")
    if record.error:
        print(f"  error: {record.error}", file=sys.stderr)
    return 1 if record.status is JobStatus.FAILED else 0


def verify_adjustment(ctx: JobContext, symbol: str) -> int:
    """Print raw vs adjusted closes around each split/bonus and the largest overnight move of
    each series. A correct adjustment removes the ex-date jump from the adjusted series."""
    session = ctx.session_factory()
    try:
        iid = session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))
        if iid is None:
            print(f"{symbol}: not in instruments", file=sys.stderr)
            return 1
        prices = pd.DataFrame(
            session.execute(
                select(PriceDaily.date, PriceDaily.close, PriceDaily.adj_close)
                .where(PriceDaily.instrument_id == iid)
                .order_by(PriceDaily.date)
            ).all(),
            columns=["date", "close", "adj_close"],
        ).set_index("date")
        actions = session.execute(
            select(
                CorporateAction.ex_date,
                CorporateAction.action_type,
                CorporateAction.ratio_old,
                CorporateAction.ratio_new,
            )
            .where(
                CorporateAction.instrument_id == iid,
                CorporateAction.action_type.in_(["split", "bonus"]),
            )
            .order_by(CorporateAction.ex_date)
        ).all()
    finally:
        session.close()
    if prices.empty:
        print(f"{symbol}: no stored prices; run eod_prices first", file=sys.stderr)
        return 1
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index), name="date")
    print(f"{symbol}: {len(prices)} bars, {len(actions)} split/bonus action(s)")
    for ex_date, kind, old, new in actions:
        ex = pd.Timestamp(ex_date)
        window = prices.loc[ex - pd.Timedelta(days=5) : ex + pd.Timedelta(days=5)]
        print(f"\n{kind} {old:g}:{new:g} ex {ex_date}")
        print(window.to_string())
    raw_gap = largest_overnight_gap(prices["close"])
    adj_gap = largest_overnight_gap(prices["adj_close"].dropna())
    print(f"\nlargest overnight move: raw {raw_gap:.1%}, adjusted {adj_gap:.1%}")
    return 0


def main(argv: Sequence[str] | None = None, context_factory: ContextFactory | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "xbrl-inspect":
        return xbrl_inspect(args.path)
    if args.command == "pdf-inspect":
        return pdf_inspect(args.path, args.fy)
    ctx = (context_factory or _default_context)()
    if args.command == "list":
        return _list(ctx)
    if args.command == "run":
        return _run(ctx, args)
    if args.command == "xbrl-reparse":
        return xbrl_reparse(ctx, args.symbols)
    if args.command == "xbrl-coverage":
        return xbrl_coverage(ctx, args.symbols)
    if args.command == "pdf-reparse":
        return pdf_reparse(ctx, args.symbols)
    if args.command == "pipeline-worker":
        return pipeline_worker(ctx)
    return verify_adjustment(ctx, args.symbol)
