"""Tests for the Phase 4 read API (reports + evaluation)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.db.models import (
    MarketRegime,
    ModelPerformance,
    RegimeStrategyStat,
    Report,
)
from app.main import create_app

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


async def _seed(session) -> None:
    session.add_all(
        [
            Report(
                kind="daily",
                generated_at=NOW - timedelta(days=1),
                window_start=NOW - timedelta(days=2),
                window_end=NOW - timedelta(days=1),
                summary="old daily",
                sections={"a": 1},
                markdown="# old",
            ),
            Report(
                kind="daily",
                generated_at=NOW,
                window_start=NOW - timedelta(days=1),
                window_end=NOW,
                summary="new daily",
                sections={"a": 2},
                markdown="# new",
            ),
            Report(
                kind="weekly",
                generated_at=NOW - timedelta(hours=1),
                window_start=NOW - timedelta(days=7),
                window_end=NOW,
                summary="the week",
                sections={"top": ["x"]},
                markdown="# week",
            ),
        ]
    )
    session.add_all(
        [
            ModelPerformance(
                ts=NOW - timedelta(days=1),
                model_id=1,
                model_name="p_profit",
                window_days=7,
                resolved_count=100,
                auc=Decimal("0.60"),
                brier=Decimal("0.20"),
                accuracy=Decimal("0.55"),
                base_rate=Decimal("0.40"),
                calibration=[{"p_bin": 0, "predicted": 0.1, "observed": 0.12, "n": 5}],
            ),
            ModelPerformance(
                ts=NOW,
                model_id=1,
                model_name="p_profit",
                window_days=7,
                resolved_count=120,
                auc=Decimal("0.70"),
                brier=Decimal("0.18"),
                accuracy=Decimal("0.60"),
                base_rate=Decimal("0.42"),
                calibration=[{"p_bin": 0, "predicted": 0.2, "observed": 0.19, "n": 8}],
            ),
            ModelPerformance(
                ts=NOW,
                model_id=2,
                model_name="p_roi",
                window_days=7,
                resolved_count=90,
                auc=None,
                brier=None,
                accuracy=None,
                base_rate=None,
                calibration=None,
            ),
        ]
    )
    session.add_all(
        [
            MarketRegime(
                ts=NOW - timedelta(hours=2),
                window_minutes=60,
                regime="bull",
                high_volatility=True,
                features={"ret": 0.1},
                description="up",
            ),
            MarketRegime(
                ts=NOW,
                window_minutes=60,
                regime="bear",
                panic_selling=True,
                features={"ret": -0.2},
                description="down",
            ),
        ]
    )
    session.add_all(
        [
            RegimeStrategyStat(
                ts=NOW - timedelta(days=1),
                regime="bull",
                style="sniper",
                window_days=30,
                closed_positions=10,
                win_rate=Decimal("0.5"),
                avg_roi=Decimal("0.1"),
                total_pnl_sol=Decimal("1.0"),
            ),
            RegimeStrategyStat(
                ts=NOW,
                regime="bull",
                style="sniper",
                window_days=30,
                closed_positions=20,
                win_rate=Decimal("0.6"),
                avg_roi=Decimal("0.2"),
                total_pnl_sol=Decimal("2.0"),
            ),
            RegimeStrategyStat(
                ts=NOW,
                regime="bear",
                style="scalper",
                window_days=30,
                closed_positions=15,
                win_rate=Decimal("0.4"),
                avg_roi=Decimal("-0.05"),
                total_pnl_sol=Decimal("-0.5"),
            ),
        ]
    )
    await session.commit()


@pytest.fixture
async def client(db_session):
    await _seed(db_session)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_reports_newest_first(client) -> None:
    resp = await client.get("/api/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    gens = [r["generated_at"] for r in data]
    assert gens == sorted(gens, reverse=True)


async def test_list_reports_kind_filter(client) -> None:
    resp = await client.get("/api/reports", params={"kind": "daily", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(r["kind"] == "daily" for r in data)
    assert data[0]["summary"] == "new daily"
    assert data[0]["sections"] == {"a": 2}


async def test_latest_report(client) -> None:
    resp = await client.get("/api/reports/latest", params={"kind": "weekly"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "the week"


async def test_latest_report_404_empty(db_session) -> None:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/reports/latest", params={"kind": "weekly"})
    assert resp.status_code == 404


async def test_evaluation_models_latest_per_name(client) -> None:
    resp = await client.get("/api/evaluation/models")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    by_name = {r["model_name"]: r for r in data}
    assert by_name["p_profit"]["resolved_count"] == 120
    assert float(by_name["p_profit"]["auc"]) == pytest.approx(0.70)
    assert by_name["p_profit"]["calibration"][0]["predicted"] == 0.2
    assert by_name["p_roi"]["auc"] is None


async def test_evaluation_regimes(client) -> None:
    resp = await client.get("/api/evaluation/regimes", params={"limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["regime"] == "bear"
    assert data[0]["panic_selling"] is True
    assert data[0]["features"] == {"ret": -0.2}


async def test_evaluation_regime_strategies(client) -> None:
    resp = await client.get("/api/evaluation/regime-strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    bull = next(r for r in data if r["regime"] == "bull")
    assert bull["closed_positions"] == 20  # latest ts wins


async def test_evaluation_regime_strategies_filter(client) -> None:
    resp = await client.get("/api/evaluation/regime-strategies", params={"regime": "bear"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["style"] == "scalper"


async def test_openapi_includes_paths(client) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for p in [
        "/api/reports",
        "/api/reports/latest",
        "/api/evaluation/models",
        "/api/evaluation/regimes",
        "/api/evaluation/regime-strategies",
    ]:
        assert p in paths
