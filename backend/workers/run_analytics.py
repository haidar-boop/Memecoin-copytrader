"""Entry point: analytics loops (python -m workers.run_analytics)."""

from __future__ import annotations

from app.analytics.runner import run_analytics_loop
from app.config import Settings
from app.db.session import build_engine, build_session_factory
from workers.base import run_worker


async def main(settings: Settings) -> None:
    engine = build_engine(settings)
    try:
        await run_analytics_loop(settings, build_session_factory(engine))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run_worker("analytics", main)
