"""Copy-trading API: decisions, positions, risk status, controls.

State-changing endpoints (resume, approvals) require the shared admin token
when one is configured; without a configured token they are refused outright
so an unauthenticated deployment cannot resume trading or approve orders.
The emergency-stop endpoint deliberately works WITHOUT a token — stopping
must never be harder than starting.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.config import get_settings
from app.db.models import CopyPosition, CopyTrade, Token, TradeDecision
from app.decision.safety import SafetyGuard
from app.execution.executor import CopyExecutor
from app.services.rpc import RpcBudget, SolanaRpc

router = APIRouter(prefix="/api/copytrading", tags=["copytrading"])


def _require_admin(x_admin_token: str | None) -> None:
    settings = get_settings()
    if not settings.admin_token:
        raise HTTPException(
            status_code=403,
            detail="admin_token is not configured; state-changing endpoints disabled",
        )
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="invalid admin token")


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    source_signature: str | None
    leader_wallet_id: int | None
    token_id: int | None
    side: str
    mode: str
    confidence_score: Decimal | None
    risk_score: Decimal | None
    expected_reward: Decimal | None
    expected_drawdown: Decimal | None
    p_profit: Decimal | None
    decision: str
    size_sol: Decimal | None
    reasons: list | None
    factors: list | None


class CopyTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    decision_id: int
    created_at: datetime
    mode: str
    side: str
    token_id: int
    size_sol: Decimal
    status: str
    attempts: int
    tx_signature: str | None
    filled_token_amount: Decimal | None
    filled_price_sol: Decimal | None
    error: str | None


class CopyPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    token_id: int
    leader_wallet_id: int | None
    mode: str
    status: str
    opened_at: datetime
    closed_at: datetime | None
    spent_sol: Decimal
    tokens_bought: Decimal
    sold_sol: Decimal
    realized_pnl_sol: Decimal | None


@router.get("/decisions", response_model=list[DecisionOut])
async def decisions(
    decision: str | None = Query(None, pattern="^(copy|skip)$"),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[TradeDecision]:
    stmt = select(TradeDecision).order_by(TradeDecision.created_at.desc()).limit(limit)
    if decision:
        stmt = stmt.where(TradeDecision.decision == decision)
    return list((await db.execute(stmt)).scalars())


@router.get("/trades", response_model=list[CopyTradeOut])
async def trades(
    status_filter: str | None = Query(None, max_length=20),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[CopyTrade]:
    stmt = select(CopyTrade).order_by(CopyTrade.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(CopyTrade.status == status_filter)
    return list((await db.execute(stmt)).scalars())


@router.get("/positions", response_model=list[CopyPositionOut])
async def positions(
    status_filter: str | None = Query(None, pattern="^(open|closed)$"),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[CopyPosition]:
    stmt = select(CopyPosition).order_by(CopyPosition.opened_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(CopyPosition.status == status_filter)
    return list((await db.execute(stmt)).scalars())


@router.get("/status")
async def status(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    guard = SafetyGuard(get_settings(), redis)
    return await guard.status(db)


@router.post("/emergency-stop")
async def emergency_stop(redis: aioredis.Redis = Depends(get_redis)) -> dict:
    guard = SafetyGuard(get_settings(), redis)
    await guard.trip_emergency_stop("manual stop via API")
    return {"emergency_stop": "manual stop via API"}


@router.post("/resume")
async def resume(
    x_admin_token: str | None = Header(None),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    _require_admin(x_admin_token)
    guard = SafetyGuard(get_settings(), redis)
    await guard.clear_emergency_stop()
    return {"emergency_stop": None}


@router.post("/approvals/{trade_id}", response_model=CopyTradeOut)
async def approve_trade(
    trade_id: int,
    x_admin_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> CopyTrade:
    _require_admin(x_admin_token)
    settings = get_settings()
    # Lock the row so two concurrent approvals can't both execute it (the
    # status check below is only atomic under the lock).
    stmt = select(CopyTrade).where(CopyTrade.id == trade_id)
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    trade = (await db.execute(stmt)).scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="copy trade not found")
    if trade.status != "pending_approval":
        raise HTTPException(status_code=409, detail=f"trade is {trade.status}")
    token = await db.get(Token, trade.token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="token not found")

    guard = SafetyGuard(settings, redis)
    # Re-check the safety rails AT APPROVAL TIME: the emergency stop or loss
    # limits may have tripped since the decision was made. Approval must never
    # bypass the kill switch.
    blocked = await guard.gate_reasons(db, token.mint)
    if blocked:
        trade.status = "rejected"
        trade.error = "safety rails blocked at approval: " + "; ".join(blocked)
        await db.commit()
        raise HTTPException(status_code=409, detail=trade.error)

    rpc = None
    try:
        if settings.copy_mode == "live":
            rpc = SolanaRpc(
                settings.solana_rpc_url,
                timeout_seconds=settings.rpc_timeout_seconds,
                max_retries=settings.rpc_max_retries,
                requests_per_second=settings.rpc_requests_per_second,
                budget=RpcBudget(redis, settings.rpc_daily_credit_budget),
                priority_budget=RpcBudget(
                    redis,
                    settings.rpc_priority_daily_credit_budget,
                    key_prefix=RpcBudget.PRIORITY_KEY_PREFIX,
                ),
            )
        executor = CopyExecutor(settings, redis, rpc, guard)
        trade.status = "approved"
        result = await executor.run_approved(db, trade, token.mint)
        await db.commit()
        return result
    finally:
        if rpc is not None:
            await rpc.aclose()
