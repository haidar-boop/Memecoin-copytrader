"""Entry point: Solana WebSocket listener (python -m workers.run_listener)."""

from __future__ import annotations

from app.config import Settings
from app.ingestion.ws_listener import LogsListener
from app.services.redis import create_redis
from workers.base import run_worker


async def main(settings: Settings) -> None:
    redis = create_redis(settings.redis_url)
    try:
        await LogsListener(settings, redis).run()
    finally:
        await redis.aclose()


if __name__ == "__main__":
    run_worker("listener", main)
