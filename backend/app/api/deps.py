"""FastAPI dependencies (overridable in tests via ``app.dependency_overrides``)."""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.config import AppConfig, get_config
from app.core.security import StateSigner, get_cipher, get_state_signer
from app.core.settings import Settings, get_settings
from app.data.broker_tokens import BrokerTokenStore
from app.data.providers.fyers import FyersAuth
from app.db.session import get_session_factory


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


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConfigDep = Annotated[AppConfig, Depends(get_config)]
StateSignerDep = Annotated[StateSigner, Depends(get_state_signer)]
TokenStoreDep = Annotated[BrokerTokenStore, Depends(get_token_store)]
FyersAuthDep = Annotated[FyersAuth, Depends(get_fyers_auth)]
