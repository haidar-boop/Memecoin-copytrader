"""Entry point: ingest writer (python -m workers.run_ingest_writer)."""

from __future__ import annotations

import os

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.ingestion.fetcher import IngestWriter
from app.services.redis import create_redis
from app.services.rpc import RpcBudget, SolanaRpc
from workers.base import run_worker


async def main(settings: Settings) -> None:
    redis = create_redis(settings.redis_url)
    engine = build_engine(settings)
    rpc = SolanaRpc(
        settings.solana_rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        max_retries=settings.rpc_max_retries,
        requests_per_second=settings.rpc_requests_per_second,
        budget=RpcBudget(redis, settings.rpc_daily_credit_budget),
        priority_budget=RpcBudget(
            redis,
            settings.rpc_priority_daily_credit_budget,
            key_prefix=RpcBudget.PRIORITY_KEY_PREFIX,
        ),
        redis=redis,
    )
    consumer = f"writer-{os.environ.get('HOSTNAME', os.getpid())}"
    try:
        await IngestWriter(
            settings, redis, rpc, build_session_factory(engine), consumer_name=consumer
        ).run()
    finally:
        await rpc.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    run_worker("ingest_writer", main)
