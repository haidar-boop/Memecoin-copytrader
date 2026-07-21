"""Redis helpers: client factory, ingest stream, dedup, pub/sub."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

TRADES_CHANNEL = "events:trades"
SOL_PRICE_KEY = "price:sol_usd"
HOLDERS_KEY_PREFIX = "holders:"


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
