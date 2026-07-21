"""Phase 4 evaluation loop: resolve predictions, score models, detect regimes,
compute regime-strategy stats, and generate periodic AI reports.

Job modules are imported lazily inside each cycle so the worker boots (and
other jobs keep running) even if one module is broken or mid-deploy — the
failing cycle is counted and logged by the shared job machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logging_config import get_logger
from app.services.jobs import JobSpec, run_jobs

log = get_logger(__name__)

__all__ = ["run_evaluation_loop"]


def _build_job_specs(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> list[JobSpec]:
    async def evaluation_cycle() -> None:
        from app.evaluation import prediction_eval

        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            await prediction_eval.resolve_outcomes(
                session,
                label_horizon_hours=settings.ml_label_horizon_hours,
                batch=settings.eval_resolve_batch,
                now=now,
            )
        async with session_factory() as session:
            await prediction_eval.compute_model_performance(
                session,
                window_days=settings.eval_window_days,
                min_resolved=settings.eval_min_resolved,
                bins=settings.eval_calibration_bins,
                now=now,
            )

    async def regime_cycle() -> None:
        from app.evaluation import regime, regime_strategy

        now = datetime.now(tz=UTC)
        async with session_factory() as session:
            await regime.detect_regime(
                session,
                window_minutes=settings.regime_window_minutes,
                lookback_windows=settings.regime_lookback_windows,
                now=now,
            )
        async with session_factory() as session:
            await regime_strategy.compute_regime_strategy_stats(
                session,
                window_days=settings.regime_strategy_window_days,
                now=now,
            )

    async def report_cycle() -> None:
        from app.evaluation import reports

        now = datetime.now(tz=UTC)
        # One daily report per UTC day — same restart-proof guard as weekly.
        async with session_factory() as session:
            if not await reports.daily_report_exists(session, now):
                await reports.generate_report(
                    session,
                    kind="daily",
                    window_days=1,
                    top_n=settings.report_wallet_top_n,
                    now=now,
                )
        # One weekly report per ISO week, generated on the first cycle of the
        # week — guarded so a sub-day interval or a worker restart can't emit
        # duplicate weekly rows.
        async with session_factory() as session:
            if not await reports.weekly_report_exists(session, now):
                await reports.generate_report(
                    session,
                    kind="weekly",
                    window_days=7,
                    top_n=settings.report_wallet_top_n,
                    now=now,
                )

    return [
        ("prediction_evaluation", float(settings.evaluation_interval_seconds), evaluation_cycle),
        ("market_regime", float(settings.regime_interval_seconds), regime_cycle),
        ("reports", float(settings.report_interval_seconds), report_cycle),
    ]


async def run_evaluation_loop(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Start every periodic evaluation job; returns cleanly on cancellation."""
    await run_jobs("evaluation", _build_job_specs(settings, session_factory))
