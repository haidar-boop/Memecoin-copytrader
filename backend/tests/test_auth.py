"""Tests for JWT dashboard authentication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

import app.api.auth as auth_api
import app.auth.deps as auth_deps
import app.config as config_mod
from app.api.auth import router as auth_router
from app.auth.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import Settings

CORRECT_PASSWORD = "correct-horse"


def make_settings(**overrides) -> Settings:
    base = dict(
        admin_username="admin",
        admin_password_hash=hash_password(CORRECT_PASSWORD),
        jwt_secret="test-secret",
        app_env="dev",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def patched(monkeypatch, settings):
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)
    return settings


def build_client() -> httpx.AsyncClient:
    application = FastAPI()
    application.include_router(auth_router)
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --- security unit tests ---------------------------------------------------


def test_hash_verify_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False


def test_verify_malformed_hash_returns_false():
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


def test_token_roundtrip(settings):
    token = create_access_token("admin", settings)
    claims = decode_token(token, settings)
    assert claims is not None
    assert claims["sub"] == "admin"
    assert "exp" in claims and "iat" in claims


def test_expired_token_returns_none(settings):
    past = datetime.now(UTC) - timedelta(hours=100)
    token = create_access_token("admin", settings, now=past)
    assert decode_token(token, settings) is None


def test_tampered_token_returns_none(settings):
    token = create_access_token("admin", settings)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert decode_token(tampered, settings) is None


def test_wrong_secret_returns_none(settings):
    token = create_access_token("admin", settings)
    other = make_settings(jwt_secret="different-secret")
    assert decode_token(token, other) is None


def test_prod_default_secret_guard_raises():
    prod = make_settings(jwt_secret="dev-insecure-change-me", app_env="prod")
    with pytest.raises(RuntimeError):
        create_access_token("admin", prod)


# --- API tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_and_token_works(patched):
    async with build_client() as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": CORRECT_PASSWORD},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        token = body["access_token"]

        me = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert me.status_code == 200
        assert me.json()["username"] == "admin"


@pytest.mark.asyncio
async def test_login_wrong_password_401(patched):
    async with build_client() as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nope"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_user_401(patched):
    async with build_client() as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "root", "password": CORRECT_PASSWORD},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unconfigured_503(monkeypatch):
    settings = make_settings(admin_password_hash=None)
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
    async with build_client() as client:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": CORRECT_PASSWORD},
        )
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_me_missing_token_401(patched):
    async with build_client() as client:
        resp = await client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_me_invalid_token_401(patched):
    async with build_client() as client:
        resp = await client.get(
            "/api/auth/me", headers={"Authorization": "Bearer garbage.token.here"}
        )
        assert resp.status_code == 401


# --- notification-feed auth gating (Phase 5 security fix) -------------------

def test_ws_authorized_gating() -> None:
    """The private feed is open when auth is unconfigured, locked when set."""
    from app.auth.deps import ws_authorized
    from app.auth.security import create_access_token, hash_password
    from app.config import Settings

    open_settings = Settings(app_env="dev", admin_password_hash=None)
    assert ws_authorized(None, open_settings) is True  # unconfigured -> open

    locked = Settings(
        app_env="dev", jwt_secret="test-secret", admin_password_hash=hash_password("pw")
    )
    assert ws_authorized(None, locked) is False  # no token -> blocked
    assert ws_authorized("garbage", locked) is False  # bad token -> blocked
    good = create_access_token("admin", locked)
    assert ws_authorized(good, locked) is True  # valid token -> allowed
