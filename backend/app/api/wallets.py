from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.analytics import WalletStatsOut, WalletStatsSnapshotOut
from app.api.deps import get_db
from app.api.schemas import PositionOut, TradeOut, WalletOut
from app.db.models import Position, Token, Trade, Wallet, WalletStats, WalletStatsSnapshot

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


@router.get("", response_model=list[WalletOut])
async def list_wallets(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tracked_only: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[Wallet]:
    stmt = select(Wallet).order_by(Wallet.last_seen_at.desc()).limit(limit).offset(offset)
    if tracked_only:
        stmt = stmt.where(Wallet.is_tracked.is_(True))
    return list((await db.execute(stmt)).scalars())


@router.get("/{address}", response_model=WalletOut)
async def get_wallet(address: str, db: AsyncSession = Depends(get_db)) -> Wallet:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return wallet


@router.get("/{address}/trades", response_model=list[TradeOut])
async def wallet_trades(
    address: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[TradeOut]:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    rows = await db.execute(
        select(Trade, Token.mint)
        .join(Token, Token.id == Trade.token_id)
        .where(Trade.wallet_id == wallet.id)
        .order_by(Trade.block_time.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        TradeOut(
            signature=trade.signature,
            event_index=trade.event_index,
            block_time=trade.block_time,
            slot=trade.slot,
            wallet_address=address,
            token_mint=mint,
            dex=trade.dex,
            aggregator=trade.aggregator,
            side=trade.side,
            token_amount=trade.token_amount,
            quote_amount=trade.quote_amount,
            quote_mint=trade.quote_mint,
            price_quote=trade.price_quote,
            price_usd=trade.price_usd,
        )
        for trade, mint in rows.all()
    ]


@router.get("/{address}/positions", response_model=list[PositionOut])
async def wallet_positions(
    address: str,
    status_filter: str | None = Query(None, pattern="^(open|closed)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[Position]:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    stmt = (
        select(Position)
        .where(Position.wallet_id == wallet.id)
        .order_by(Position.opened_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status_filter:
        stmt = stmt.where(Position.status == status_filter)
    return list((await db.execute(stmt)).scalars())


@router.get("/{address}/stats", response_model=WalletStatsOut)
async def wallet_stats(address: str, db: AsyncSession = Depends(get_db)) -> WalletStats:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    stats = (
        await db.execute(select(WalletStats).where(WalletStats.wallet_id == wallet.id))
    ).scalar_one_or_none()
    if stats is None:
        raise HTTPException(status_code=404, detail="wallet stats not computed yet")
    return stats


@router.get("/{address}/stats/history", response_model=list[WalletStatsSnapshotOut])
async def wallet_stats_history(
    address: str,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> list[WalletStatsSnapshot]:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(WalletStatsSnapshot)
            .where(
                WalletStatsSnapshot.wallet_id == wallet.id,
                WalletStatsSnapshot.ts >= since,
            )
            .order_by(WalletStatsSnapshot.ts.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)
