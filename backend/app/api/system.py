"""System health / monitoring API.

Exposes a component-level health check (DB, Redis, ingestion freshness, worker
liveness, table counts) and an alert feed backed by the pure rule engine in
``app.services.alerts``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.config import Settings, get_settings
from app.db.models import (
    FailedTransaction,
    MarketRegime,
    ModelPerformance,
    Token,
    TokenSnapshot,
    Trade,
    Wallet,
)
from app.db.util import aware, sql_cutoff
from app.decision.safety import DAILY_PNL_KEY_PREFIX, EMERGENCY_STOP_KEY, _today
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

_LAMPORTS = Decimal(10) ** 9

# Age thresholds (minutes) beyond which a data source is considered "stale".
TRADE_STALE_MINUTES = 15.0
SNAPSHOT_STALE_MINUTES = 15.0
REGIME_STALE_MINUTES = 120.0
MODEL_STALE_MINUTES = 1440.0


class ComponentHealth(BaseModel):
    status: str
    detail: str | None = None
    age_seconds: float | None = None


class HealthOut(BaseModel):
    status: str  # ok | degraded | down
    generated_at: datetime
    components: dict[str, ComponentHealth]
    counts: dict[str, int]


class AlertOut(BaseModel):
    name: str
    severity: str
    message: str
    value: float | None = None
    threshold: float | None = None


class AlertsOut(BaseModel):
    generated_at: datetime
    context: dict[str, Any]
    alerts: list[AlertOut]


async def _latest(session: AsyncSession, column) -> datetime | None:
    value = (await session.execute(select(func.max(column)))).scalar_one_or_none()
    return aware(value) if value is not None else None


def _freshness(ts: datetime | None, now: datetime, stale_minutes: float) -> ComponentHealth:
    if ts is None:
        return ComponentHealth(status="absent", detail="no rows")
    age = (now - ts).total_seconds()
    status = "ok" if age <= stale_minutes * 60 else "stale"
    return ComponentHealth(status=status, age_seconds=age)


async def gather_context(
    session: AsyncSession, redis: aioredis.Redis, settings: Settings
) -> dict[str, Any]:
    """Build the plain context dict consumed by ``evaluate_alerts``."""
    now = datetime.now(tz=UTC)
    hour_ago = sql_cutoff(session, now - timedelta(hours=1))

    latest_trade = await _latest(session, Trade.block_time)
    trades_1h = (
        await session.execute(
            select(func.count()).select_from(Trade).where(Trade.block_time >= hour_ago)
        )
    ).scalar_one()
    failed_1h = (
        await session.execute(
            select(func.count())
            .select_from(FailedTransaction)
            .where(FailedTransaction.block_time >= hour_ago)
        )
    ).scalar_one()
    latest_auc = (
        await session.execute(
            select(ModelPerformance.auc).order_by(ModelPerformance.ts.desc()).limit(1)
        )
    ).scalar_one_or_none()
    latest_regime = await _latest(session, MarketRegime.ts)

    try:
        emergency_stop = bool(await redis.get(EMERGENCY_STOP_KEY))
        raw_pnl = await redis.get(DAILY_PNL_KEY_PREFIX + _today(now))
        from app.services.rpc import RPC_FREEZE_KEY

        rpc_frozen = bool(await redis.get(RPC_FREEZE_KEY))
    except Exception as exc:  # redis unreachable — treat as unknown, non-firing
        log.warning("alert_context_redis_unreadable", error=str(exc))
        emergency_stop = False
        raw_pnl = None
        rpc_frozen = False
    daily_pnl_sol = float(Decimal(int(raw_pnl or 0)) / _LAMPORTS)

    # None (never any data) is distinct from a large age (stalled): a fresh
    # deploy with no trades/regimes yet must NOT fire a critical stall alert.
    minutes_since_trade = (
        (now - latest_trade).total_seconds() / 60.0 if latest_trade is not None else None
    )
    minutes_since_regime = (
        (now - latest_regime).total_seconds() / 60.0 if latest_regime is not None else None
    )

    return {
        "minutes_since_last_trade": minutes_since_trade,
        "trades_1h": int(trades_1h),
        "failed_tx_1h": int(failed_1h),
        "emergency_stop": emergency_stop,
        "rpc_frozen": rpc_frozen,
        "daily_pnl_sol": daily_pnl_sol,
        "daily_loss_limit_sol": float(settings.copy_daily_loss_limit_sol),
        "latest_model_auc": float(latest_auc) if latest_auc is not None else None,
        "minutes_since_last_regime": minutes_since_regime,
    }


@router.get("/health", response_model=HealthOut)
async def health(
    response: Response,
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> HealthOut:
    now = datetime.now(tz=UTC)
    components: dict[str, ComponentHealth] = {}
    counts: dict[str, int] = {}

    # --- DB reachability + counts -----------------------------------------
    db_up = True
    try:
        await session.execute(select(1))
        for name, model in (
            ("trades", Trade),
            ("wallets", Wallet),
            ("tokens", Token),
        ):
            counts[name] = int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
        components["database"] = ComponentHealth(status="ok")
    except Exception as exc:
        db_up = False
        components["database"] = ComponentHealth(status="down", detail=str(exc))

    # --- Redis reachability -----------------------------------------------
    redis_up = True
    try:
        await redis.ping()
        components["redis"] = ComponentHealth(status="ok")
    except Exception as exc:
        redis_up = False
        components["redis"] = ComponentHealth(status="down", detail=str(exc))

    # --- freshness / worker liveness (only if DB is up) -------------------
    if db_up:
        components["ingestion"] = _freshness(
            await _latest(session, Trade.block_time), now, TRADE_STALE_MINUTES
        )
        components["enrichment_worker"] = _freshness(
            await _latest(session, TokenSnapshot.ts), now, SNAPSHOT_STALE_MINUTES
        )
        components["regime_worker"] = _freshness(
            await _latest(session, MarketRegime.ts), now, REGIME_STALE_MINUTES
        )
        components["model_worker"] = _freshness(
            await _latest(session, ModelPerformance.ts), now, MODEL_STALE_MINUTES
        )

    # --- overall roll-up ---------------------------------------------------
    # "absent" (no rows yet) is a fresh-install state, not an outage: only a
    # "stale" component (had data, now overdue) degrades overall health, so a
    # brand-new deploy reports ok instead of a permanent false "degraded".
    if not db_up or not redis_up:
        status = "down"
        response.status_code = 503
    elif any(c.status == "stale" for c in components.values()):
        status = "degraded"
    else:
        status = "ok"

    return HealthOut(status=status, generated_at=now, components=components, counts=counts)


@router.get("/alerts", response_model=AlertsOut)
async def alerts(
    session: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> AlertsOut:
    from app.services.alerts import evaluate_alerts

    context = await gather_context(session, redis, settings)
    fired = evaluate_alerts(context)
    return AlertsOut(
        generated_at=datetime.now(tz=UTC),
        context=context,
        alerts=[AlertOut(**a.model_dump()) for a in fired],
    )
