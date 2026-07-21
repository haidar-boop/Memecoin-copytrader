"""Password hashing and JWT helpers for the dashboard API.

Uses bcrypt for password storage and PyJWT (HS256) for stateless access
tokens. All decode/verify paths are hardened to never raise on malformed
input — they return a falsy result instead so callers can treat "bad
credentials" and "bad token" uniformly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import bcrypt
import jwt

from app.logging_config import get_logger

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger(__name__)

# The default signing secret shipped in config. Using it in a non-dev
# environment is a misconfiguration we refuse to tolerate.
_DEFAULT_SECRET = "dev-insecure-change-me"


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt, returning a str."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a bcrypt hash.

    Never raises: a malformed/empty hash yields ``False``.
    """
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Mint a signed HS256 access token for ``subject``.

    Fails closed: if the signing secret is still the shipped default and we
    are not in a dev environment, refuse to issue a token.
    """
    if settings.jwt_secret == _DEFAULT_SECRET and not settings.is_dev:
        raise RuntimeError(
            "Refusing to sign JWTs with the default insecure jwt_secret "
            "outside dev; set JWT_SECRET to a strong value."
        )
    issued_at = now or datetime.now(UTC)
    expire = issued_at + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": subject,
        "iat": int(issued_at.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, settings: Settings) -> dict | None:
    """Decode and validate a token, returning its claims or ``None``.

    Returns ``None`` for any invalid, expired, or tampered token rather than
    raising, so callers never need to guard against jwt exceptions.

    Fails closed symmetrically with :func:`create_access_token`: a token
    signed with the shipped default secret is rejected outside dev, so a
    production deployment left on the default secret cannot be bypassed with a
    forged token signed by that publicly-known key.
    """
    if settings.jwt_secret == _DEFAULT_SECRET and not settings.is_dev:
        return None
    try:
        return jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return None
