"""On-demand pipeline (SPEC v0.2 §3.7): steps, optional-step failures as data gaps, required
failures, resume after a crash, freshness, dedup, and the SSE progress stream."""

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_stream_sessions
from app.core.config import get_config
from app.db.enums import PipelineStatus
from app.db.models import Instrument, PipelineRun, PriceDaily
from app.devtools.synthetic import seed_company, seed_index
from app.pipeline import runner
from app.pipeline.runner import (
    STEPS,
    claim_next,
    execute,
    new_steps,
    report_freshness,
    run_pending,
    start_run,
)
from app.reports.service import latest_report
from tests.api_support import app_client
from tests.jobs_support import NOW, Env

STEP_NAMES = [s.name for s in STEPS]


@pytest.fixture
def seeded(env: Env) -> Env:
    with env.session() as s:
        seed_index(s, env.ctx.config.jobs.universe_index)
        seed_company(s, "SYNTH")
        s.commit()
    return env


def queue(env: Env, symbol: str, *, force: bool = True) -> int:
    with env.session() as s:
        run, _ = start_run(s, symbol, trigger="user", force=force,
                           cfg=env.ctx.config.jobs.pipeline, now=NOW)  # fmt: skip
        assert run is not None
        s.commit()
        return run.id


def load(env: Env, run_id: int) -> PipelineRun:
    with env.session() as s:
        run = s.get(PipelineRun, run_id)
        assert run is not None
        return run


def steps(run: PipelineRun) -> dict[str, tuple[str, str | None]]:
    return {s["name"]: (s["status"], s["message"]) for s in run.steps}


def test_steps_follow_the_spec_order() -> None:
    assert STEP_NAMES == [
        "symbol", "indianapi", "prices", "corporate_actions", "filings_index", "xbrl_parse",
        "pdf_gap_fill", "shareholding_events", "reconcile", "metrics", "valuation", "technical",
        "scoring", "report",
    ]  # fmt: skip
    required = {s.name for s in STEPS if not s.optional}
    # metrics fails loudly but optionally: the report (prices, technicals) is still built
    assert required == {"symbol", "prices", "valuation", "scoring", "report"}


def test_run_with_failing_optional_steps_still_stores_a_report(seeded: Env) -> None:
    run_id = queue(seeded, "SYNTH")
    assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.DONE)]
    run = load(seeded, run_id)
    st = steps(run)
    assert run.status is PipelineStatus.DONE and run.attempts == 1 and run.finished_at
    # the fake providers have no data for SYNTH: stored bars are used, sources are warnings
    assert st["prices"][0] == "warning" and "using stored bars" in (st["prices"][1] or "")
    assert st["filings_index"] == ("warning", "NSE results list unavailable")
    # no exchange-filed figures for SYNTH: nothing to reconcile
    assert st["reconcile"] == ("ok", "no exchange-filed figures to check")
    assert all(st[n][0] == "ok" for n in ("valuation", "scoring", "report"))
    assert st["scoring"][1] and st["scoring"][1].startswith("grade ")
    with seeded.session() as s:
        report = latest_report(s, "SYNTH")
    assert report is not None and run.report_as_of == report.as_of
    assert "pipeline filings index: NSE results list unavailable" in report.data_gaps
    # the XBRL step says which years it found per statement
    assert st["xbrl_parse"][1] and "Years found: " in st["xbrl_parse"][1]
    assert st["metrics"][1] and "fiscal year(s) with a P&L" in st["metrics"][1]


def test_metrics_fails_loudly_without_a_pl_but_the_report_is_built(seeded: Env) -> None:
    from sqlalchemy import delete

    from app.db.models import FinAnnual, FinQuarterly

    with seeded.session() as s:
        s.execute(delete(FinAnnual))
        s.execute(delete(FinQuarterly))
        s.commit()
    run_id = queue(seeded, "SYNTH")
    assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.DONE)]
    st = steps(load(seeded, run_id))
    status, msg = st["metrics"]
    assert status == "failed" and msg
    assert msg.startswith("no fundamental metrics: no fiscal year with a P&L (revenue / PAT)")
    assert "[P&L 0 yr, BS 0 yr, CF 0 yr (consolidated)]" in msg and "Settings → Uploads" in msg
    assert st["report"][0] == "ok"  # still built from prices and technicals


def test_required_step_failure_fails_the_run(seeded: Env) -> None:
    run_id = queue(seeded, "NOPRICES")
    assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.FAILED)]
    run = load(seeded, run_id)
    st = steps(run)
    assert st["prices"][0] == "failed" and "no prices for NOPRICES" in (st["prices"][1] or "")
    after = STEP_NAMES[STEP_NAMES.index("prices") + 1 :]
    assert all(st[n] == ("skipped", "an earlier step failed") for n in after)
    assert run.error and run.error.startswith("Prices: no prices")
    with seeded.session() as s:
        assert latest_report(s, "NOPRICES") is None


