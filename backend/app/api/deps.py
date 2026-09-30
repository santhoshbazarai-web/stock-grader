"""FastAPI dependencies (overridable in tests via ``app.dependency_overrides``)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis
from sqlalchemy.orm import Session

from app.core.auth import SESSION_COOKIE, LoginThrottle, SessionSigner, get_session_signer
from app.core.config import AppConfig, Provider, get_config
from app.core.security import StateSigner, get_cipher, get_state_signer
from app.core.settings import Settings, get_settings
from app.data.broker_tokens import BrokerTokenStore
from app.data.providers.fyers import FyersAuth
from app.data.providers.kite import KiteAuth
from app.db.session import get_session, get_session_factory

_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE, auto_error=False, description="Session cookie set by /api/auth/login"
)
_bearer_scheme = HTTPBearer(
    auto_error=False, description="Session token returned by /api/auth/login"
)


def require_user(
    signer: Annotated[SessionSigner, Depends(get_session_signer)],
    cookie: Annotated[str | None, Depends(_cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> None:
    """Single-user auth: a valid session cookie or ``Authorization: Bearer`` token."""
    token = bearer.credentials if bearer is not None else cookie
    if not signer.valid(token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "not logged in",
            headers={"WWW-Authenticate": "Bearer"},
        )


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_login_throttle(
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginThrottle:
    return LoginThrottle(
        redis, max_failures=settings.login_max_failures, lockout_s=settings.login_lockout_s
    )


def get_token_store() -> BrokerTokenStore:
    return BrokerTokenStore(get_session_factory(), get_cipher())


def get_fyers_auth(settings: Annotated[Settings, Depends(get_settings)]) -> FyersAuth:
    if not (settings.fyers_app_id and settings.fyers_secret and settings.fyers_redirect_uri):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Fyers is not configured: set FYERS_APP_ID, FYERS_SECRET and FYERS_REDIRECT_URI",
        )
    return FyersAuth(
        settings.fyers_app_id,
        settings.fyers_secret.get_secret_value(),
        settings.fyers_redirect_uri,
    )


def get_kite_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[AppConfig, Depends(get_config)],
) -> KiteAuth:
    # KITE_REDIRECT_URI is registered in the Kite developer console, not sent by the client.
    if not (settings.kite_api_key and settings.kite_api_secret):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Kite is not configured: set KITE_API_KEY and KITE_API_SECRET",
        )
    return KiteAuth(
        settings.kite_api_key,
        settings.kite_api_secret.get_secret_value(),
        config.providers.token_daily_expiry_ist[Provider.KITE],
    )


SessionDep = Annotated[Session, Depends(get_session)]


def get_stream_sessions() -> Callable[[], AbstractContextManager[Session]]:
    """Short-lived sessions for a long-running response (the pipeline's SSE stream polls the
    database; it must not hold one session open for minutes)."""
    return get_session_factory()


StreamSessionsDep = Annotated[
    Callable[[], AbstractContextManager[Session]], Depends(get_stream_sessions)
]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ConfigDep = Annotated[AppConfig, Depends(get_config)]
StateSignerDep = Annotated[StateSigner, Depends(get_state_signer)]
TokenStoreDep = Annotated[BrokerTokenStore, Depends(get_token_store)]
FyersAuthDep = Annotated[FyersAuth, Depends(get_fyers_auth)]
KiteAuthDep = Annotated[KiteAuth, Depends(get_kite_auth)]
