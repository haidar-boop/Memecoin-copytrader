from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas import TradeOut
from app.db.models import Token, Trade, Wallet

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("/recent", response_model=list[TradeOut])
async def recent_trades(
    limit: int = Query(100, ge=1, le=1000),
    dex: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[TradeOut]:
    stmt = (
        select(Trade, Wallet.address, Token.mint)
        .join(Wallet, Wallet.id == Trade.wallet_id)
        .join(Token, Token.id == Trade.token_id)
        .order_by(Trade.block_time.desc())
        .limit(limit)
    )
    if dex:
        stmt = stmt.where(Trade.dex == dex)
    rows = await db.execute(stmt)
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
        for trade, address, mint in rows.all()
    ]
