"""Analytics job loop: periodic wallet stats, strategy, patterns, retraining.

Imports of the heavy job modules happen inside each cycle so the worker
process boots (and other jobs keep running) even if one module is broken or
mid-deployment — the failing cycle is counted and logged by the shared job
machinery instead of taking the loop down.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.services.jobs import JobSpec, run_jobs

log = get_logger(__name__)

__all__ = ["run_analytics_loop"]


def _build_job_specs(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> list[JobSpec]:
    async def wallet_stats_cycle() -> None:
        from app.analytics import wallet_metrics

        async with session_factory() as session:
            await wallet_metrics.run_once(
                session,
                min_closed_positions=settings.analytics_min_closed_positions,
                wallet_batch=settings.analytics_wallet_batch,
                now=datetime.now(tz=UTC),
            )

    async def strategy_cycle() -> None:
        from app.analytics import strategy

        async with session_factory() as session:
            await strategy.run_once(
                session,
                k=settings.strategy_clusters_k,
                min_wallets=settings.strategy_min_wallets,
                window_days=settings.strategy_window_days,
                now=datetime.now(tz=UTC),
            )

    async def patterns_cycle() -> None:
        from app.analytics import patterns

        async with session_factory() as session:
            await patterns.run_once(
                session,
                window_days=settings.patterns_window_days,
                min_evidence=settings.patterns_min_evidence,
                now=datetime.now(tz=UTC),
            )

    async def retrain_cycle() -> None:
        from app.analytics.ml import train

        async with session_factory() as session:
            result = await train.retrain_once(session, settings, now=datetime.now(tz=UTC))
            log.info("ml_retrain_result", **{str(k): str(v) for k, v in (result or {}).items()})

    return [
        ("wallet_stats", float(settings.wallet_stats_interval_seconds), wallet_stats_cycle),
        ("strategy", float(settings.strategy_interval_seconds), strategy_cycle),
        ("patterns", float(settings.patterns_interval_seconds), patterns_cycle),
        ("ml_retrain", float(settings.ml_retrain_interval_seconds), retrain_cycle),
    ]


async def run_analytics_loop(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Start every periodic analytics job; returns cleanly on cancellation."""
    await run_jobs("analytics", _build_job_specs(settings, session_factory))