def test_an_interrupted_run_resumes_from_its_first_unfinished_step(
    seeded: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = queue(seeded, "SYNTH")
    done = new_steps()
    for s in done[:5]:
        s.update(status="ok", message="done before the crash")
    done[5].update(status="running")  # the worker died during the XBRL step
    stale = NOW - timedelta(seconds=seeded.ctx.config.jobs.pipeline.stale_after_s + 1)
    with seeded.session() as s:
        s.execute(update(PipelineRun).where(PipelineRun.id == run_id).values(
            steps=done, status=PipelineStatus.RUNNING, attempts=1, heartbeat_at=stale))  # fmt: skip
        s.commit()
    called: list[str] = []
    for step in STEPS:
        original = step.fn

        def spy(ctx: Any, state: Any, _name: str = step.name, _fn: Any = original) -> Any:
            called.append(_name)
            return _fn(ctx, state)

        monkeypatch.setitem(runner.STEP_BY_NAME, step.name,
                            runner.StepDef(step.name, step.label, step.optional, spy))  # fmt: skip
    assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.DONE)]
    assert called == STEP_NAMES[5:]
    run = load(seeded, run_id)
    assert run.attempts == 2 and steps(run)["prices"] == ("ok", "done before the crash")


def test_a_live_run_is_not_taken_over_and_a_hopeless_one_gives_up(seeded: Env) -> None:
    cfg = seeded.ctx.config.jobs.pipeline
    run_id = queue(seeded, "SYNTH")
    with seeded.session() as s:
        s.execute(update(PipelineRun).where(PipelineRun.id == run_id)
                  .values(status=PipelineStatus.RUNNING, attempts=1, heartbeat_at=NOW))  # fmt: skip
        s.commit()
        assert claim_next(s, cfg, NOW + timedelta(seconds=10)) is None  # heartbeat is fresh
        s.execute(update(PipelineRun).where(PipelineRun.id == run_id)
                  .values(attempts=cfg.max_attempts))  # fmt: skip
        s.commit()
        later = NOW + timedelta(seconds=cfg.stale_after_s + 1)
        assert claim_next(s, cfg, later) is None
    run = load(seeded, run_id)
    assert run.status is PipelineStatus.FAILED and "giving up" in (run.error or "")


def test_fresh_report_starts_no_run_and_dedup(seeded: Env) -> None:
    cfg = seeded.ctx.config.jobs.pipeline
    run_id = queue(seeded, "SYNTH")
    assert queue(seeded, "SYNTH") == run_id  # asking again joins the active run
    run_pending(seeded.ctx)
    with seeded.session() as s:
        now = datetime.now(UTC)
        fresh = report_freshness(s, "SYNTH", cfg, now)
        assert fresh.fresh, fresh.reason
        run, _ = start_run(s, "SYNTH", trigger="user", force=False, cfg=cfg, now=now)
        assert run is None  # served instantly
        # too old
        old = report_freshness(s, "SYNTH", cfg, now + timedelta(hours=cfg.max_report_age_hours + 1))
        assert not old.fresh and "built more than" in old.reason
        # a newer price bar than the report
        rep_as_of = load(seeded, run_id).report_as_of
        assert rep_as_of is not None
        iid = s.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
        s.add(PriceDaily(instrument_id=iid, date=rep_as_of + timedelta(days=7), open=1, high=1,
                         low=1, close=1, volume=1, source="fyers", fetched_at=NOW))  # fmt: skip
        s.flush()
        stale = report_freshness(s, "SYNTH", cfg, now)
        assert not stale.fresh and "newer than the report" in stale.reason
        s.rollback()
        assert not report_freshness(s, "NEWCO", cfg, now).fresh


def test_execute_refuses_nothing_twice(seeded: Env) -> None:
    run_id = queue(seeded, "SYNTH")
    assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.DONE)]
    assert run_pending(seeded.ctx) == []  # nothing left to claim
    assert execute(seeded.ctx, run_id) is PipelineStatus.DONE  # all steps finished: no-op


# ───────────────────────── API + SSE ─────────────────────────


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


def _run_row(version: int, status: PipelineStatus, done: int) -> PipelineRun:
    st = new_steps()
    for s in st[:done]:
        s.update(status="ok", message="fine")
    return PipelineRun(id=7, symbol="SYNTH", trigger="user", force=False, status=status,
                       steps=st, version=version, attempts=1, error=None, report_as_of=None,
                       created_at=NOW, started_at=NOW, finished_at=None)  # fmt: skip


