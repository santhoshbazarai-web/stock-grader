"""`python -m app.jobs` CLI and the worker's scheduler."""

from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core.config import JobName, load_config
from app.db.models import JobRun
from app.jobs.cli import main
from app.jobs.registry import REGISTRY
from app.jobs.worker import build_scheduler
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, action, ohlcv


def cli(env: Env, *argv: str) -> int:
    return main(list(argv), context_factory=lambda: env.ctx)


def test_list(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(env, "list") == 0
    out = capsys.readouterr().out
    assert "eod_prices" in out and "15 18 * * mon-fri" in out
    assert "pending P10/P11" in out


def test_run_with_symbols(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.prices.bars["TCS"] = ohlcv({"2024-06-13": 3800, "2024-06-14": 3810})
    env.prices.bars["INFY"] = ohlcv({"2024-06-14": 1500})
    assert cli(env, "run", "eod_prices", "--symbols", "tcs,INFY", "--symbols", "TCS") == 0
    out = capsys.readouterr().out
    assert "eod_prices: success" in out and "rows written: 3" in out
    with env.session() as s:
        run = s.scalars(select(JobRun)).one()
    assert run.params["symbols"] == ["tcs,INFY", "TCS"]
    assert sorted(r[0] for r in env.prices.requests) == ["INFY", "TCS"]


def test_run_failed_job_exit_code(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(env, "run", "nse_bhavcopy", "--date", "2024-06-14") == 1
    assert "no delivery data" in capsys.readouterr().err


@pytest.mark.parametrize(("job", "message"), [("nope", "unknown job"), ("valuation_scores", "P10")])
def test_unknown_or_pending_job(
    env: Env, capsys: pytest.CaptureFixture[str], job: str, message: str
) -> None:
    assert cli(env, "run", job) == 2
    assert message in capsys.readouterr().err


def test_verify_adjustment(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.prices.bars["SAMPLE"] = ohlcv(
        {"2024-06-03": 1000, "2024-06-04": 1000, "2024-06-05": 500, "2024-06-06": 505}
    )
    env.nse.actions["SAMPLE"] = pd.DataFrame([action("2024-06-05", "bonus", 1, 2)])
    cli(env, "run", "eod_prices", "--symbols", "SAMPLE")
    capsys.readouterr()

    assert cli(env, "verify-adjustment", "--symbol", "sample") == 0
    out = capsys.readouterr().out
    assert "bonus 1:2 ex 2024-06-05" in out
    assert "largest overnight move: raw 50.0%, adjusted 1.0%" in out


def test_verify_adjustment_unknown_symbol(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(env, "verify-adjustment", "--symbol", "NOPE") == 1


def test_scheduler_registers_ready_jobs_with_config_triggers(env: Env) -> None:
    config = load_config(REPO_CONFIG_DIR)
    scheduler = build_scheduler(env.ctx, config)
    jobs = {j.id: j for j in scheduler.get_jobs()}
    ready = {str(n) for n, spec in REGISTRY.items() if spec.fn is not None}
    assert set(jobs) == ready
    assert {"valuation_scores", "alerts_intraday"}.isdisjoint(jobs)
    assert "technicals" in jobs

    tz = ZoneInfo("Asia/Kolkata")
    trigger = jobs["eod_prices"].trigger
    expected = CronTrigger.from_crontab(config.jobs.schedules[JobName.EOD_PRICES], timezone=tz)
    assert str(trigger) == str(expected)
    fire = trigger.get_next_fire_time(None, pd.Timestamp("2024-06-14 12:00", tz=tz))
    assert (fire.hour, fire.minute, fire.weekday()) == (18, 15, 4)  # Friday 18:15 IST
    assert jobs["eod_prices"].max_instances == 1 and jobs["eod_prices"].coalesce is True
