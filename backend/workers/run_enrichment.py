"""Entry point: enrichment loops (python -m workers.run_enrichment)."""

from __future__ import annotations

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.enrichment import run_enrichment_loop
from app.services.redis import create_redis
from app.services.rpc import SolanaRpc
from workers.base import run_worker


async def main(settings: Settings) -> None:
    redis = create_redis(settings.redis_url)
    engine = build_engine(settings)
    rpc = SolanaRpc(
        settings.solana_rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        max_retries=settings.rpc_max_retries,
        requests_per_second=settings.rpc_requests_per_second,
    )
    try:
        await run_enrichment_loop(settings, build_session_factory(engine), rpc, redis)
    finally:
        await rpc.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    run_worker("enrichment", main)
