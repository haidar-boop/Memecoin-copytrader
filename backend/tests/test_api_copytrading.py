"""Copytrading API tests: reads, status, admin-token gating."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as config
from app.api.deps import get_db, get_redis
from app.config import Settings
from app.db.models import CopyPosition, TradeDecision
from app.decision.safety import EMERGENCY_STOP_KEY
from app.main import create_app
from tests.conftest import StubRedis

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def client_factory(db_session: AsyncSession, stub_redis: StubRedis, monkeypatch):
    def build(settings: Settings):
        monkeypatch.setattr(config, "get_settings", lambda: settings)
        # Routers imported get_settings by reference from app.config.
        import app.api.copytrading as ct

        monkeypatch.setattr(ct, "get_settings", lambda: settings)
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_redis] = lambda: stub_redis
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return build


async def seed(db_session: AsyncSession) -> None:
    db_session.add(
        TradeDecision(
            created_at=NOW, leader_wallet_id=1, token_id=1, side="buy",
            mode="paper", decision="copy", confidence_score=Decimal("70"),
            risk_score=Decimal("40"), reasons=[{"gate": "copy_enabled", "passed": True}],
            factors=[{"factor": "wallet_confidence", "value": 70.0}],
        )
    )
    db_session.add(
        CopyPosition(
            token_id=1, leader_wallet_id=1, mode="paper", status="open",
            opened_at=NOW, spent_sol=Decimal("0.1"), tokens_bought=Decimal(1000),
        )
    )
    await db_session.commit()


async def test_reads_and_status(db_session, stub_redis, client_factory) -> None:
    await seed(db_session)
    async with client_factory(Settings()) as client:
        decisions = (await client.get("/api/copytrading/decisions")).json()
        assert len(decisions) == 1 and decisions[0]["decision"] == "copy"
        assert decisions[0]["reasons"][0]["gate"] == "copy_enabled"

        positions = (await client.get("/api/copytrading/positions?status_filter=open")).json()
        assert len(positions) == 1

        status = (await client.get("/api/copytrading/status")).json()
        assert status["open_positions"] == 1
        assert status["exposure_sol"] == "0.1"
        assert status["emergency_stop"] is None


async def test_emergency_stop_needs_no_token_but_resume_does(
    db_session, stub_redis, client_factory
) -> None:
    async with client_factory(Settings()) as client:
        response = await client.post("/api/copytrading/emergency-stop")
        assert response.status_code == 200
        assert stub_redis.data.get(EMERGENCY_STOP_KEY)

        # resume without configured admin_token: refused outright
        response = await client.post("/api/copytrading/resume")
        assert response.status_code == 403
        assert stub_redis.data.get(EMERGENCY_STOP_KEY)

    async with client_factory(Settings(admin_token="s3cret")) as client:
        response = await client.post("/api/copytrading/resume")
        assert response.status_code == 403  # missing header
        response = await client.post(
            "/api/copytrading/resume", headers={"X-Admin-Token": "wrong"}
        )
        assert response.status_code == 403
        response = await client.post(
            "/api/copytrading/resume", headers={"X-Admin-Token": "s3cret"}
        )
        assert response.status_code == 200
        assert EMERGENCY_STOP_KEY not in stub_redis.data


async def test_approval_missing_trade_404(db_session, stub_redis, client_factory) -> None:
    async with client_factory(Settings(admin_token="s3cret")) as client:
        response = await client.post(
            "/api/copytrading/approvals/999", headers={"X-Admin-Token": "s3cret"}
        )
        assert response.status_code == 404
