from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.schemas import TokenOut, TokenSnapshotOut
from app.db.models import Token, TokenSnapshot

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


@router.get("", response_model=list[TokenOut])
async def list_tokens(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[Token]:
    stmt = select(Token).order_by(Token.first_seen_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars())


@router.get("/{mint}", response_model=TokenOut)
async def get_token(mint: str, db: AsyncSession = Depends(get_db)) -> Token:
    token = (await db.execute(select(Token).where(Token.mint == mint))).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="token not found")
    return token


@router.get("/{mint}/snapshots", response_model=list[TokenSnapshotOut])
async def token_snapshots(
    mint: str,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(1000, ge=1, le=10000),
    db: AsyncSession = Depends(get_db),
) -> list[TokenSnapshot]:
    token = (await db.execute(select(Token).where(Token.mint == mint))).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="token not found")
    since = datetime.now(tz=UTC) - timedelta(hours=hours)
    rows = await db.execute(
        select(TokenSnapshot)
        .where(TokenSnapshot.token_id == token.id, TokenSnapshot.ts >= since)
        .order_by(TokenSnapshot.ts.desc())
        .limit(limit)
    )
    return list(rows.scalars())
