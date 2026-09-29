"""Broker connection endpoints (SPEC §3.3, §8). Read-only brokers: login, callback, status."""

import logging
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import (
    ConfigDep,
    FyersAuthDep,
    KiteAuthDep,
    SettingsDep,
    StateSignerDep,
    TokenStoreDep,
)
from app.core.config import AppConfig
from app.core.security import InvalidStateError, StateSigner
from app.core.settings import Settings
from app.data.broker_tokens import BrokerTokenStore
from app.data.providers.base import IssuedToken, ProviderError
from app.db.enums import Broker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brokers", tags=["brokers"])


class BrokerStatus(BaseModel):
    broker: Broker
    connected: bool
    expires_at: datetime | None
    reason: str


def _state_purpose(broker: Broker) -> str:
    return f"{broker.value}-login"


def _complete_login(
    broker: Broker,
    *,
    state: str,
    approved: bool,
    code: str | None,
    exchange: Callable[[str], IssuedToken],
    signer: StateSigner,
    store: BrokerTokenStore,
    settings: Settings,
    config: AppConfig,
) -> RedirectResponse:
    """Verify state → exchange the one-time code → store the encrypted token → back to the UI."""

    def back(outcome: str, reason: str = "") -> RedirectResponse:
        params = {"broker": broker.value, "status": outcome}
        if reason:
            params["reason"] = reason
        url = f"{settings.web_url.rstrip('/')}/settings?{urlencode(params)}"
        return RedirectResponse(url, status_code=303)

    try:
        signer.verify(state, _state_purpose(broker), max_age_s=config.providers.oauth_state_ttl_s)
    except InvalidStateError as exc:
        logger.warning("%s callback rejected: %s", broker, exc)
        return back("error", "invalid_state")
    if not approved or not code:
        return back("error", "login_declined")
    try:
        issued = exchange(code)
    except ProviderError as exc:
        logger.warning("%s token exchange failed: %s", broker, exc)
        return back("error", "exchange_failed")
    store.save(broker, issued.access_token, issued.expires_at)
    return back("connected")


@router.get("/status")
def broker_status(store: TokenStoreDep) -> list[BrokerStatus]:
    return [
        BrokerStatus(
            broker=s.broker, connected=s.connected, expires_at=s.expires_at, reason=s.reason
        )
        for s in store.all_statuses()
    ]


# ───────────────────────── Fyers ─────────────────────────


@router.get("/fyers/login")
def fyers_login(auth: FyersAuthDep, signer: StateSignerDep) -> RedirectResponse:
    state = signer.issue(_state_purpose(Broker.FYERS))
    return RedirectResponse(auth.login_url(state), status_code=307)


@router.get("/fyers/callback")
def fyers_callback(
    auth: FyersAuthDep,
    signer: StateSignerDep,
    store: TokenStoreDep,
    settings: SettingsDep,
    config: ConfigDep,
    state: str = "",
    auth_code: str | None = None,
    s: str | None = None,
) -> RedirectResponse:
    return _complete_login(
        Broker.FYERS,
        state=state,
        approved=s == "ok",
        code=auth_code,
        exchange=auth.exchange,
        signer=signer,
        store=store,
        settings=settings,
        config=config,
    )


# ───────────────────────── Kite ─────────────────────────


@router.get("/kite/login")
def kite_login(auth: KiteAuthDep, signer: StateSignerDep) -> RedirectResponse:
    state = signer.issue(_state_purpose(Broker.KITE))
    return RedirectResponse(auth.login_url(state), status_code=307)


@router.get("/kite/callback")
def kite_callback(
    auth: KiteAuthDep,
    signer: StateSignerDep,
    store: TokenStoreDep,
    settings: SettingsDep,
    config: ConfigDep,
    state: str = "",
    request_token: str | None = None,
    status: str | None = None,
) -> RedirectResponse:
    return _complete_login(
        Broker.KITE,
        state=state,
        approved=status == "success",
        code=request_token,
        exchange=auth.exchange,
        signer=signer,
        store=store,
        settings=settings,
        config=config,
    )
