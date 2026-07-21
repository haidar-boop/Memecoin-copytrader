"""Safety rails for copy trading. Redis-backed operational state.

Every rail fails CLOSED: if state cannot be read, trading is blocked. The
emergency stop is a plain Redis key so an operator (or the auto-stop logic)
can halt everything instantly, worker restarts included.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CopyPosition
from app.db.util import quantize_sol
from app.logging_config import get_logger

log = get_logger(__name__)

EMERGENCY_STOP_KEY = "copy:emergency_stop"
FAILURE_STREAK_KEY = "copy:consecutive_failures"
# Realized PnL is accumulated in integer LAMPORTS (exact) per UTC day. The
# per-calendar-day reset is intentional daily-limit semantics; max-exposure,
# consecutive-failure, and emergency-stop rails cover the cross-midnight case.
DAILY_PNL_KEY_PREFIX = "copy:daily_pnl_lamports:"  # + YYYY-MM-DD
COOLDOWN_KEY_PREFIX = "copy:cooldown:"  # + token mint
_LAMPORTS = Decimal(10) ** 9


class _Redis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None) -> object: ...

    async def delete(self, *keys: str) -> object: ...

    async def incr(self, key: str) -> int: ...

    async def incrby(self, key: str, amount: int) -> int: ...


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(tz=UTC)).date().isoformat()


class SafetyGuard:
    def __init__(self, settings: Settings, redis: _Redis):
        self._settings = settings
        self._redis = redis

    # --- checks ------------------------------------------------------------

    async def gate_reasons(self, session: AsyncSession, token_mint: str) -> list[str]:
        """Every safety rail that currently blocks a new copy buy."""
        blocked: list[str] = []
        try:
            if await self._redis.get(EMERGENCY_STOP_KEY):
                blocked.append("emergency stop is active")
            if await self._redis.get(COOLDOWN_KEY_PREFIX + token_mint):
                blocked.append(
                    f"token cooldown active ({self._settings.copy_token_cooldown_seconds}s)"
                )
            streak = int(await self._redis.get(FAILURE_STREAK_KEY) or 0)
            if streak >= self._settings.copy_max_consecutive_failures:
                blocked.append(f"{streak} consecutive execution failures")
            daily = await self._daily_pnl_sol()
            if daily <= -Decimal(str(self._settings.copy_daily_loss_limit_sol)):
                blocked.append(f"daily loss limit reached ({daily} SOL realized today)")
        except Exception as exc:
            log.error("safety_state_unreadable", error=str(exc))
            blocked.append("safety state unreadable (failing closed)")

        open_count, exposure = await self.open_exposure(session)
        if open_count >= self._settings.copy_max_open_positions:
            blocked.append(f"max open positions ({open_count})")
        if exposure >= Decimal(str(self._settings.copy_max_exposure_sol)):
            blocked.append(f"max exposure reached ({exposure} SOL)")
        return blocked

    async def open_exposure(self, session: AsyncSession) -> tuple[int, Decimal]:
        """(open copy positions, SOL still at risk in them)."""
        count, spent, sold = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(CopyPosition.spent_sol), 0),
                    func.coalesce(func.sum(CopyPosition.sold_sol), 0),
                ).where(CopyPosition.status == "open")
            )
        ).one()
        exposure = quantize_sol(Decimal(str(spent)) - Decimal(str(sold)))
        return int(count), max(exposure, Decimal(0))

    # --- state transitions -------------------------------------------------

    async def record_execution_result(self, success: bool) -> None:
        if success:
            await self._redis.delete(FAILURE_STREAK_KEY)
            return
        streak = await self._redis.incr(FAILURE_STREAK_KEY)
        if streak >= self._settings.copy_max_consecutive_failures:
            await self.trip_emergency_stop(f"{streak} consecutive execution failures")

    async def _daily_pnl_sol(self, now: datetime | None = None) -> Decimal:
        lamports = int(await self._redis.get(DAILY_PNL_KEY_PREFIX + _today(now)) or 0)
        return Decimal(lamports) / _LAMPORTS

    async def record_realized_pnl(self, pnl_sol: Decimal, now: datetime | None = None) -> None:
        # Accumulate exact integer lamports so the loss-limit comparison never
        # drifts (binary-float INCRBYFLOAT accumulates rounding error).
        key = DAILY_PNL_KEY_PREFIX + _today(now)
        total_lamports = await self._redis.incrby(key, int(pnl_sol * _LAMPORTS))
        total = Decimal(total_lamports) / _LAMPORTS
        if total <= -Decimal(str(self._settings.copy_daily_loss_limit_sol)):
            await self.trip_emergency_stop(f"daily loss limit hit ({total} SOL)")

    async def start_cooldown(self, token_mint: str) -> None:
        await self._redis.set(
            COOLDOWN_KEY_PREFIX + token_mint,
            "1",
            ex=self._settings.copy_token_cooldown_seconds,
        )

    async def trip_emergency_stop(self, reason: str) -> None:
        await self._redis.set(EMERGENCY_STOP_KEY, reason)
        log.error("emergency_stop_tripped", reason=reason)
        # Best-effort operator alert; never let a notification failure prevent
        # the kill switch from engaging.
        try:
            from app.services.notifications import Notification, NotificationService

            await NotificationService(self._redis).emit(
                Notification.emergency_stop(reason)
            )
        except Exception as exc:  # pragma: no cover - best-effort side channel
            log.warning("emergency_stop_notify_failed", error=str(exc))

    async def clear_emergency_stop(self) -> None:
        await self._redis.delete(EMERGENCY_STOP_KEY, FAILURE_STREAK_KEY)
        log.info("emergency_stop_cleared")

    async def status(self, session: AsyncSession) -> dict:
        open_count, exposure = await self.open_exposure(session)
        stop_reason = None
        daily = Decimal(0)
        try:
            stop_reason = await self._redis.get(EMERGENCY_STOP_KEY)
            daily = await self._daily_pnl_sol()
        except Exception as exc:
            log.warning("safety_status_partial", error=str(exc))
        return {
            "emergency_stop": stop_reason,
            "daily_realized_pnl_sol": str(daily),
            "open_positions": open_count,
            "exposure_sol": str(exposure),
            "mode": self._settings.copy_mode,
            "enabled": self._settings.copy_enabled,
        }
