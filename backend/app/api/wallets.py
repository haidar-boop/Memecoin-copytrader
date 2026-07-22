from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.exc import IntegrityError

from app.api.analytics import WalletStatsOut, WalletStatsSnapshotOut
from app.api.deps import get_db, get_redis
from app.api.schemas import PositionOut, TradeOut, WalletOut
from app.auth.deps import require_auth
from app.db.models import (
    Position,
    Token,
    Trade,
    Wallet,
    WalletStats,
    WalletStatsSnapshot,
    WalletVetting,
)
from app.logging_config import get_logger
from app.services.redis import get_followed_wallets, set_followed_wallets


async def _nudge_follow_lane(redis, address: str, followed: bool) -> None:
    """Best-effort immediate update of the priority-lane set on star/unstar.

    The analytics cycle republishes the authoritative set every few minutes;
    this just closes the gap so a fresh star is subscribed within a minute
    instead of waiting for the next cycle.
    """
    try:
        current = set(await get_followed_wallets(redis))
        if followed:
            current.add(address)
        else:
            current.discard(address)
        await set_followed_wallets(redis, list(current))
    except Exception:  # noqa: BLE001 - lane sync must never fail the request
        log.warning("follow_lane_nudge_failed", address=address)

log = get_logger(__name__)

router = APIRouter(prefix="/api/wallets", tags=["wallets"])

# Solana pubkeys are 32-44 chars of base58 (no 0, O, I, l).
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class TrackRequest(BaseModel):
    address: str


@router.post("/track", response_model=WalletOut)
async def track_wallet(
    body: TrackRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    _user: str = Depends(require_auth),
) -> Wallet:
    """Star a wallet: mark it manually tracked so the copy engine follows it.

    Tracking bypasses the confidence bar in the evaluator, so this is a
    trade-controlling mutation — it FAILS CLOSED (strict auth, like the
    copytrading resume/approval endpoints), never open-when-unconfigured.

    Unknown addresses are created on the spot — starring a wallet found on
    Twitter/DexScreener must not wait for the chain listener to happen upon
    it first. Idempotent.
    """
    address = body.address.strip()
    if not _BASE58_RE.fullmatch(address):
        raise HTTPException(status_code=422, detail="not a valid Solana address")
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        now = datetime.now(tz=UTC)
        wallet = Wallet(address=address, first_seen_at=now, last_seen_at=now,
                        is_tracked=True)
        db.add(wallet)
        try:
            await db.commit()
        except IntegrityError:
            # Lost the race against a concurrent star or the ingestion
            # writer inserting this actively-trading wallet: fall through
            # to flagging the row that won.
            await db.rollback()
            wallet = (
                await db.execute(select(Wallet).where(Wallet.address == address))
            ).scalar_one()
            wallet.is_tracked = True
            await db.commit()
    else:
        wallet.is_tracked = True
        await db.commit()
    await db.refresh(wallet)
    await _nudge_follow_lane(redis, address, followed=True)
    log.info("wallet_tracked", address=address)
    return wallet


@router.delete("/{address}/track", response_model=WalletOut)
async def untrack_wallet(
    address: str,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    _user: str = Depends(require_auth),
) -> Wallet:
    """Unstar a wallet. It may still be auto-followed if its confidence
    clears the copy_auto_follow bar — this only removes the manual pin."""
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    wallet.is_tracked = False
    await db.commit()
    await db.refresh(wallet)
    await _nudge_follow_lane(redis, address, followed=False)
    log.info("wallet_untracked", address=address)
    return wallet


@router.get("", response_model=list[WalletOut])
async def list_wallets(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tracked_only: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[Wallet]:
    """List wallets, most recently seen first.

    ``vetting_verdict`` is deliberately left None here — stitching the latest
    verdict onto every row of a paged list isn't worth the extra query for a
    field the UI only surfaces on rankings and the detail page.
    """
    stmt = select(Wallet).order_by(Wallet.last_seen_at.desc()).limit(limit).offset(offset)
    if tracked_only:
        stmt = stmt.where(Wallet.is_tracked.is_(True))
    return list((await db.execute(stmt)).scalars())


@router.get("/{address}", response_model=WalletOut)
async def get_wallet(address: str, db: AsyncSession = Depends(get_db)) -> WalletOut:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.address == address))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    # Latest vetting verdict (append-only table); None = never vetted.
    verdict = (
        await db.execute(
            select(WalletVetting.verdict)
            .where(WalletVetting.wallet_id == wallet.id)
            .order_by(WalletVetting.ts.desc(), WalletVetting.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    out = WalletOut.model_validate(wallet)
    out.vetting_verdict = verdict
    return out


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
