"""On-demand pipeline (SPEC v0.2 §3.7): start a run, follow it (JSON or Server-Sent Events)."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import ConfigDep, RedisDep, SessionDep, StreamSessionsDep
from app.api.schemas import (
    SYMBOL_PATTERN,
    HostPause,
    PipelineRequest,
    PipelineRunOut,
    PipelineStart,
)
from app.core.circuit_breaker import CircuitBreaker, paused_text
from app.db.models import PipelineRun
from app.pipeline.runner import TERMINAL, start_run

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


def run_out(run: PipelineRun) -> PipelineRunOut:
    return PipelineRunOut.model_validate({c: getattr(run, c) for c in PipelineRunOut.model_fields})


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start(body: PipelineRequest, session: SessionDep, config: ConfigDep) -> PipelineStart:
    """Build a stock's report on demand. A fresh stored report (newer than the latest prices
    and filings) is returned as is: ``fresh`` true and no run. Otherwise the active run for the
    symbol, or a new one, which the worker starts within a second; follow it on
    ``GET /api/pipeline/{id}/events``."""
    run, fresh = start_run(session, body.symbol, trigger="user", force=body.force,
                           cfg=config.jobs.pipeline, now=datetime.now(UTC))  # fmt: skip
    session.commit()
    return PipelineStart(symbol=body.symbol.upper(), fresh=run is None, reason=fresh.reason,
                         report_as_of=fresh.report_as_of,
                         run=run_out(run) if run is not None else None)  # fmt: skip


@router.get("/hosts")
def paused_hosts(config: ConfigDep, redis: RedisDep) -> dict[str, HostPause]:
    """Hosts whose circuit breaker is open (a blocked response paused them), by name."""
    breaker = CircuitBreaker(redis, config.providers.breaker)
    return {h: HostPause(until=u, text=paused_text(h, u)) for h, u in breaker.paused().items()}


@router.get("")
def list_runs(
    session: SessionDep,
    symbol: Annotated[str | None, Query(pattern=SYMBOL_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[PipelineRunOut]:
    q = select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
    if symbol:
        q = q.where(PipelineRun.symbol == symbol.upper())
    return [run_out(r) for r in session.scalars(q)]


def _get(session: SessionDep, run_id: int) -> PipelineRun:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no pipeline run {run_id}")
    return run


@router.get("/{run_id}", responses={404: {"description": "Unknown run"}})
def get_run(run_id: int, session: SessionDep) -> PipelineRunOut:
    return run_out(_get(session, run_id))


@router.get(
    "/{run_id}/events",
    responses={200: {"content": {"text/event-stream": {}}}, 404: {"description": "Unknown run"}},
    response_class=StreamingResponse,
)
def events(
    run_id: int, session: SessionDep, config: ConfigDep, sessions: StreamSessionsDep
) -> StreamingResponse:
    """Server-Sent Events: a ``progress`` event with the whole run (as ``GET /{id}``) each time
    it changes, then ``end`` when it is done or failed. Comment lines keep the connection
    alive. Reconnecting (EventSource does so by itself) resends the current state."""
    _get(session, run_id)
    cfg = config.jobs.pipeline

    def stream() -> Iterator[str]:
        seen, last_sent = -1, time.monotonic()
        deadline = last_sent + cfg.sse_max_minutes * 60
        yield "retry: 2000\n\n"
        while time.monotonic() < deadline:
            with sessions() as s:
                run = s.get(PipelineRun, run_id, populate_existing=True)
                if run is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                if run.version != seen:
                    seen = run.version
                    payload = run_out(run).model_dump_json()
                    yield f"event: progress\nid: {run.version}\ndata: {payload}\n\n"
                    last_sent = time.monotonic()
                if run.status in TERMINAL:
                    yield f"event: end\ndata: {json.dumps({'status': run.status.value})}\n\n"
                    return
            if time.monotonic() - last_sent >= cfg.sse_heartbeat_s:
                yield ": keep-alive\n\n"
                last_sent = time.monotonic()
            time.sleep(cfg.sse_poll_s)
        yield 'event: end\ndata: {"status": "timeout"}\n\n'

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)
