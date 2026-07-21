"""Position lifecycle and idempotency tests for the ingest writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Position, Trade, Wallet
from app.ingestion import writer
from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.programs import USDC_MINT, WSOL_MINT

TRADER = "TraderWa11et111111111111111111111111111111111"
MEME = "MemeMint11111111111111111111111111111111111111"

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def make_event(
    side: Side,
    token_amount: str,
    quote_amount: str,
    *,
    signature: str = "sig-1",
    event_index: int = 0,
    minutes: int = 0,
    quote_mint: str = WSOL_MINT,
) -> SwapEvent:
    token = Decimal(token_amount)
    quote = Decimal(quote_amount)
    return SwapEvent(
        signature=signature,
        slot=1,
        block_time=T0 + timedelta(minutes=minutes),
        wallet=TRADER,
        dex=Dex.PUMPFUN,
        side=side,
        token_mint=MEME,
        quote_mint=quote_mint,
        token_amount=token,
        quote_amount=quote,
        price_quote_per_token=quote / token,
    )


async def apply(session: AsyncSession, event: SwapEvent, sol_price: Decimal | None = None) -> bool:
    wallet_id = await writer.upsert_wallet(session, event.wallet, event.block_time)
    token_id = await writer.upsert_token(session, event.token_mint, event.block_time, event.dex.value)
    inserted = await writer.insert_trade(session, event, wallet_id, token_id, None, sol_price)
    if inserted:
        await writer.update_position(session, event, wallet_id, token_id, sol_price)
    await session.commit()
    return inserted


async def get_position(session: AsyncSession) -> Position:
    return (await session.execute(select(Position))).scalar_one()


def approx(value: Decimal, expected: str, tolerance: str = "1e-9") -> bool:
    """SQLite stores NUMERIC as float; PostgreSQL is exact. Compare loosely."""
    return abs(value - Decimal(expected)) < Decimal(tolerance)


async def test_buy_opens_position(db_session: AsyncSession) -> None:
    await apply(db_session, make_event(Side.BUY, "1000", "2"))
    position = await get_position(db_session)
    assert position.status == "open"
    assert position.bought_tokens == Decimal("1000")
    assert position.bought_sol == Decimal("2")
    assert position.avg_entry_price == Decimal("0.002")
    assert position.remaining_tokens == Decimal("1000")


async def test_full_roundtrip_closes_with_pnl(db_session: AsyncSession) -> None:
    await apply(db_session, make_event(Side.BUY, "1000", "2"))
    await apply(
        db_session,
        make_event(Side.SELL, "1000", "3", signature="sig-2", minutes=30),
    )
    position = await get_position(db_session)
    assert position.status == "closed"
    assert position.realized_pnl_sol == Decimal("1")  # 3 received - 2 cost basis
    assert position.roi == Decimal("0.5")
    assert position.hold_time_seconds == 30 * 60
    assert position.closed_at is not None


async def test_partial_sell_keeps_position_open(db_session: AsyncSession) -> None:
    await apply(db_session, make_event(Side.BUY, "1000", "2"))
    await apply(db_session, make_event(Side.SELL, "400", "1.6", signature="sig-2", minutes=5))
    position = await get_position(db_session)
    assert position.status == "open"
    assert position.remaining_tokens == Decimal("600")
    # Sold 400 at 0.004 vs entry 0.002 -> pnl 1.6 - 0.8 = 0.8
    assert approx(position.realized_pnl_sol, "0.8")


async def test_reentry_creates_second_position(db_session: AsyncSession) -> None:
    await apply(db_session, make_event(Side.BUY, "100", "1"))
    await apply(db_session, make_event(Side.SELL, "100", "2", signature="sig-2", minutes=1))
    await apply(db_session, make_event(Side.BUY, "50", "1", signature="sig-3", minutes=10))
    positions = list((await db_session.execute(select(Position))).scalars())
    assert len(positions) == 2
    assert {p.status for p in positions} == {"open", "closed"}


async def test_duplicate_trade_is_idempotent(db_session: AsyncSession) -> None:
    event = make_event(Side.BUY, "1000", "2")
    assert await apply(db_session, event) is True
    assert await apply(db_session, event) is False
    trades = list((await db_session.execute(select(Trade))).scalars())
    assert len(trades) == 1
    position = await get_position(db_session)
    assert position.bought_tokens == Decimal("1000")  # not double-counted


async def test_stable_quote_skips_position_without_sol_price(db_session: AsyncSession) -> None:
    event = make_event(Side.BUY, "1000", "150", quote_mint=USDC_MINT)
    await apply(db_session, event, sol_price=None)
    assert (await db_session.execute(select(Position))).scalar_one_or_none() is None
    assert len(list((await db_session.execute(select(Trade))).scalars())) == 1


async def test_stable_quote_converts_with_sol_price(db_session: AsyncSession) -> None:
    event = make_event(Side.BUY, "1000", "150", quote_mint=USDC_MINT)
    await apply(db_session, event, sol_price=Decimal("150"))
    position = await get_position(db_session)
    assert position.bought_sol == Decimal("1")


def _naive(dt) -> object:
    return dt.replace(tzinfo=None)


async def test_wallet_first_last_seen_converge(db_session: AsyncSession) -> None:
    late = make_event(Side.BUY, "10", "1", minutes=60)
    early = make_event(Side.BUY, "10", "1", signature="sig-2", minutes=0)
    await apply(db_session, late)
    await apply(db_session, early)
    wallet = (await db_session.execute(select(Wallet))).scalar_one()
    # SQLite returns naive datetimes; compare on the naive value.
    assert _naive(wallet.first_seen_at) == _naive(early.block_time)
    assert _naive(wallet.last_seen_at) == _naive(late.block_time)
