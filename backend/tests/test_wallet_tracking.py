"""Tests for starring (manually tracking) wallets via the API."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.api import wallets
from app.api.deps import get_db
from app.config import Settings
from app.db.models import Wallet

VALID_ADDR = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _build_app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(wallets.router)

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_track_creates_unknown_wallet(db_session) -> None:
    async with _client(_build_app(db_session)) as client:
        resp = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
    assert resp.status_code == 200
    body = resp.json()
    assert body["address"] == VALID_ADDR
    assert body["is_tracked"] is True

    row = (
        await db_session.execute(select(Wallet).where(Wallet.address == VALID_ADDR))
    ).scalar_one()
    assert row.is_tracked is True


async def test_track_existing_wallet_is_idempotent(db_session) -> None:
    now = datetime.now(tz=UTC)
    db_session.add(
        Wallet(address=VALID_ADDR, first_seen_at=now, last_seen_at=now, is_tracked=False)
    )
    await db_session.commit()

    async with _client(_build_app(db_session)) as client:
        first = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
        second = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["is_tracked"] is True
    count = len(
        (
            await db_session.execute(select(Wallet).where(Wallet.address == VALID_ADDR))
        ).all()
    )
    assert count == 1


async def test_track_rejects_invalid_address(db_session) -> None:
    async with _client(_build_app(db_session)) as client:
        bad_chars = await client.post("/api/wallets/track", json={"address": "not-a-key!"})
        too_short = await client.post("/api/wallets/track", json={"address": "abc"})
        zero_char = await client.post(
            "/api/wallets/track", json={"address": "0" + VALID_ADDR[1:]}
        )
    assert bad_chars.status_code == 422
    assert too_short.status_code == 422
    assert zero_char.status_code == 422  # base58 has no '0'


async def test_untrack_clears_flag_and_404s_unknown(db_session) -> None:
    now = datetime.now(tz=UTC)
    db_session.add(
        Wallet(address=VALID_ADDR, first_seen_at=now, last_seen_at=now, is_tracked=True)
    )
    await db_session.commit()

    async with _client(_build_app(db_session)) as client:
        resp = await client.delete(f"/api/wallets/{VALID_ADDR}/track")
        missing = await client.delete(
            "/api/wallets/2WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM/track"
        )
    assert resp.status_code == 200
    assert resp.json()["is_tracked"] is False
    assert missing.status_code == 404


async def test_track_requires_auth_when_configured(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With a password hash configured, the dependency demands a Bearer token.
    configured = Settings(admin_password_hash="$2b$12$x")
    monkeypatch.setattr("app.auth.deps.get_settings", lambda: configured)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
    assert resp.status_code == 401