def test_sse_streams_each_change_then_ends(client: TestClient, db: Session) -> None:
    db.add(PipelineRun(id=7, symbol="SYNTH", trigger="user", status=PipelineStatus.QUEUED,
                       steps=new_steps(), version=1))  # fmt: skip
    db.flush()
    states = iter([_run_row(1, PipelineStatus.QUEUED, 0), _run_row(1, PipelineStatus.QUEUED, 0),
                   _run_row(2, PipelineStatus.RUNNING, 3),
                   _run_row(3, PipelineStatus.DONE, 13)])  # fmt: skip

    class Scripted:
        def get(self, *_: Any, **__: Any) -> PipelineRun:
            return next(states)

    cfg = get_config()
    fast = cfg.model_copy(update={"jobs": cfg.jobs.model_copy(update={
        "pipeline": cfg.jobs.pipeline.model_copy(update={"sse_poll_s": 0.01})})})  # fmt: skip
    app = client.app
    app.dependency_overrides[get_stream_sessions] = lambda: (  # type: ignore[attr-defined]
        lambda: contextlib.nullcontext(Scripted())
    )
    app.dependency_overrides[get_config] = lambda: fast  # type: ignore[attr-defined]
    with client.stream("GET", "/api/pipeline/7/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        body = "".join(res.iter_text())
    events = [e for e in body.split("\n\n") if e.strip()]
    assert events[0] == "retry: 2000"
    kinds = [e.split("\n")[0] for e in events[1:]]
    assert kinds == ["event: progress", "event: progress", "event: progress", "event: end"]
    assert '"status":"done"' in events[3] and "id: 3" in events[3]
    assert client.get("/api/pipeline/999/events").status_code == 404


def test_start_via_api(client: TestClient, db: Session) -> None:
    seed_company(db, "SYNTH")
    db.flush()
    started = client.post("/api/pipeline", json={"symbol": "synth"})
    assert started.status_code == 202
    body = started.json()
    assert body["fresh"] is False and body["reason"] == "no report yet"
    run = body["run"]
    assert run["status"] == "queued" and len(run["steps"]) == len(STEPS)
    assert run["steps"][0] == {"name": "symbol", "label": "Symbol", "optional": False,
                               "status": "pending", "message": None, "started_at": None,
                               "finished_at": None}  # fmt: skip
    assert client.get(f"/api/pipeline/{run['id']}").json()["id"] == run["id"]
    assert [r["id"] for r in client.get("/api/pipeline?symbol=SYNTH").json()] == [run["id"]]
    client.get("/api/stocks/SYNTH/report")  # builds and stores a report now
    fresh = client.post("/api/pipeline", json={"symbol": "SYNTH"}).json()
    assert fresh["fresh"] is True and fresh["run"] is None and fresh["report_as_of"]
    forced = client.post("/api/pipeline", json={"symbol": "SYNTH", "force": True}).json()
    assert forced["run"]["id"] == run["id"]  # the queued run is joined
    assert client.post("/api/pipeline", json={"symbol": "bad sym!"}).status_code == 422


def test_steps_of_a_paused_host_are_skipped_and_the_rest_still_run(seeded: Env) -> None:
    import uuid

    from app.core.circuit_breaker import CircuitBreaker
    from app.core.config import Provider

    prefix = f"test-br-{uuid.uuid4().hex}"
    breaker = CircuitBreaker(seeded.ctx.redis, seeded.ctx.config.providers.breaker, prefix=prefix)
    breaker.trip(Provider.NSE)
    seeded.ctx.router._breaker = breaker  # the worker's router with NSE blocked
    try:
        run_id = queue(seeded, "SYNTH")
        assert run_pending(seeded.ctx) == [(run_id, PipelineStatus.DONE)]
        st = steps(load(seeded, run_id))
    finally:
        for k in seeded.ctx.redis.scan_iter(f"{prefix}:*"):
            seeded.ctx.redis.delete(k)
    for name in ("corporate_actions", "filings_index", "xbrl_parse", "pdf_gap_fill",
                 "shareholding_events"):  # fmt: skip
        status, message = st[name]
        assert status == "skipped" and (message or "").startswith(
            "skipped (host paused): NSE paused until "
        )
    assert all(st[n][0] == "ok" for n in ("valuation", "scoring", "report"))  # nothing is hidden


def test_hosts_endpoint_lists_paused_hosts(client: TestClient, redis_client: Redis) -> None:
    from app.core.circuit_breaker import CircuitBreaker
    from app.core.config import Provider

    breaker = CircuitBreaker(redis_client, get_config().providers.breaker)
    try:
        assert client.get("/api/pipeline/hosts").json() == {}
        until = breaker.trip(Provider.NSE)
        body = client.get("/api/pipeline/hosts").json()
        assert until is not None and list(body) == ["nse"]
        assert body["nse"]["text"].startswith("NSE paused until ") and body["nse"]["text"].endswith(
            " IST"
        )
    finally:
        redis_client.delete("breaker:nse")
