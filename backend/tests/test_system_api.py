"""System health / alerts API tests over an in-memory app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.api import system
from app.api.deps import get_db, get_redis
from app.db.models import (
    FailedTransaction,
    MarketRegime,
    ModelPerformance,
    TokenSnapshot,
    Trade,
)
from app.decision.safety import DAILY_PNL_KEY_PREFIX, EMERGENCY_STOP_KEY, _today


class FakeRedis:
    """Minimal async Redis stand-in with ping/get/set for the system API."""

    def __init__(self, up: bool = True) -> None:
        self.data: dict[str, str] = {}
        self._up = up

    async def ping(self) -> bool:
        if not self._up:
            raise ConnectionError("redis down")
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.data[key] = str(value)
        return True


def _make_trade(sig: str, block_time: datetime) -> Trade:
    return Trade(
        signature=sig,
        event_index=0,
        block_time=block_time,
        slot=1,
        wallet_id=1,
        token_id=1,
        dex="pumpfun",
        side="buy",
        token_amount=1,
        quote_amount=1,
        quote_mint="So11111111111111111111111111111111111111112",
    )


def _build_app(session, redis) -> FastAPI:
    app = FastAPI()
    app.include_router(system.router)

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = lambda: redis
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_health_fresh_dataset_ok(db_session) -> None:
    now = datetime.now(tz=UTC)
    db_session.add_all(
        [
            _make_trade("sigfresh", now - timedelta(minutes=1)),
            TokenSnapshot(token_id=1, ts=now - timedelta(minutes=1)),
            MarketRegime(ts=now - timedelta(minutes=5), window_minutes=60, regime="bull"),
            ModelPerformance(
                ts=now - timedelta(minutes=10),
                model_id=1,
                model_name="m",
                window_days=30,
                resolved_count=100,
                auc="0.72",
            ),
        ]
    )
    await db_session.commit()

    redis = FakeRedis()
    async with _client(_build_app(db_session, redis)) as client:
        resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["redis"]["status"] == "ok"
    assert body["components"]["ingestion"]["status"] == "ok"
    assert body["components"]["regime_worker"]["status"] == "ok"
    assert body["counts"]["trades"] == 1


@pytest.mark.asyncio
async def test_health_empty_and_stale_is_degraded(db_session) -> None:
    now = datetime.now(tz=UTC)
    # A very old trade => stale; no snapshots/regimes/model => absent.
    db_session.add(_make_trade("sigold", now - timedelta(hours=5)))
    await db_session.commit()

    redis = FakeRedis()
    async with _client(_build_app(db_session, redis)) as client:
        resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["ingestion"]["status"] == "stale"
    assert body["components"]["enrichment_worker"]["status"] == "absent"
    assert body["components"]["regime_worker"]["status"] == "absent"


@pytest.mark.asyncio
async def test_health_redis_down_returns_503(db_session) -> None:
    redis = FakeRedis(up=False)
    async with _client(_build_app(db_session, redis)) as client:
        resp = await client.get("/api/system/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["components"]["redis"]["status"] == "down"


@pytest.mark.asyncio
async def test_alerts_quiet_when_healthy(db_session) -> None:
    now = datetime.now(tz=UTC)
    db_session.add_all(
        [
            _make_trade("sigq", now - timedelta(minutes=1)),
            MarketRegime(ts=now - timedelta(minutes=5), window_minutes=60, regime="bull"),
        ]
    )
    await db_session.commit()

    redis = FakeRedis()
    async with _client(_build_app(db_session, redis)) as client:
        resp = await client.get("/api/system/alerts")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()["alerts"]}
    assert "ingestion_stalled" not in names
    assert "copy_emergency_stop_active" not in names


@pytest.mark.asyncio
async def test_alerts_emergency_stop_and_stall_fire(db_session) -> None:
    now = datetime.now(tz=UTC)
    # Stale trade + stale (old) regime => ingestion + regime alerts, plus the
    # emergency stop. A STALE regime (had data, now overdue) fires the alert;
    # an ABSENT regime (fresh deploy) intentionally does not.
    db_session.add(_make_trade("sigstale", now - timedelta(hours=3)))
    db_session.add(
        FailedTransaction(signature="f1", block_time=now - timedelta(minutes=5), slot=1)
    )
    db_session.add(
        MarketRegime(ts=now - timedelta(hours=5), window_minutes=60, regime="bull")
    )
    await db_session.commit()

    redis = FakeRedis()
    redis.data[EMERGENCY_STOP_KEY] = "manual trip"
    redis.data[DAILY_PNL_KEY_PREFIX + _today(now)] = str(-2_000_000_000)  # -2 SOL

    async with _client(_build_app(db_session, redis)) as client:
        resp = await client.get("/api/system/alerts")
    assert resp.status_code == 200
    body = resp.json()
    names = {a["name"] for a in body["alerts"]}
    assert "copy_emergency_stop_active" in names
    assert "ingestion_stalled" in names
    assert "no_recent_regime" in names
    assert "daily_loss_near_limit" in names
    assert body["context"]["emergency_stop"] is True
    assert body["context"]["daily_pnl_sol"] == -2.0
