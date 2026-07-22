"""Analytics API: wallet rankings, strategy clusters, patterns, model registry."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import (
    DiscoveredPattern,
    MlModel,
    StrategyCluster,
    StrategyStat,
    Wallet,
    WalletStats,
    WalletVetting,
)
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

RankBy = Literal["confidence_score", "total_pnl_sol", "win_rate", "pnl_30d_sol"]


class WalletStatsOut(BaseModel):
    """Full wallet_stats row (shared with /api/wallets/{address}/stats)."""

    model_config = ConfigDict(from_attributes=True)

    wallet_id: int
    computed_at: datetime
    trade_count: int
    buy_count: int
    sell_count: int
    position_count: int
    closed_position_count: int
    win_count: int
    win_rate: Decimal | None
    total_pnl_sol: Decimal | None
    total_volume_sol: Decimal | None
    avg_roi: Decimal | None
    median_roi: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_sol: Decimal | None
    max_drawdown_pct: Decimal | None
    avg_hold_seconds: int | None
    median_hold_seconds: int | None
    avg_position_sol: Decimal | None
    max_position_sol: Decimal | None
    avg_entry_delay_seconds: int | None
    trades_per_day: Decimal | None
    partial_exit_ratio: Decimal | None
    roi_std: Decimal | None
    pnl_7d_sol: Decimal | None
    pnl_30d_sol: Decimal | None
    first_trade_at: datetime | None
    last_trade_at: datetime | None
    confidence_score: Decimal | None
    confidence_components: list | None
    style: str | None
    style_confidence: Decimal | None


class TopWalletOut(BaseModel):
    address: str
    wallet_id: int
    is_tracked: bool = False
    confidence_score: Decimal | None
    total_pnl_sol: Decimal | None
    win_rate: Decimal | None
    pnl_30d_sol: Decimal | None
    closed_position_count: int
    style: str | None
    computed_at: datetime
    # Latest fake-wallet vetting verdict ("clear" | "suspicious" |
    # "inconclusive"); None when the wallet was never vetted.
    vetting_verdict: str | None = None


class WalletStatsSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    wallet_id: int
    ts: datetime
    trade_count: int
    closed_position_count: int
    win_rate: Decimal | None
    total_pnl_sol: Decimal | None
    pnl_7d_sol: Decimal | None
    pnl_30d_sol: Decimal | None
    confidence_score: Decimal | None
    style: str | None


class StrategyStatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    style: str
    window_days: int
    wallet_count: int
    closed_positions: int
    win_rate: Decimal | None
    avg_roi: Decimal | None
    total_pnl_sol: Decimal | None
    profit_factor: Decimal | None
    avg_hold_seconds: int | None


class StrategyClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    computed_at: datetime
    name: str
    member_count: int
    feature_names: list | None
    centroid: list | None
    description: str | None
    latest_stat: StrategyStatOut | None = None


class PatternOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    key: dict | None
    stats: dict | None
    evidence_count: int
    window_start: datetime | None
    window_end: datetime | None
    computed_at: datetime
    description: str | None


class MlModelOut(BaseModel):
    """Registry entry — artifact_path is deliberately excluded."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: int
    algo: str
    trained_at: datetime
    training_rows: int
    params: dict | None
    metrics: dict | None
    feature_names: list | None
    is_active: bool


_RANK_COLUMNS = {
    "confidence_score": WalletStats.confidence_score,
    "total_pnl_sol": WalletStats.total_pnl_sol,
    "win_rate": WalletStats.win_rate,
    "pnl_30d_sol": WalletStats.pnl_30d_sol,
}


