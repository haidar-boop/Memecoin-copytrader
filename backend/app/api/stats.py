from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.config import get_settings
from app.db.models import FailedTransaction, Token, Trade, Transaction, Wallet

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Event tables grow into the billions; COUNT(*) over them would scan every
# chunk. Serve planner estimates (summed over Timescale chunk children via
# pg_inherits) and fall back to an exact count only while the estimate is
# unavailable (never analyzed).
_ESTIMATED_TABLES = {"transactions", "trades", "failed_transactions"}

_ESTIMATE_SQL = text(
    """
    SELECT COALESCE(sum(c.reltuples), -1)::bigint
    FROM pg_class c
    WHERE c.oid = CAST(:table AS regclass)
       OR c.oid IN (SELECT inhrelid FROM pg_inherits WHERE inhparent = CAST(:table AS regclass))
    """
)


async def _table_count(db: AsyncSession, name: str, model: type) -> dict:
    if name in _ESTIMATED_TABLES and db.get_bind().dialect.name == "postgresql":
        estimate = (await db.execute(_ESTIMATE_SQL, {"table": name})).scalar()
        if estimate is not None and estimate >= 0:
            return {"count": int(estimate), "estimate": True}
    exact = (await db.execute(select(func.count()).select_from(model))).scalar()
    return {"count": int(exact or 0), "estimate": False}


@router.get("/ingestion")
async def ingestion_stats(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    settings = get_settings()
    counts = {}
    for name, model in (
        ("wallets", Wallet),
        ("tokens", Token),
        ("transactions", Transaction),
        ("trades", Trade),
        ("failed_transactions", FailedTransaction),
    ):
        counts[name] = await _table_count(db, name, model)

    queue_depth = None
    listener_checkpoint: dict = {}
    rpc_budget: dict = {
        "limit": settings.rpc_daily_credit_budget or None,
        "used_today": None,
        "exhausted": False,
    }
    try:
        queue_depth = await redis.xlen(settings.ingest_stream_key)
        listener_checkpoint = await redis.hgetall("ingest:checkpoint:listener")
        from datetime import UTC, datetime

        from app.services.rpc import RpcBudget

        raw = await redis.get(
            RpcBudget.KEY_PREFIX + datetime.now(UTC).strftime("%Y-%m-%d")
        )
        if raw is not None:
            used = int(raw)
            rpc_budget["used_today"] = used
            rpc_budget["exhausted"] = bool(
                settings.rpc_daily_credit_budget
                and used > settings.rpc_daily_credit_budget
            )
    except Exception:
        pass

    return {
        "counts": counts,
        "queue_depth": queue_depth,
        "listener_checkpoint": listener_checkpoint,
        "rpc_budget": rpc_budget,
    }
