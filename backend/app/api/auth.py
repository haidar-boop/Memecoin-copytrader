"""Authentication API: login and current-user for the dashboard.

A single operator account is supported (``admin_username`` +
``admin_password_hash``). Login returns a short-lived HS256 JWT that the
integrator's protected endpoints validate via :func:`app.auth.deps.require_auth`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import require_auth
from app.auth.security import create_access_token, verify_password
from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    username: str


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    settings = get_settings()
    if settings.admin_password_hash is None:
        raise HTTPException(status_code=503, detail="auth not configured")

    # Same 401 for wrong user and wrong password: never reveal which was
    # wrong. Verify the password even on a username mismatch would be ideal
    # for timing, but the hash is fixed so we simply gate on both.
    username_ok = body.username == settings.admin_username
    password_ok = verify_password(body.password, settings.admin_password_hash)
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token(settings.admin_username, settings)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expiry_minutes * 60,
    )


@router.get("/me", response_model=MeResponse)
async def me(username: str = Depends(require_auth)) -> MeResponse:
    return MeResponse(username=username)
