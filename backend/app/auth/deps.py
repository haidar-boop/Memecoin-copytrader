"""FastAPI auth dependency for protecting dashboard endpoints."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.auth.security import decode_token
from app.config import get_settings

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_auth(request: Request) -> str:
    """Require a valid Bearer token; return the subject username.

    Raises 401 (with a ``WWW-Authenticate: Bearer`` challenge) when the
    Authorization header is missing, malformed, or carries an invalid token.
    """
    header = request.headers.get("Authorization")
    if not header:
        raise _UNAUTHORIZED
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHORIZED
    return _subject_from_token(token, get_settings())


def _subject_from_token(token: str, settings) -> str:
    claims = decode_token(token, settings)
    if claims is None:
        raise _UNAUTHORIZED
    subject = claims.get("sub")
    if not subject or not isinstance(subject, str):
        raise _UNAUTHORIZED
    return subject


def auth_configured(settings) -> bool:
    """True when an operator password is set, i.e. login is possible.

    In an unconfigured (dev/demo) deployment no token can ever be obtained, so
    endpoints that gate "only when auth is configured" stay open there and
    lock down automatically the moment credentials are provisioned.
    """
    return settings.admin_password_hash is not None


def require_auth_if_configured(request: Request) -> str | None:
    """Protect an endpoint only when auth is configured.

    Used for the operator's private notification feed (dashboard + WS): in
    production (password set) a valid Bearer token is required; in an
    unconfigured deployment the endpoint is open like the rest of the
    read API.
    """
    settings = get_settings()
    if not auth_configured(settings):
        return None
    header = request.headers.get("Authorization")
    if not header:
        raise _UNAUTHORIZED
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHORIZED
    return _subject_from_token(token, settings)


def ws_authorized(token: str | None, settings) -> bool:
    """WebSocket handshake check: valid token, or auth not configured."""
    if not auth_configured(settings):
        return True
    if not token:
        return False
    claims = decode_token(token, settings)
    return bool(claims and isinstance(claims.get("sub"), str))
