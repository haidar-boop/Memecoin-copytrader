"""Idempotent persistence of parsed transactions.

All writes are safe to replay: entity upserts converge, event-table inserts
dedupe on their composite primary keys, and position updates are driven only
by trades that were actually inserted in this call.

Position math (SOL terms): positions aggregate WSOL-quoted trades directly;
stable-quoted trades are converted using the current SOL/USD price when the
enrichment loop has cached one, and skipped for position math (still stored
as trades) when it hasn't.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DexPool,
    FailedTransaction,
    Position,
    Token,
    Trade,
    Transaction,
    Wallet,
)
from app.ingestion.events import Side, SwapEvent
from app.ingestion.parsers import util
from app.ingestion.programs import STABLE_MINTS, WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

# A position is closed once the remaining balance is below this fraction of
# everything bought (wallets rarely sell the exact dust-level remainder).
DUST_FRACTION = Decimal("0.005")


def _is_postgres(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def upsert_wallet(session: AsyncSession, address: str, seen_at: datetime) -> int:
    if _is_postgres(session):
        stmt = pg_insert(Wallet).values(
            address=address, first_seen_at=seen_at, last_seen_at=seen_at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Wallet.address],
            set_={
                "first_seen_at": func.least(stmt.excluded.first_seen_at, Wallet.first_seen_at),
                "last_seen_at": func.greatest(stmt.excluded.last_seen_at, Wallet.last_seen_at),
            },
        ).returning(Wallet.id)
        return (await session.execute(stmt)).scalar_one()

    wallet = (
        await session.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(address=address, first_seen_at=seen_at, last_seen_at=seen_at)
        session.add(wallet)
        await session.flush()
    else:
        wallet.first_seen_at = min(_aware(wallet.first_seen_at), seen_at)
        wallet.last_seen_at = max(_aware(wallet.last_seen_at), seen_at)
    return wallet.id


async def upsert_token(session: AsyncSession, mint: str, seen_at: datetime, dex: str) -> int:
    if _is_postgres(session):
        stmt = pg_insert(Token).values(mint=mint, first_seen_at=seen_at, primary_dex=dex)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Token.mint],
            set_={"first_seen_at": func.least(stmt.excluded.first_seen_at, Token.first_seen_at)},
        ).returning(Token.id)
        return (await session.execute(stmt)).scalar_one()

    token = (
        await session.execute(select(Token).where(Token.mint == mint))
    ).scalar_one_or_none()
    if token is None:
        token = Token(mint=mint, first_seen_at=seen_at, primary_dex=dex)
        session.add(token)
        await session.flush()
    else:
        token.first_seen_at = min(_aware(token.first_seen_at), seen_at)
    return token.id


async def get_or_create_pool(
    session: AsyncSession, event: SwapEvent, token_id: int, seen_at: datetime
) -> int | None:
    if not event.pool_address:
        return None
    pool = (
        await session.execute(select(DexPool).where(DexPool.address == event.pool_address))
    ).scalar_one_or_none()
    if pool is not None:
        return pool.id
    pool = DexPool(
        address=event.pool_address,
        dex=event.dex.value,
        token_id=token_id,
        base_mint=event.token_mint,
        quote_mint=event.quote_mint,
        first_seen_at=seen_at,
    )
    session.add(pool)
    await session.flush()
    return pool.id


async def insert_transaction(
    session: AsyncSession,
    tx: dict,
    wallet_id: int | None,
    store_raw: bool = False,
) -> None:
    values = {
        "signature": (tx.get("transaction", {}).get("signatures") or [""])[0],
        "block_time": util.block_time(tx),
        "slot": int(tx.get("slot", 0)),
        "wallet_id": wallet_id,
        "fee_lamports": tx.get("meta", {}).get("fee"),
        "success": util.is_success(tx),
        "error": None if util.is_success(tx) else str(tx.get("meta", {}).get("err")),
        "program_ids": sorted(util.program_ids(tx)),
        "raw": tx if store_raw else None,
    }
    if _is_postgres(session):
        await session.execute(pg_insert(Transaction).values(**values).on_conflict_do_nothing())
    else:
        exists = await session.get(Transaction, (values["signature"], values["block_time"]))
        if exists is None:
            session.add(Transaction(**values))
            await session.flush()


async def insert_failed_transaction(
    session: AsyncSession, tx: dict, wallet_id: int | None
) -> None:
    values = {
        "signature": (tx.get("transaction", {}).get("signatures") or [""])[0],
        "block_time": util.block_time(tx),
        "slot": int(tx.get("slot", 0)),
        "wallet_id": wallet_id,
        "error": str(tx.get("meta", {}).get("err")),
        "program_ids": sorted(util.program_ids(tx)),
        "fee_lamports": tx.get("meta", {}).get("fee"),
    }
    if _is_postgres(session):
        await session.execute(
            pg_insert(FailedTransaction).values(**values).on_conflict_do_nothing()
        )
    else:
        exists = await session.get(FailedTransaction, (values["signature"], values["block_time"]))
        if exists is None:
            session.add(FailedTransaction(**values))
            await session.flush()


async def insert_trade(
    session: AsyncSession,
    event: SwapEvent,
    wallet_id: int,
    token_id: int,
    pool_id: int | None,
    sol_price_usd: Decimal | None,
) -> bool:
    """Insert a trade row; returns True when the row is new."""
    price_usd = None
    if event.price_quote_per_token is not None and sol_price_usd is not None:
        if event.quote_mint == WSOL_MINT:
            price_usd = event.price_quote_per_token * sol_price_usd
        elif event.quote_mint in STABLE_MINTS:
            price_usd = event.price_quote_per_token
    # Token-to-token legs quote in the OTHER token's units; storing that as
    # price_quote poisons any stat that averages prices for the token. Only
    # SOL/stable quotes are prices.
    price_quote = (
        event.price_quote_per_token
        if event.quote_mint == WSOL_MINT or event.quote_mint in STABLE_MINTS
        else None
    )
    values = {
        "signature": event.signature,
        "event_index": event.event_index,
        "block_time": event.block_time,
        "slot": event.slot,
        "wallet_id": wallet_id,
        "token_id": token_id,
        "pool_id": pool_id,
        "dex": event.dex.value,
        "aggregator": event.aggregator,
        "side": event.side.value,
        "token_amount": event.token_amount,
        "quote_amount": event.quote_amount,
        "quote_mint": event.quote_mint,
        "price_quote": price_quote,
        "price_usd": price_usd,
        "sol_price_usd": sol_price_usd,
        "program_id": event.program_id,
    }
    if _is_postgres(session):
        result = await session.execute(
            pg_insert(Trade).values(**values).on_conflict_do_nothing()
        )
        return bool(result.rowcount)
    exists = await session.get(
        Trade, (values["signature"], values["event_index"], values["block_time"])
    )
    if exists is not None:
        return False
    session.add(Trade(**values))
    await session.flush()
    return True


def _quote_in_sol(event: SwapEvent, sol_price_usd: Decimal | None) -> Decimal | None:
    if event.quote_mint == WSOL_MINT:
        return event.quote_amount
    if event.quote_mint in STABLE_MINTS and sol_price_usd and sol_price_usd > 0:
        # Near-real-time approximation: the *current* SOL price converts a
        # stable-quoted trade. Good within seconds of block time (live
        # ingestion); historical backfills should skip position math instead.
        return event.quote_amount / sol_price_usd
    return None


async def update_position(
    session: AsyncSession,
    event: SwapEvent,
    wallet_id: int,
    token_id: int,
    sol_price_usd: Decimal | None,
) -> None:
    """Fold one *newly inserted* trade into the wallet's open position."""
    quote_sol = _quote_in_sol(event, sol_price_usd)
    if quote_sol is None:
        return

    stmt = select(Position).where(
        Position.wallet_id == wallet_id,
        Position.token_id == token_id,
        Position.status == "open",
    )
    if _is_postgres(session):
        stmt = stmt.with_for_update()
    position = (await session.execute(stmt)).scalar_one_or_none()

    now = event.block_time
    if position is None:
        position = Position(
            wallet_id=wallet_id,
            token_id=token_id,
            status="open",
            opened_at=now,
            bought_tokens=Decimal(0),
            sold_tokens=Decimal(0),
            bought_sol=Decimal(0),
            sold_sol=Decimal(0),
            remaining_tokens=Decimal(0),
            realized_pnl_sol=Decimal(0),
            trade_count=0,
        )
        session.add(position)

    position.trade_count += 1
    position.last_trade_at = now
    position.updated_at = now

    if event.side == Side.BUY:
        position.bought_tokens += event.token_amount
        position.bought_sol += quote_sol
        position.avg_entry_price = (
            position.bought_sol / position.bought_tokens if position.bought_tokens else None
        )
    else:
        position.sold_tokens += event.token_amount
        position.sold_sol += quote_sol
        position.avg_exit_price = (
            position.sold_sol / position.sold_tokens if position.sold_tokens else None
        )
        # Cost basis of the sold chunk at average entry; zero basis for
        # tokens acquired outside observed trades (airdrops, transfers).
        entry = position.avg_entry_price or Decimal(0)
        position.realized_pnl_sol += quote_sol - entry * event.token_amount

    position.remaining_tokens = position.bought_tokens - position.sold_tokens

    dust_level = position.bought_tokens * DUST_FRACTION
    if position.bought_tokens > 0 and position.remaining_tokens <= dust_level:
        position.status = "closed"
        position.closed_at = now
        position.hold_time_seconds = int((now - _aware(position.opened_at)).total_seconds())
        if position.bought_sol > 0:
            position.roi = position.realized_pnl_sol / position.bought_sol
    elif position.bought_tokens == 0 and position.sold_tokens > 0:
        # Pure sell of tokens we never saw bought: close immediately.
        position.status = "closed"
        position.closed_at = now
        position.hold_time_seconds = 0

    await session.flush()


