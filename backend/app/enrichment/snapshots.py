"""Per-token market snapshots. Append-only rows in ``token_snapshots``.

Every cycle picks the tokens that traded during the last interval (most
active first, capped) and inserts one snapshot row per token: price from the
latest WSOL-quoted trade, 5m VWAP, WSOL-quoted volume over 5m/1h/24h windows,
trade/buyer counts, holder count from the Redis cache the holders job
maintains, pool liquidity via :mod:`app.enrichment.liquidity`, and USD
figures when the market job has cached a SOL price. Rows are only ever
inserted — history is training data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token, TokenSnapshot, Trade
from app.enrichment import liquidity
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger
from app.services.redis import HOLDERS_KEY_PREFIX, SOL_PRICE_KEY

log = get_logger(__name__)


class _Rpc(Protocol):
    async def get_balance(self, pubkey: str) -> int | None: ...

    async def get_token_account_balance(self, account: str) -> dict | None: ...


class _Redis(Protocol):
    async def get(self, key: str) -> str | None: ...


def _dec(value: object) -> Decimal | None:
    """Coerce driver/Redis output to a finite Decimal (SQLite hands back floats)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


async def _active_token_ids(
    session: AsyncSession, cutoff: datetime, limit: int
) -> list[int]:
    return list(
        (
            await session.execute(
                select(Trade.token_id)
                .where(Trade.block_time >= cutoff)
                .group_by(Trade.token_id)
                .order_by(func.count().desc(), Trade.token_id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def _wsol_sums(
    session: AsyncSession, token_ids: list[int], cutoff: datetime
) -> dict[int, tuple[Decimal, Decimal]]:
    """token_id -> (sum quote_amount, sum token_amount) over WSOL-quoted trades."""
    rows = await session.execute(
        select(Trade.token_id, func.sum(Trade.quote_amount), func.sum(Trade.token_amount))
        .where(
            Trade.token_id.in_(token_ids),
            Trade.block_time >= cutoff,
            Trade.quote_mint == WSOL_MINT,
        )
        .group_by(Trade.token_id)
    )
    return {
        token_id: (_dec(quote) or Decimal(0), _dec(base) or Decimal(0))
        for token_id, quote, base in rows
    }


async def _trade_counts(
    session: AsyncSession,
    token_ids: list[int],
    cutoff: datetime,
    *,
    buys_only: bool = False,
    distinct_wallets: bool = False,
) -> dict[int, int]:
    metric = func.count(func.distinct(Trade.wallet_id)) if distinct_wallets else func.count()
    stmt = (
        select(Trade.token_id, metric)
        .where(Trade.token_id.in_(token_ids), Trade.block_time >= cutoff)
        .group_by(Trade.token_id)
    )
    if buys_only:
        stmt = stmt.where(Trade.side == Side.BUY.value)
    rows = await session.execute(stmt)
    return {token_id: int(count) for token_id, count in rows}


async def _latest_wsol_price(session: AsyncSession, token_id: int) -> Decimal | None:
    value = (
        await session.execute(
            select(Trade.price_quote)
            .where(
                Trade.token_id == token_id,
                Trade.quote_mint == WSOL_MINT,
                Trade.price_quote.is_not(None),
            )
            .order_by(Trade.block_time.desc(), Trade.slot.desc(), Trade.event_index.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _dec(value)


async def _holder_count(redis: _Redis, mint: str) -> int | None:
    cached = await redis.get(HOLDERS_KEY_PREFIX + mint)
    if cached is None:
        return None
    try:
        return int(cached)
    except (TypeError, ValueError):
        return None


async def run_once(
    session: AsyncSession,
    rpc: _Rpc,
    redis: _Redis,
    *,
    interval_seconds: int,
    active_token_limit: int,
    now: datetime | None = None,
) -> int:
    """One snapshot pass; returns the number of TokenSnapshot rows inserted."""
    now = now or datetime.now(tz=UTC)
    token_ids = await _active_token_ids(
        session, now - timedelta(seconds=interval_seconds), active_token_limit
    )
    if not token_ids:
        return 0

    cutoff_5m = now - timedelta(minutes=5)
    cutoff_1h = now - timedelta(hours=1)
    cutoff_24h = now - timedelta(hours=24)

    sums_5m = await _wsol_sums(session, token_ids, cutoff_5m)
    sums_1h = await _wsol_sums(session, token_ids, cutoff_1h)
    sums_24h = await _wsol_sums(session, token_ids, cutoff_24h)
    trades_5m = await _trade_counts(session, token_ids, cutoff_5m)
    trades_1h = await _trade_counts(session, token_ids, cutoff_1h)
    buyers_5m = await _trade_counts(
        session, token_ids, cutoff_5m, buys_only=True, distinct_wallets=True
    )
    prices = {token_id: await _latest_wsol_price(session, token_id) for token_id in token_ids}

    tokens = (
        (await session.execute(select(Token).where(Token.id.in_(token_ids)))).scalars().all()
    )
    token_by_id = {token.id: token for token in tokens}
    liquidity_by_token = await liquidity.fetch_liquidity(session, rpc, token_ids)
    sol_price = _dec(await redis.get(SOL_PRICE_KEY))

    rows: list[dict] = []
    for token_id in token_ids:
        token = token_by_id.get(token_id)
        price_sol = prices[token_id]
        quote_5m, base_5m = sums_5m.get(token_id, (Decimal(0), Decimal(0)))
        vwap_5m = quote_5m / base_5m if base_5m > 0 else None
        price_usd = (
            price_sol * sol_price if price_sol is not None and sol_price is not None else None
        )
        supply = _dec(token.supply) if token is not None else None
        market_cap_usd = (
            price_usd * supply if price_usd is not None and supply is not None else None
        )
        holder_count = await _holder_count(redis, token.mint) if token is not None else None
        rows.append(
            {
                "token_id": token_id,
                "ts": now,
                "price_sol": price_sol,
                "price_usd": price_usd,
                "vwap_sol_5m": vwap_5m,
                "market_cap_usd": market_cap_usd,
                "liquidity_sol": liquidity_by_token.get(token_id),
                "volume_sol_5m": quote_5m,
                "volume_sol_1h": sums_1h.get(token_id, (Decimal(0), Decimal(0)))[0],
                "volume_sol_24h": sums_24h.get(token_id, (Decimal(0), Decimal(0)))[0],
                "trades_5m": trades_5m.get(token_id, 0),
                "trades_1h": trades_1h.get(token_id, 0),
                "buyers_5m": buyers_5m.get(token_id, 0),
                "holder_count": holder_count,
            }
        )
    # Core executemany INSERT: append-only by construction, and it avoids the
    # ORM's RETURNING-based sentinel matching, which SQLite's naive datetimes
    # cannot satisfy for composite time-keyed primary keys.
    await session.execute(insert(TokenSnapshot), rows)
    await session.commit()
    log.info("token_snapshot_cycle", tokens=len(rows))
    return len(rows)
