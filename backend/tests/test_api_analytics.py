"""Tests for the analytics API surface (analytics router + wallet stats endpoints)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    DiscoveredPattern,
    MlModel,
    StrategyCluster,
    StrategyStat,
    Wallet,
    WalletStats,
    WalletStatsSnapshot,
)
from app.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=2)


def _wallet(i: int, address: str) -> Wallet:
    return Wallet(id=i, address=address, first_seen_at=OLD, last_seen_at=NOW)


def _stats(wallet_id: int, **kw) -> WalletStats:
    defaults = dict(
        wallet_id=wallet_id,
        computed_at=NOW,
        trade_count=10,
        buy_count=5,
        sell_count=5,
        position_count=5,
        closed_position_count=5,
        win_count=3,
    )
    defaults.update(kw)
    return WalletStats(**defaults)


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            _wallet(1, "AAA"),
            _wallet(2, "BBB"),
            _wallet(3, "CCC"),
            _wallet(4, "DDD"),  # wallet with no stats row
        ]
    )
    session.add_all(
        [
            _stats(
                1,
                confidence_score=Decimal("90.50"),
                total_pnl_sol=Decimal("5"),
                win_rate=Decimal("0.6"),
                pnl_30d_sol=Decimal("2"),
                closed_position_count=10,
                style="sniper",
                confidence_components=[
                    {
                        "component": "win_rate",
                        "value": 0.6,
                        "weight": 0.3,
                        "contribution": 18.0,
                        "note": "solid",
                    }
                ],
            ),
            _stats(
                2,
                confidence_score=Decimal("70.00"),
                total_pnl_sol=Decimal("50"),
                win_rate=Decimal("0.9"),
                pnl_30d_sol=None,
                closed_position_count=3,
                style="scalper",
            ),
            _stats(
                3,
                confidence_score=None,  # excluded from confidence ranking
                total_pnl_sol=Decimal("1"),
                win_rate=Decimal("0.1"),
                pnl_30d_sol=Decimal("9"),
                closed_position_count=1,
            ),
        ]
    )
    # Snapshot history for wallet 1: two recent, one too old for days=30.
    # Core executemany avoids the ORM sentinel issue on composite-PK tables.
    await session.execute(
        insert(WalletStatsSnapshot),
        [
            {
                "wallet_id": 1,
                "ts": ts,
                "trade_count": 10,
                "buy_count": 5,
                "sell_count": 5,
                "position_count": 5,
                "closed_position_count": 5,
                "win_count": 3,
                "confidence_score": Decimal("80.00"),
            }
            for ts in (
                NOW - timedelta(days=1),
                NOW - timedelta(days=5),
                NOW - timedelta(days=90),
            )
        ],
    )
    # Strategy clusters: an older run and the latest run.
    session.add_all(
        [
            StrategyCluster(computed_at=OLD, name="stale", member_count=1),
            StrategyCluster(
                computed_at=NOW,
                name="sniper",
                member_count=7,
                feature_names=["avg_hold_seconds"],
                centroid=[1.5],
                description="fast entries",
            ),
            StrategyCluster(computed_at=NOW, name="scalper", member_count=4),
        ]
    )
    session.add_all(
        [
            StrategyStat(
                ts=OLD,
                style="sniper",
                window_days=30,
                wallet_count=5,
                closed_positions=20,
                win_rate=Decimal("0.4"),
            ),
            StrategyStat(
                ts=NOW,
                style="sniper",
                window_days=30,
                wallet_count=7,
                closed_positions=42,
                win_rate=Decimal("0.55"),
                total_pnl_sol=Decimal("12"),
            ),
        ]
    )
    # Patterns: latest run per kind only.
    session.add_all(
        [
            DiscoveredPattern(
                kind="time_of_day", key={"hour": 3}, stats={"wr": 0.2}, evidence_count=5,
                computed_at=OLD,
            ),
            DiscoveredPattern(
                kind="time_of_day", key={"hour": 14}, stats={"wr": 0.6}, evidence_count=50,
                computed_at=NOW,
            ),
            DiscoveredPattern(
                kind="lifecycle", key={"bucket": "launch"}, stats={"wr": 0.3}, evidence_count=9,
                computed_at=OLD,
            ),
        ]
    )
    session.add_all(
        [
            MlModel(
                name="entry_quality",
                version=1,
                algo="gbdt",
                trained_at=OLD,
                training_rows=100,
                metrics={"auc": 0.61},
                artifact_path="/secret/models/v1.joblib",
                is_active=False,
            ),
            MlModel(
                name="entry_quality",
                version=2,
                algo="gbdt",
                trained_at=NOW,
                training_rows=500,
                metrics={"auc": 0.66},
                artifact_path="/secret/models/v2.joblib",
                is_active=True,
            ),
        ]
    )
    await session.commit()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _seed(session)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


async def test_top_wallets_by_confidence(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/wallets/top", params={"by": "confidence_score"})
    assert resp.status_code == 200
    body = resp.json()
    # wallet 3 has null confidence and is excluded; ranking desc.
    assert [row["address"] for row in body] == ["AAA", "BBB"]
    assert body[0]["confidence_score"] == "90.50"
    assert body[0]["style"] == "sniper"


async def test_top_wallets_by_pnl_and_min_closed(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/api/analytics/wallets/top", params={"by": "total_pnl_sol"}
    )
    assert [row["address"] for row in resp.json()] == ["BBB", "AAA", "CCC"]

    resp = await client.get(
        "/api/analytics/wallets/top",
        params={"by": "total_pnl_sol", "min_closed": 5},
    )
    assert [row["address"] for row in resp.json()] == ["AAA"]

    # pnl_30d_sol: wallet 2 has null and is excluded.
    resp = await client.get("/api/analytics/wallets/top", params={"by": "pnl_30d_sol"})
    assert [row["address"] for row in resp.json()] == ["CCC", "AAA"]


async def test_top_wallets_rejects_bad_field(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/wallets/top", params={"by": "artifact_path"})
    assert resp.status_code == 422


async def test_wallet_stats_detail(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/wallets/AAA/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet_id"] == 1
    assert body["confidence_score"] == "90.50"
    assert body["confidence_components"] == [
        {
            "component": "win_rate",
            "value": 0.6,
            "weight": 0.3,
            "contribution": 18.0,
            "note": "solid",
        }
    ]


async def test_wallet_stats_404s(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/wallets/NOPE/stats")).status_code == 404
    # wallet exists but has no stats row
    assert (await client.get("/api/wallets/DDD/stats")).status_code == 404
    assert (await client.get("/api/wallets/NOPE/stats/history")).status_code == 404


async def test_wallet_stats_history(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/wallets/AAA/stats/history", params={"days": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2  # 90-day-old row excluded
    ts_values = [row["ts"] for row in body]
    assert ts_values == sorted(ts_values, reverse=True)

    resp = await client.get(
        "/api/wallets/AAA/stats/history", params={"days": 365, "limit": 1}
    )
    assert len(resp.json()) == 1


async def test_strategies_latest_run_only(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/strategies")
    assert resp.status_code == 200
    body = resp.json()
    names = [c["name"] for c in body]
    assert names == ["scalper", "sniper"]  # stale run excluded, ordered by name
    sniper = body[1]
    assert sniper["member_count"] == 7
    assert Decimal(sniper["latest_stat"]["win_rate"]) == Decimal("0.55")
    assert sniper["latest_stat"]["closed_positions"] == 42
    assert body[0]["latest_stat"] is None


async def test_patterns_latest_per_kind(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/patterns")
    assert resp.status_code == 200
    body = resp.json()
    kinds = {row["kind"] for row in body}
    assert kinds == {"time_of_day", "lifecycle"}
    tod = [row for row in body if row["kind"] == "time_of_day"]
    assert len(tod) == 1
    assert tod[0]["key"] == {"hour": 14}  # only the latest run for the kind

    resp = await client.get("/api/analytics/patterns", params={"kind": "lifecycle"})
    body = resp.json()
    assert len(body) == 1
    assert body[0]["kind"] == "lifecycle"


async def test_models_registry_hides_artifact_path(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/analytics/models")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["version"] for m in body] == [2, 1]  # newest first
    assert body[0]["is_active"] is True
    assert body[0]["metrics"] == {"auc": 0.66}
    for m in body:
        assert "artifact_path" not in m
        assert "/secret" not in resp.text


async def test_openapi_includes_new_paths(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    for p in (
        "/api/analytics/wallets/top",
        "/api/analytics/strategies",
        "/api/analytics/patterns",
        "/api/analytics/models",
        "/api/wallets/{address}/stats",
        "/api/wallets/{address}/stats/history",
    ):
        assert p in paths