async def persist_parsed_transaction(
    session: AsyncSession,
    tx: dict,
    events: list[SwapEvent],
    sol_price_usd: Decimal | None,
    store_raw: bool = False,
) -> list[SwapEvent]:
    """Persist one fetched transaction and its parsed events.

    Returns the events that resulted in *new* trade rows (for publishing).
    """
    seen_at = util.block_time(tx)
    payer = util.fee_payer(tx)
    payer_wallet_id = await upsert_wallet(session, payer, seen_at) if payer else None

    if not util.is_success(tx):
        await insert_failed_transaction(session, tx, payer_wallet_id)
        await insert_transaction(session, tx, payer_wallet_id, store_raw=store_raw)
        return []

    await insert_transaction(session, tx, payer_wallet_id, store_raw=store_raw)

    new_events: list[SwapEvent] = []
    for event in events:
        wallet_id = (
            payer_wallet_id
            if event.wallet == payer and payer_wallet_id is not None
            else await upsert_wallet(session, event.wallet, seen_at)
        )
        token_id = await upsert_token(session, event.token_mint, seen_at, event.dex.value)
        pool_id = await get_or_create_pool(session, event, token_id, seen_at)
        if await insert_trade(session, event, wallet_id, token_id, pool_id, sol_price_usd):
            await update_position(session, event, wallet_id, token_id, sol_price_usd)
            new_events.append(event)
    return new_events
