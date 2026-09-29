"""``backtests`` job: runs queued ``POST /api/backtests`` requests (SPEC §11), one at a time.

A request is claimed with ``SELECT … FOR UPDATE SKIP LOCKED``, so two workers never run the same
one. While it runs, ``results.progress`` shows rebalances done / total; a failure stores the
error on the row. The API only queues; the work happens here.
"""

import logging
from datetime import date
from typing import Any

from sqlalchemy import select, update

from app.backtest.runner import load_world, run_backtest
from app.db.enums import BacktestStatus
from app.db.models import Backtest
from app.jobs.runner import JobContext, JobOptions, JobOutcome

logger = logging.getLogger(__name__)


def _claim(ctx: JobContext) -> int | None:
    session = ctx.session_factory()
    try:
        row = session.scalars(
            select(Backtest)
            .where(Backtest.status == BacktestStatus.QUEUED)
            .order_by(Backtest.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return None
        row.status = BacktestStatus.RUNNING
        session.commit()
        return row.id
    finally:
        session.close()


def _run_one(ctx: JobContext, backtest_id: int) -> None:
    session = ctx.session_factory()
    try:
        b = session.get(Backtest, backtest_id)
        if b is None:
            return
        p: dict[str, Any] = b.params

        def progress(done: int, total: int) -> None:
            session.execute(
                update(Backtest)
                .where(Backtest.id == backtest_id)
                .values(results={"progress": {"done": done, "total": total}})
            )
            session.commit()

        symbols = [s.upper() for s in p["symbols"]] if p.get("symbols") else None
        world, label = load_world(session, ctx.config, symbols=symbols)
        results = run_backtest(
            world,
            ctx.config,
            grades=p["grades"],
            zones=p["zones"],
            holding_days=int(p["holding_days"]),
            start=date.fromisoformat(p["start"]),
            end=date.fromisoformat(p["end"]),
            symbols=symbols,
            benchmark_label=label,
            progress=progress,
        )
        session.execute(
            update(Backtest)
            .where(Backtest.id == backtest_id)
            .values(status=BacktestStatus.DONE, results=results, error=None)
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("backtest %s failed", backtest_id)
        session.execute(
            update(Backtest)
            .where(Backtest.id == backtest_id)
            .values(status=BacktestStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
        )
        session.commit()
        raise
    finally:
        session.close()


def backtests(ctx: JobContext, options: JobOptions) -> JobOutcome:
    done: list[int] = []
    failed: dict[int, str] = {}
    while (bid := _claim(ctx)) is not None:
        try:
            _run_one(ctx, bid)
            done.append(bid)
        except Exception as exc:
            failed[bid] = f"{type(exc).__name__}: {exc}"[:300]
    if not done and not failed:
        return JobOutcome(skipped_reason="no queued backtests")
    return JobOutcome(len(done), {"done": done, "failed": failed})
