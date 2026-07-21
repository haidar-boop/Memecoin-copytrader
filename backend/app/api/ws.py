"""WebSocket live-update endpoint plus a recent-notifications HTTP route.

``GET /ws/live`` streams trade events and operator notifications to the client:
the recent notification backlog is sent first, then live messages from both
:data:`TRADES_CHANNEL` and :data:`NOTIFICATIONS_CHANNEL` are forwarded as
``{"channel": ..., "data": ...}`` frames.

``GET /api/notifications`` returns the recent notification buffer over HTTP.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_redis
from app.auth.deps import require_auth_if_configured, ws_authorized
from app.config import get_settings
from app.logging_config import get_logger
from app.services.notifications import NotificationService
from app.services.redis import NOTIFICATIONS_CHANNEL, TRADES_CHANNEL

log = get_logger(__name__)

router = APIRouter(tags=["ws"])


def frame_message(channel: str, raw: Any) -> dict[str, Any]:
    """Build a client frame from a raw pub/sub payload.

    ``data`` is the JSON-parsed payload when possible; non-JSON (or already
    decoded) payloads are passed through as-is so a malformed message never
    breaks the stream.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    data: Any
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = raw
    else:
        data = raw
    return {"channel": channel, "data": data}


@router.get("/api/notifications")
async def get_notifications(
    limit: int = Query(50, ge=1, le=200),
    redis=Depends(get_redis),
    _user: str | None = Depends(require_auth_if_configured),
) -> list[dict[str, Any]]:
    # The notification feed is the operator's OWN trading activity (followed
    # wallets, sizes, PnL) — protected once auth is configured.
    service = NotificationService(redis)
    return await service.recent(limit)


@router.websocket("/ws/live")
async def live(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    # The live feed carries the operator's private notifications; when auth is
    # configured the handshake must present a valid token (query param, since
    # browsers can't set WebSocket Authorization headers).
    if not ws_authorized(token, get_settings()):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    redis = websocket.app.state.redis
    service = NotificationService(redis)

    # 1) Replay the recent notification backlog (newest-first from the buffer;
    #    send oldest-first so the client renders in chronological order).
    backlog = await service.recent(200)
    for payload in reversed(backlog):
        await websocket.send_json(frame_message(NOTIFICATIONS_CHANNEL, payload))

    # 2) Subscribe to live channels and forward each message.
    pubsub = redis.pubsub()
    await pubsub.subscribe(TRADES_CHANNEL, NOTIFICATIONS_CHANNEL)

    async def reader() -> None:
        async for message in pubsub.listen():
            if message is None or message.get("type") != "message":
                continue
            channel = message.get("channel")
            if isinstance(channel, (bytes, bytearray)):
                channel = channel.decode("utf-8", errors="replace")
            await websocket.send_json(frame_message(channel, message.get("data")))

    reader_task = asyncio.create_task(reader())
    try:
        # Block on client disconnects; the reader task pushes messages.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log.info("ws_client_disconnected")
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await pubsub.unsubscribe(TRADES_CHANNEL, NOTIFICATIONS_CHANNEL)
            await pubsub.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