@router.get("/wallets/top", response_model=list[TopWalletOut])
async def top_wallets(
    by: RankBy = Query("confidence_score"),
    limit: int = Query(50, ge=1, le=500),
    min_closed: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[TopWalletOut]:
    col = _RANK_COLUMNS[by]
    stmt = (
        select(WalletStats, Wallet.address, Wallet.is_tracked)
        .join(Wallet, Wallet.id == WalletStats.wallet_id)
        .where(col.is_not(None))
        .order_by(col.desc())
        .limit(limit)
    )
    if min_closed > 0:
        stmt = stmt.where(WalletStats.closed_position_count >= min_closed)
    rows = (await db.execute(stmt)).all()
    verdicts = await _latest_verdicts(db, [ws.wallet_id for ws, _, _ in rows])
    return [
        TopWalletOut(
            address=address,
            wallet_id=ws.wallet_id,
            is_tracked=bool(is_tracked),
            confidence_score=ws.confidence_score,
            total_pnl_sol=ws.total_pnl_sol,
            win_rate=ws.win_rate,
            pnl_30d_sol=ws.pnl_30d_sol,
            closed_position_count=ws.closed_position_count,
            style=ws.style,
            computed_at=ws.computed_at,
            vetting_verdict=verdicts.get(ws.wallet_id),
        )
        for ws, address, is_tracked in rows
    ]


async def _latest_verdicts(
    db: AsyncSession, wallet_ids: list[int]
) -> dict[int, str]:
    """Latest vetting verdict per wallet in ONE bounded query.

    wallet_vettings is append-only, so "latest" is the max-``ts`` row per
    wallet (group-by join — SQLite-compatible, unlike DISTINCT ON). A ts tie
    is broken by insertion order: rows are read ``id`` ascending and the dict
    keeps the last write, so the newest row wins.
    """
    if not wallet_ids:
        return {}
    latest_ts = (
        select(
            WalletVetting.wallet_id.label("wallet_id"),
            func.max(WalletVetting.ts).label("max_ts"),
        )
        .where(WalletVetting.wallet_id.in_(wallet_ids))
        .group_by(WalletVetting.wallet_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(WalletVetting.wallet_id, WalletVetting.verdict)
            .join(
                latest_ts,
                (WalletVetting.wallet_id == latest_ts.c.wallet_id)
                & (WalletVetting.ts == latest_ts.c.max_ts),
            )
            .order_by(WalletVetting.id)
        )
    ).all()
    return {wallet_id: verdict for wallet_id, verdict in rows}


@router.get("/strategies", response_model=list[StrategyClusterOut])
async def strategies(db: AsyncSession = Depends(get_db)) -> list[StrategyClusterOut]:
    latest = (await db.execute(select(func.max(StrategyCluster.computed_at)))).scalar_one()
    if latest is None:
        return []
    clusters = list(
        (
            await db.execute(
                select(StrategyCluster)
                .where(StrategyCluster.computed_at == latest)
                .order_by(StrategyCluster.name)
            )
        ).scalars()
    )
    # Most recent StrategyStat per style via a bounded group-by join — the
    # stats table appends one row per style per run and must never be
    # full-scanned from a user-facing endpoint.
    latest_ts = (
        select(StrategyStat.style, func.max(StrategyStat.ts).label("max_ts"))
        .group_by(StrategyStat.style)
        .subquery()
    )
    stat_rows = list(
        (
            await db.execute(
                select(StrategyStat)
                .join(
                    latest_ts,
                    (StrategyStat.style == latest_ts.c.style)
                    & (StrategyStat.ts == latest_ts.c.max_ts),
                )
                .order_by(StrategyStat.id.desc())
            )
        ).scalars()
    )
    latest_by_style: dict[str, StrategyStat] = {}
    for stat in stat_rows:
        latest_by_style.setdefault(stat.style, stat)

    def base_style(name: str) -> str:
        # Cluster rows keep dedup suffixes ("sniper-2"); stats aggregate by
        # the base style name.
        head, _, tail = name.rpartition("-")
        return head if head and tail.isdigit() else name

    out: list[StrategyClusterOut] = []
    for cluster in clusters:
        item = StrategyClusterOut.model_validate(cluster)
        stat = latest_by_style.get(cluster.name) or latest_by_style.get(
            base_style(cluster.name)
        )
        if stat is not None:
            item.latest_stat = StrategyStatOut.model_validate(stat)
        out.append(item)
    return out


@router.get("/patterns", response_model=list[PatternOut])
async def patterns(
    kind: str | None = Query(None, max_length=48),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[DiscoveredPattern]:
    latest_per_kind = (
        select(
            DiscoveredPattern.kind.label("kind"),
            func.max(DiscoveredPattern.computed_at).label("max_computed_at"),
        )
        .group_by(DiscoveredPattern.kind)
        .subquery()
    )
    stmt = (
        select(DiscoveredPattern)
        .join(
            latest_per_kind,
            (DiscoveredPattern.kind == latest_per_kind.c.kind)
            & (DiscoveredPattern.computed_at == latest_per_kind.c.max_computed_at),
        )
        .order_by(DiscoveredPattern.computed_at.desc(), DiscoveredPattern.id)
        .limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(DiscoveredPattern.kind == kind)
    return list((await db.execute(stmt)).scalars())


@router.get("/models", response_model=list[MlModelOut])
async def models(db: AsyncSession = Depends(get_db)) -> list[MlModel]:
    stmt = select(MlModel).order_by(MlModel.trained_at.desc(), MlModel.id.desc())
    return list((await db.execute(stmt)).scalars())
