"""Phase 4 read API: generated reports and evaluation metrics.

All endpoints are read-only and unauthenticated. Rows are append-only
aggregates, so every listing surfaces the latest slice via bounded
group-by joins rather than full-table loads.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import (
    MarketRegime,
    ModelPerformance,
    RegimeStrategyStat,
    Report,
)
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["reports"])

ReportKind = Literal["daily", "weekly"]


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    summary: str | None
    sections: dict | None
    markdown: str | None


class ModelPerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    model_id: int
    model_name: str
    window_days: int
    resolved_count: int
    auc: Decimal | None
    brier: Decimal | None
    accuracy: Decimal | None
    base_rate: Decimal | None
    mean_roi_error: Decimal | None
    calibration: list | None


class MarketRegimeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    window_minutes: int
    regime: str
    high_volatility: bool
    low_liquidity: bool
    whale_accumulation: bool
    panic_selling: bool
    launch_wave: bool
    trend_exhaustion: bool
    features: dict | None
    description: str | None


class RegimeStrategyStatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    regime: str
    style: str
    window_days: int
    closed_positions: int
    win_rate: Decimal | None
    avg_roi: Decimal | None
    total_pnl_sol: Decimal | None


@router.get("/api/reports", response_model=list[ReportOut])
async def list_reports(
    kind: ReportKind | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[Report]:
    stmt = select(Report).order_by(Report.generated_at.desc(), Report.id.desc()).limit(limit)
    if kind is not None:
        stmt = stmt.where(Report.kind == kind)
    return list((await db.execute(stmt)).scalars())


@router.get("/api/reports/latest", response_model=ReportOut)
async def latest_report(
    kind: ReportKind = Query("weekly"),
    db: AsyncSession = Depends(get_db),
) -> Report:
    stmt = (
        select(Report)
        .where(Report.kind == kind)
        .order_by(Report.generated_at.desc(), Report.id.desc())
        .limit(1)
    )
    report = (await db.execute(stmt)).scalars().first()
    if report is None:
        raise HTTPException(status_code=404, detail=f"no {kind} report available")
    return report


@router.get("/api/evaluation/models", response_model=list[ModelPerformanceOut])
async def evaluation_models(db: AsyncSession = Depends(get_db)) -> list[ModelPerformance]:
    # Latest ModelPerformance per model_name via a bounded group-by join —
    # the table appends one row per model per evaluation run.
    latest_per_name = (
        select(
            ModelPerformance.model_name.label("model_name"),
            func.max(ModelPerformance.ts).label("max_ts"),
        )
        .group_by(ModelPerformance.model_name)
        .subquery()
    )
    stmt = (
        select(ModelPerformance)
        .join(
            latest_per_name,
            (ModelPerformance.model_name == latest_per_name.c.model_name)
            & (ModelPerformance.ts == latest_per_name.c.max_ts),
        )
        .order_by(ModelPerformance.model_name)
    )
    rows = list((await db.execute(stmt)).scalars())
    # Guard against duplicate ts collisions within a name.
    seen: set[str] = set()
    out: list[ModelPerformance] = []
    for row in rows:
        if row.model_name in seen:
            continue
        seen.add(row.model_name)
        out.append(row)
    return out


@router.get("/api/evaluation/regimes", response_model=list[MarketRegimeOut])
async def evaluation_regimes(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[MarketRegime]:
    stmt = (
        select(MarketRegime)
        .order_by(MarketRegime.ts.desc(), MarketRegime.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars())


@router.get("/api/evaluation/regime-strategies", response_model=list[RegimeStrategyStatOut])
async def evaluation_regime_strategies(
    regime: str | None = Query(None, max_length=24),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[RegimeStrategyStat]:
    # Latest ts per (regime, style) via bounded group-by join.
    latest_per_pair = (
        select(
            RegimeStrategyStat.regime.label("regime"),
            RegimeStrategyStat.style.label("style"),
            func.max(RegimeStrategyStat.ts).label("max_ts"),
        )
        .group_by(RegimeStrategyStat.regime, RegimeStrategyStat.style)
        .subquery()
    )
    stmt = (
        select(RegimeStrategyStat)
        .join(
            latest_per_pair,
            (RegimeStrategyStat.regime == latest_per_pair.c.regime)
            & (RegimeStrategyStat.style == latest_per_pair.c.style)
            & (RegimeStrategyStat.ts == latest_per_pair.c.max_ts),
        )
        .order_by(RegimeStrategyStat.regime, RegimeStrategyStat.style)
        .limit(limit)
    )
    if regime is not None:
        stmt = stmt.where(RegimeStrategyStat.regime == regime)
    rows = list((await db.execute(stmt)).scalars())
    # Guard the rare tie where two rows share the same max ts for one
    # (regime, style) pair (e.g. a replayed cycle): keep one per pair.
    seen: set[tuple[str, str]] = set()
    deduped: list[RegimeStrategyStat] = []
    for row in rows:
        key = (row.regime, row.style)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped
