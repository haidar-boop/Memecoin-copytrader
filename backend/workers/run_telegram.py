"""Entry point: Telegram bot worker (python -m workers.run_telegram)."""

from __future__ import annotations

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.services.redis import create_redis
from telegram_bot.bot import TelegramNotifier
from workers.base import run_worker


async def main(settings: Settings) -> None:
    redis = create_redis(settings.redis_url)
    engine = build_engine(settings)
    try:
        await TelegramNotifier(settings, redis, build_session_factory(engine)).run()
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    run_worker("telegram", main)
