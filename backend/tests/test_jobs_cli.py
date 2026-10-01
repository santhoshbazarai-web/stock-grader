"""`python -m app.jobs` CLI and the worker's scheduler."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core.config import JobName, load_config
from app.db.models import JobRun
from app.jobs.cli import main
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobContext
from app.jobs.worker import build_scheduler
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, action, ohlcv


def cli(env: Env, *argv: str) -> int:
    return main(list(argv), context_factory=lambda: env.ctx)


def test_list(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(env, "list") == 0
    out = capsys.readouterr().out
    assert "eod_prices" in out and "15 18 * * mon-fri" in out
    assert "alerts_intraday" in out and "*/5 9-15 * * mon-fri" in out
    assert "pending" not in out  # every job is implemented


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


@pytest.mark.parametrize(("job", "message"), [("nope", "unknown job"), ("alerts_intraday", "P99")])
def test_unknown_or_pending_job(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    job: str,
    message: str,
) -> None:
    from app.jobs import cli as cli_module
    from app.jobs.runner import JobSpec

    pending = JobSpec(JobName.ALERTS_INTRADAY, "later", pending_phase="P99")
    monkeypatch.setitem(cli_module.REGISTRY, JobName.ALERTS_INTRADAY, pending)
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
    assert {"technicals", "valuation_scores", "refresh_queue", "alerts_intraday"} <= set(jobs)

    tz = ZoneInfo("Asia/Kolkata")
    trigger = jobs["eod_prices"].trigger
    expected = CronTrigger.from_crontab(config.jobs.schedules[JobName.EOD_PRICES], timezone=tz)
    assert str(trigger) == str(expected)
    fire = trigger.get_next_fire_time(None, pd.Timestamp("2024-06-14 12:00", tz=tz))
    assert (fire.hour, fire.minute, fire.weekday()) == (18, 15, 4)  # Friday 18:15 IST
    assert jobs["eod_prices"].max_instances == 1 and jobs["eod_prices"].coalesce is True


def test_xbrl_inspect_needs_no_database_or_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FERNET_KEY", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    def no_context() -> JobContext:
        raise AssertionError("xbrl-inspect must not build a job context")

    doc = Path(__file__).parent / "fixtures" / "xbrl" / "acme_q4fy24_consolidated.xml"
    assert main(["xbrl-inspect", str(doc)], context_factory=no_context) == 0
    out = capsys.readouterr().out
    assert "OneD" in out and "used: quarter" in out and "used: balance_sheet" in out
    assert "dimensional, ignored" in out
    assert "revenue                      4,800.00" in out
    assert "xbrl_map.yaml version 2" in out
    assert "Liabilities  [OneI]" in out  # an unmapped element, listed for review
    assert "sum:ind_as:CostOfMaterialsConsumed+PurchasesOfStockInTrade" in out  # cogs lines

    bad = tmp_path / "bad.xml"
    bad.write_bytes(b"<html/>")
    assert main(["xbrl-inspect", str(bad)], context_factory=no_context) == 1
    assert "not an XBRL instance" in capsys.readouterr().err
