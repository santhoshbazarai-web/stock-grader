"""Command line for jobs.

    python -m app.jobs list
    python -m app.jobs run eod_prices --symbols TCS,INFY [--full]
    python -m app.jobs run nse_bhavcopy --date 2024-03-28
    python -m app.jobs run shareholding --force
    python -m app.jobs verify-adjustment --symbol INFY
    python -m app.jobs xbrl-inspect path/to/results.xml   # check the XBRL element mapping

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
    return p


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
    ctx = (context_factory or _default_context)()
    if args.command == "list":
        return _list(ctx)
    if args.command == "run":
        return _run(ctx, args)
    return verify_adjustment(ctx, args.symbol)
