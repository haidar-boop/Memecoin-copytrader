"""Redis helpers: client factory, ingest stream, dedup, pub/sub."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

TRADES_CHANNEL = "events:trades"
# Structured operator-facing notifications (copied buys/sells, safety trips,
# reports, market moves...). Consumed by the WebSocket broadcaster and the
# Telegram bot.
NOTIFICATIONS_CHANNEL = "events:notifications"
SOL_PRICE_KEY = "price:sol_usd"
HOLDERS_KEY_PREFIX = "holders:"
# JSON list of wallet addresses the copy engine follows (tracked + above the
# auto-follow confidence bar). Written by the analytics follow-lane publisher
# and the track API; read by the WS listener for per-wallet subscriptions.
FOLLOWED_WALLETS_KEY = "follow:wallets"


async def get_followed_wallets(redis: aioredis.Redis) -> list[str]:
    """Current followed-wallet set, empty on any error (lane degrades off)."""
    try:
        raw = await redis.get(FOLLOWED_WALLETS_KEY)
        parsed = json.loads(raw) if raw else []
        return [str(a) for a in parsed] if isinstance(parsed, list) else []
    except Exception:  # noqa: BLE001
        return []


async def set_followed_wallets(redis: aioredis.Redis, addresses: list[str]) -> None:
    """Atomically replace the followed-wallet set (sorted for cheap compare)."""
    await redis.set(FOLLOWED_WALLETS_KEY, json.dumps(sorted(set(addresses))))


def create_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(url, decode_responses=True)


async def ensure_group(redis: aioredis.Redis, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:  # group already exists
        if "BUSYGROUP" not in str(exc):
            raise


async def try_dedup(redis: aioredis.Redis, key: str, ttl_seconds: int) -> bool:
    """Returns True when the key was newly claimed (i.e. not a duplicate)."""
    return bool(await redis.set(key, "1", nx=True, ex=ttl_seconds))


async def publish_json(redis: aioredis.Redis, channel: str, payload: dict[str, Any]) -> None:
    await redis.publish(channel, json.dumps(payload, default=str))
