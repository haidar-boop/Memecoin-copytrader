"""Best-effort pool liquidity, in SOL, for the snapshot job.

Two measurable pool shapes:
- pump.fun bonding curves hold native SOL on the curve account itself, so
  ``getBalance(pool_address)`` is the pool's SOL side.
- AMM pools that recorded a ``quote_vault`` with a WSOL quote mint expose the
  SOL side via ``getTokenAccountBalance(quote_vault)``.

Anything else (stable-quoted pools, pools without vault attribution) is
skipped: tokens with no readable pool are simply absent from the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DexPool
from app.ingestion.events import Dex
from app.ingestion.parsers.util import LAMPORTS_PER_SOL
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)


class _Rpc(Protocol):
    async def get_balance(self, pubkey: str) -> int | None: ...

    async def get_token_account_balance(self, account: str) -> dict | None: ...


async def _pool_liquidity_sol(rpc: _Rpc, pool: DexPool) -> Decimal | None:
    """SOL depth of one pool, or None when it cannot be measured."""
    if pool.dex == Dex.PUMPFUN.value:
        lamports = await rpc.get_balance(pool.address)
        if lamports is None:
            return None
        return Decimal(int(lamports)) / LAMPORTS_PER_SOL
    if pool.quote_vault and pool.quote_mint == WSOL_MINT:
        value = await rpc.get_token_account_balance(pool.quote_vault)
        ui_amount = (value or {}).get("uiAmountString")
        if ui_amount is None:
            return None
        try:
            return Decimal(str(ui_amount))
        except (InvalidOperation, ValueError, TypeError):
            return None
    return None


async def fetch_liquidity(
    session: AsyncSession, rpc: _Rpc, token_ids: Sequence[int]
) -> dict[int, Decimal]:
    """Map token_id -> SOL liquidity of its deepest measurable pool.

    Best-effort by design: RPC failures on individual pools are logged and
    skipped, and tokens with no measurable pool do not appear in the result.
    """
    if not token_ids:
        return {}
    pools = (
        (
            await session.execute(
                select(DexPool).where(DexPool.token_id.in_(list(token_ids))).order_by(DexPool.id)
            )
        )
        .scalars()
        .all()
    )
    result: dict[int, Decimal] = {}
    for pool in pools:
        try:
            sol = await _pool_liquidity_sol(rpc, pool)
        except Exception as exc:
            log.warning("liquidity_probe_failed", pool=pool.address, error=str(exc))
            continue
        if sol is None:
            continue
        current = result.get(pool.token_id)
        if current is None or sol > current:
            result[pool.token_id] = sol
    return result
