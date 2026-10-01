"""Single-user login / logout (SPEC §8). See ``app.core.auth``."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import SettingsDep, get_login_throttle, require_user
from app.core.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    SessionSigner,
    client_ip,
    get_session_signer,
    password_ok,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    token: str = Field(description="Session token; also set as an HttpOnly cookie")
    expires_in_s: int


class Me(BaseModel):
    authenticated: bool


@router.post(
    "/login",
    responses={401: {"description": "Wrong password"}, 429: {"description": "Locked out"}},
)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    signer: Annotated[SessionSigner, Depends(get_session_signer)],
    throttle: Annotated[LoginThrottle, Depends(get_login_throttle)],
) -> LoginResponse:
    """Check ``APP_PASSWORD`` and start a session."""
    client = client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        settings.trusted_proxies,
    )
    wait = throttle.locked_for(client)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many failed logins; try again in {wait}s",
            headers={"Retry-After": str(wait)},
        )
    if not password_ok(body.password, settings):
        throttle.failure(client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")
    throttle.success(client)
    token = signer.issue()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=signer.max_age_s,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return LoginResponse(token=token, expires_in_s=signer.max_age_s)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    """Clear the session cookie (bearer tokens simply expire)."""
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", dependencies=[Depends(require_user)])
def me() -> Me:
    """200 when the session is valid, 401 otherwise."""
    return Me(authenticated=True)
