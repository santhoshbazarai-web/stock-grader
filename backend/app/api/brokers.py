"""Broker connection endpoints (SPEC §3.3, §8). Read-only brokers: login, callback, status."""

import logging
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import ConfigDep, FyersAuthDep, SettingsDep, StateSignerDep, TokenStoreDep
from app.core.security import InvalidStateError
from app.data.providers.base import ProviderError
from app.db.enums import Broker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brokers", tags=["brokers"])

_FYERS_STATE_PURPOSE = "fyers-login"


class BrokerStatus(BaseModel):
    broker: Broker
    connected: bool
    expires_at: datetime | None
    reason: str


def _back_to_settings(web_url: str, broker: Broker, outcome: str, reason: str = "") -> str:
    params = {"broker": broker.value, "status": outcome}
    if reason:
        params["reason"] = reason
    return f"{web_url.rstrip('/')}/settings?{urlencode(params)}"


@router.get("/status")
def broker_status(store: TokenStoreDep) -> list[BrokerStatus]:
    return [
        BrokerStatus(
            broker=s.broker, connected=s.connected, expires_at=s.expires_at, reason=s.reason
        )
        for s in store.all_statuses()
    ]


@router.get("/fyers/login")
def fyers_login(auth: FyersAuthDep, signer: StateSignerDep) -> RedirectResponse:
    return RedirectResponse(auth.login_url(signer.issue(_FYERS_STATE_PURPOSE)), status_code=307)


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
    def back(outcome: str, reason: str = "") -> RedirectResponse:
        url = _back_to_settings(settings.web_url, Broker.FYERS, outcome, reason)
        return RedirectResponse(url, status_code=303)

    try:
        signer.verify(state, _FYERS_STATE_PURPOSE, max_age_s=config.providers.oauth_state_ttl_s)
    except InvalidStateError as exc:
        logger.warning("Fyers callback rejected: %s", exc)
        return back("error", "invalid_state")
    if s != "ok" or not auth_code:
        return back("error", "login_declined")
    try:
        issued = auth.exchange(auth_code)
    except ProviderError as exc:
        logger.warning("Fyers token exchange failed: %s", exc)
        return back("error", "exchange_failed")
    store.save(Broker.FYERS, issued.access_token, issued.expires_at)
    return back("connected")
