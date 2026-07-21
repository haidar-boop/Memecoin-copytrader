"""Tests for the WebSocket framing helper and the notifications HTTP route.

Live socket delivery (pubsub -> WebSocket forwarding) is fiddly to drive over
Starlette's TestClient with an async pubsub stub, so it is validated through
manual integration testing, not here. These unit tests instead cover:

  * ``frame_message`` — the pure per-message framing helper.
  * ``GET /api/notifications`` — exercised via httpx ASGITransport against a
    minimal app whose ``app.state.redis`` is a FakeRedis.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from app.api.ws import frame_message, router
from app.services.redis import NOTIFICATIONS_CHANNEL


def test_frame_message_parses_json() -> None:
    frame = frame_message(NOTIFICATIONS_CHANNEL, json.dumps({"kind": "copied_buy"}))
    assert frame == {"channel": NOTIFICATIONS_CHANNEL, "data": {"kind": "copied_buy"}}


def test_frame_message_bytes_payload() -> None:
    frame = frame_message("events:trades", b'{"a": 1}')
    assert frame == {"channel": "events:trades", "data": {"a": 1}}


def test_frame_message_non_json_passthrough() -> None:
    frame = frame_message("events:trades", "not json")
    assert frame == {"channel": "events:trades", "data": "not json"}


def test_frame_message_already_decoded() -> None:
    frame = frame_message("events:trades", {"already": "dict"})
    assert frame == {"channel": "events:trades", "data": {"already": "dict"}}


class FakeRedis:
    def __init__(self, items: list[str] | None = None) -> None:
        self.lists = {"notifications:recent": list(items or [])}

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        bucket = self.lists.get(key, [])
        if stop == -1:
            return bucket[start:]
        return bucket[start : stop + 1]


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.state.redis = FakeRedis(
        [json.dumps({"kind": "copied_sell", "title": "b"}),
         json.dumps({"kind": "copied_buy", "title": "a"})]
    )
    return application


async def test_notifications_endpoint(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/notifications?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert [n["title"] for n in body] == ["b", "a"]


async def test_notifications_endpoint_limit_validation(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/notifications?limit=0")
    assert resp.status_code == 422
