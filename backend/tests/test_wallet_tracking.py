"""Tests for starring (manually tracking) wallets via the API."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.api import wallets
from app.api.deps import get_db, get_redis
from app.auth.security import create_access_token
from app.config import get_settings
from app.db.models import Wallet

VALID_ADDR = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _build_app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(wallets.router)

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    from tests.conftest import StubRedis

    app.dependency_overrides[get_redis] = lambda: StubRedis()
    return app


def _auth_headers() -> dict[str, str]:
    # Tracking is a trade-controlling mutation: strict Bearer auth, always.
    token = create_access_token("admin", get_settings())
    return {"Authorization": f"Bearer {token}"}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_headers(),
    )


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


async def test_track_fails_closed_without_token(db_session) -> None:
    """No Bearer token -> 401 even in an unconfigured deployment.

    Tracking bypasses the confidence bar, so unlike read endpoints it must
    never be open-when-unconfigured.
    """
    app = _build_app(db_session)
    bare = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    async with bare as client:
        track = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
        untrack = await client.delete(f"/api/wallets/{VALID_ADDR}/track")
    assert track.status_code == 401
    assert untrack.status_code == 401


async def test_track_race_with_concurrent_insert(db_session) -> None:
    """Losing the insert race still stars the surviving row (no 500)."""
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(tz=UTC)
    original_commit = db_session.commit
    raced = {"done": False}

    async def racing_commit():
        if not raced["done"]:
            raced["done"] = True
            # Simulate the ingestion writer winning the insert: roll back
            # our pending insert and materialize the row untracked.
            await db_session.rollback()
            db_session.add(
                Wallet(
                    address=VALID_ADDR,
                    first_seen_at=now,
                    last_seen_at=now,
                    is_tracked=False,
                )
            )
            await original_commit()
            raise IntegrityError("duplicate", None, Exception("unique"))
        await original_commit()

    db_session.commit = racing_commit
    try:
        async with _client(_build_app(db_session)) as client:
            resp = await client.post("/api/wallets/track", json={"address": VALID_ADDR})
    finally:
        db_session.commit = original_commit
    assert resp.status_code == 200
    assert resp.json()["is_tracked"] is True
