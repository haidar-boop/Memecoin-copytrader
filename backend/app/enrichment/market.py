"""SOL/USD price refresh plus hourly market-wide aggregates.

The price comes from a Jupiter price-v2 endpoint
(``{"data": {"<mint>": {"price": "..."}}}``) and is parsed defensively:
malformed payloads and HTTP failures never crash the cycle — the previously
cached Redis value keeps serving until a fetch succeeds again. HTTP goes
through an injectable async ``fetch`` callable so tests never touch the
network; the default is a one-shot httpx GET.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FailedTransaction, MarketSnapshot, Token, Trade
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger
from app.services.redis import SOL_PRICE_KEY

log = get_logger(__name__)

PriceFetch = Callable[[str], Awaitable[Any]]


class _Redis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None) -> object: ...


async def http_fetch_json(url: str) -> Any:
    """Default fetch: one-shot GET returning the parsed JSON body."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def parse_sol_price(payload: object) -> Decimal | None:
    """Extract SOL/USD from a Jupiter price-v2 payload. Pure; None on any defect."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    entry = data.get(WSOL_MINT)
    if not isinstance(entry, dict):
        # Defensive fallback: some deployments key by symbol instead of mint.
        entry = next((item for item in data.values() if isinstance(item, dict)), None)
    if not isinstance(entry, dict):
        return None
    price = _dec(entry.get("price"))
    if price is None or price <= 0:
        return None
    return price


async def refresh_sol_price(redis: _Redis, url: str, fetch: PriceFetch) -> Decimal | None:
    """Fetch and cache the SOL price; fall back to the cached value on failure."""
    price: Decimal | None = None
    try:
        payload = await fetch(url)
    except Exception as exc:
        log.warning("sol_price_fetch_failed", url=url, error=str(exc))
    else:
        price = parse_sol_price(payload)
        if price is None:
            log.warning("sol_price_malformed", url=url)
    if price is not None:
        await redis.set(SOL_PRICE_KEY, str(price))
        return price
    return _dec(await redis.get(SOL_PRICE_KEY))


async def run_once(
    session: AsyncSession,
    redis: _Redis,
    *,
    price_url: str,
    fetch: PriceFetch | None = None,
    now: datetime | None = None,
) -> MarketSnapshot:
    """One market pass: refresh the SOL price and insert one MarketSnapshot row."""
    now = now or datetime.now(tz=UTC)
    cutoff = now - timedelta(hours=1)
    sol_price = await refresh_sol_price(redis, price_url, fetch or http_fetch_json)

    trades_1h = (
        await session.execute(
            select(func.count()).select_from(Trade).where(Trade.block_time >= cutoff)
        )
    ).scalar_one()
    volume_sol_1h = (
        await session.execute(
            select(func.sum(Trade.quote_amount)).where(
                Trade.block_time >= cutoff, Trade.quote_mint == WSOL_MINT
            )
        )
    ).scalar_one()
    active_wallets_1h = (
        await session.execute(
            select(func.count(func.distinct(Trade.wallet_id))).where(Trade.block_time >= cutoff)
        )
    ).scalar_one()
    tokens_launched_1h = (
        await session.execute(
            select(func.count()).select_from(Token).where(Token.first_seen_at >= cutoff)
        )
    ).scalar_one()
    failed_tx_1h = (
        await session.execute(
            select(func.count())
            .select_from(FailedTransaction)
            .where(FailedTransaction.block_time >= cutoff)
        )
    ).scalar_one()

    snapshot = MarketSnapshot(
        ts=now,
        sol_price_usd=sol_price,
        trades_1h=int(trades_1h),
        volume_sol_1h=_dec(volume_sol_1h) or Decimal(0),
        active_wallets_1h=int(active_wallets_1h),
        tokens_launched_1h=int(tokens_launched_1h),
        failed_tx_1h=int(failed_tx_1h),
    )
    session.add(snapshot)
    await session.commit()
    log.info(
        "market_snapshot_cycle",
        sol_price_usd=str(sol_price) if sol_price is not None else None,
        trades_1h=snapshot.trades_1h,
    )
    return snapshot
