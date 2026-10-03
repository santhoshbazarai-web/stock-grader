"""LLM thesis for a stock's latest report (SPEC §8a): numbers in → text out, no new facts."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ConfigDep, RedisDep, SessionDep, SettingsDep
from app.api.schemas import Symbol, ThesisOut
from app.core.rate_limiter import RateLimiter, on_demand
from app.reports.thesis_service import (
    ThesisView,
    build_model,
    disabled_reason,
    generate,
    thesis_view,
)

router = APIRouter(prefix="/stocks", tags=["stocks"])

_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "No report for this stock yet"},
}


def _out(view: ThesisView, disabled: str | None) -> ThesisOut:
    return ThesisOut(
        symbol=view.symbol, as_of=view.as_of, enabled=disabled is None, status=view.status,
        text=view.text, model=view.model, generated_at=view.generated_at,
        attempts=view.attempts, problems=view.problems, reasons=view.reasons,
    )  # fmt: skip


@router.get("/{symbol}/thesis", responses=_RESPONSES)
def get_thesis(
    symbol: Symbol, session: SessionDep, config: ConfigDep, settings: SettingsDep
) -> ThesisOut:
    """The thesis written for the latest report's exact numbers, if any. A thesis written for
    earlier numbers is not shown (``status`` ``missing``)."""
    cfg = config.jobs.thesis
    disabled = disabled_reason(settings, cfg)
    view = thesis_view(session, symbol, cfg, disabled)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no report for {symbol.upper()} yet")
    return _out(view, disabled)


@router.post(
    "/{symbol}/thesis",
    responses={**_RESPONSES, 409: {"description": "Thesis generator off or no model set"}},
)
def write_thesis(
    symbol: Symbol,
    session: SessionDep,
    config: ConfigDep,
    settings: SettingsDep,
    redis: RedisDep,
    force: bool = False,
) -> ThesisOut:
    """Write the thesis now (blocks while the model generates). Reuses one that
    already passed for the same numbers unless ``force``. A draft that cites a number not in
    the report is retried, then rejected (``status`` ``rejected`` with the problems)."""
    cfg = config.jobs.thesis
    disabled = disabled_reason(settings, cfg)
    model = build_model(settings, cfg, RateLimiter(redis, config.providers.rate_limits))
    if disabled is not None or model is None:
        raise HTTPException(status.HTTP_409_CONFLICT, disabled or "no model")
    try:
        with on_demand():
            view = generate(session, symbol, cfg, model, now=datetime.now(UTC), force=force)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _out(view, None)
