"""API smoke tests that run fully offline."""

from __future__ import annotations

import httpx

from app.main import create_app


async def test_health_live_and_openapi() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        response = await client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        for expected in (
            "/api/wallets",
            "/api/tokens",
            "/api/trades/recent",
            "/api/stats/ingestion",
            "/health/ready",
        ):
            assert expected in paths
