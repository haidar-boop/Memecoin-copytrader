"""Holder counting for active tokens via getProgramAccounts.

Counts SPL token accounts for a mint (dataSize 165, mint at offset 0) and
caches the result in Redis under ``HOLDERS_KEY_PREFIX + mint`` so the snapshot
job can read it without paying the (expensive) scan itself. The whole job is
gated on ``settings.holders_enabled`` by the enrichment loop — public RPCs
often refuse or throttle getProgramAccounts on the token program.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token, Trade
from app.ingestion.programs import TOKEN_PROGRAM
from app.logging_config import get_logger
from app.services.redis import HOLDERS_KEY_PREFIX

log = get_logger(__name__)

TOKEN_ACCOUNT_SIZE = 165  # bytes of a classic SPL token account
MINT_OFFSET = 0  # mint pubkey position inside a token account


class _Rpc(Protocol):
    async def get_program_accounts_count(
        self, program_id: str, filters: list[dict] | None = None
    ) -> int: ...


class _Redis(Protocol):
    async def set(self, key: str, value: str, ex: int | None = None) -> object: ...


async def count_holders(rpc: _Rpc, mint: str) -> int:
    """Number of SPL token accounts holding ``mint`` (upper bound on holders)."""
    return await rpc.get_program_accounts_count(
        TOKEN_PROGRAM,
        filters=[
            {"dataSize": TOKEN_ACCOUNT_SIZE},
            {"memcmp": {"offset": MINT_OFFSET, "bytes": mint}},
        ],
    )


async def run_once(
    session: AsyncSession,
    rpc: _Rpc,
    redis: _Redis,
    *,
    active_since: datetime,
    limit: int,
    cache_ttl_seconds: int,
) -> int:
    """Count holders for tokens traded since ``active_since``; cache in Redis.

    Returns the number of mints whose counts were cached this cycle.
    """
    active_token_ids = (
        (
            await session.execute(
                select(Trade.token_id)
                .where(Trade.block_time >= active_since)
                .group_by(Trade.token_id)
                .order_by(func.count().desc(), Trade.token_id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not active_token_ids:
        return 0

    mints = (
        (await session.execute(select(Token.mint).where(Token.id.in_(active_token_ids))))
        .scalars()
        .all()
    )
    cached = 0
    for mint in mints:
        try:
            count = await count_holders(rpc, mint)
        except Exception as exc:
            log.warning("holder_count_failed", mint=mint, error=str(exc))
            continue
        await redis.set(HOLDERS_KEY_PREFIX + mint, str(int(count)), ex=cache_ttl_seconds)
        cached += 1
    log.info("holders_cycle", tokens=len(mints), cached=cached)
    return cached
