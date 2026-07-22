"""Entry point: analytics loops (python -m workers.run_analytics)."""

from __future__ import annotations

from app.analytics.runner import run_analytics_loop
from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.services.redis import create_redis
from app.services.rpc import RpcBudget, SolanaRpc
from workers.base import run_worker


async def main(settings: Settings) -> None:
    engine = build_engine(settings)
    redis = create_redis(settings.redis_url)
    # Wallet vetting's funding probe is the only analytics RPC consumer;
    # it shares the global budget like every other worker.
    rpc = SolanaRpc(
        settings.solana_rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        max_retries=settings.rpc_max_retries,
        requests_per_second=settings.rpc_requests_per_second,
        budget=RpcBudget(redis, settings.rpc_daily_credit_budget),
    )
    try:
        await run_analytics_loop(settings, build_session_factory(engine), redis, rpc)
    finally:
        await rpc.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    run_worker("analytics", main)
