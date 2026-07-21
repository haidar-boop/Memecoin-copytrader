"""Sizing, safety-rail, and adaptive-ranking unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CopyPosition
from app.decision import ranking, sizing
from app.decision.safety import (
    DAILY_PNL_KEY_PREFIX,
    EMERGENCY_STOP_KEY,
    SafetyGuard,
    _today,
)
from tests.conftest import StubRedis

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def make_settings(**overrides) -> Settings:
    return Settings(**overrides)


# --- sizing ----------------------------------------------------------------


def test_fixed_sizing_with_clamps() -> None:
    settings = make_settings(copy_fixed_sol=0.3, copy_max_position_sol=0.2,
                             copy_max_exposure_sol=2.0)
    size, notes = sizing.copy_size_sol(
        settings, leader_size_sol=Decimal(5), current_exposure_sol=Decimal(0)
    )
    assert size == Decimal("0.2")
    assert any("clamped to max position" in n for n in notes)


def test_percent_sizing_and_exposure_headroom() -> None:
    settings = make_settings(copy_size_mode="percent", copy_percent_of_leader=10.0,
                             copy_max_position_sol=5.0, copy_max_exposure_sol=1.0)
    size, _ = sizing.copy_size_sol(
        settings, leader_size_sol=Decimal(4), current_exposure_sol=Decimal("0.9")
    )
    assert size == Decimal("0.1")  # 0.4 desired, clamped to headroom
    size2, notes = sizing.copy_size_sol(
        settings, leader_size_sol=Decimal(4), current_exposure_sol=Decimal("1.0")
    )
    assert size2 == 0
    assert any("no exposure headroom" in n for n in notes)


# --- safety ----------------------------------------------------------------


async def test_emergency_stop_and_failure_streak(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    guard = SafetyGuard(make_settings(copy_max_consecutive_failures=2), stub_redis)
    assert await guard.gate_reasons(db_session, "mintA") == []

    await guard.record_execution_result(False)
    await guard.record_execution_result(False)  # trips auto-stop
    blocked = await guard.gate_reasons(db_session, "mintA")
    assert any("emergency stop" in b for b in blocked)
    assert any("consecutive" in b for b in blocked)

    await guard.clear_emergency_stop()
    assert await guard.gate_reasons(db_session, "mintA") == []
    # a success resets the streak
    await guard.record_execution_result(False)
    await guard.record_execution_result(True)
    await guard.record_execution_result(False)
    assert await guard.gate_reasons(db_session, "mintA") == []


async def test_daily_loss_limit_trips_stop(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    guard = SafetyGuard(make_settings(copy_daily_loss_limit_sol=1.0), stub_redis)
    await guard.record_realized_pnl(Decimal("-0.4"))
    assert await guard.gate_reasons(db_session, "m") == []
    await guard.record_realized_pnl(Decimal("-0.7"))
    blocked = await guard.gate_reasons(db_session, "m")
    assert any("daily loss" in b for b in blocked)
    assert stub_redis.data.get(EMERGENCY_STOP_KEY)
    assert int(stub_redis.data[DAILY_PNL_KEY_PREFIX + _today()]) < -1_000_000_000


async def test_cooldown_and_exposure_limits(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    guard = SafetyGuard(
        make_settings(copy_max_open_positions=1, copy_max_exposure_sol=0.5), stub_redis
    )
    await guard.start_cooldown("mintZ")
    assert any("cooldown" in b for b in await guard.gate_reasons(db_session, "mintZ"))
    assert await guard.gate_reasons(db_session, "other") == []

    db_session.add(
        CopyPosition(
            token_id=1, leader_wallet_id=1, mode="paper", status="open",
            opened_at=NOW, spent_sol=Decimal("0.6"), tokens_bought=Decimal(100),
        )
    )
    await db_session.commit()
    blocked = await guard.gate_reasons(db_session, "other")
    assert any("max open positions" in b for b in blocked)
    assert any("max exposure" in b for b in blocked)


# --- ranking ---------------------------------------------------------------


async def test_ranking_small_sample_guard_and_demotion(stub_redis: StubRedis) -> None:
    factor, note = await ranking.adjustment_factor(stub_redis, 7)
    assert factor == 1.0 and "no copy outcomes" in note

    for _ in range(4):
        await ranking.record_outcome(stub_redis, 7, -0.5)
    factor, note = await ranking.adjustment_factor(stub_redis, 7)
    assert factor == 1.0 and "no adjustment" in note  # < MIN_OUTCOMES

    await ranking.record_outcome(stub_redis, 7, -0.5)
    factor, _ = await ranking.adjustment_factor(stub_redis, 7)
    assert factor < 1.0  # demoted after enough losing evidence

    for _ in range(20):
        await ranking.record_outcome(stub_redis, 7, 0.6)
    recovered, _ = await ranking.adjustment_factor(stub_redis, 7)
    assert 1.0 < recovered <= ranking.ADJUST_MAX  # gradual recovery, capped


async def test_ranking_floor(stub_redis: StubRedis) -> None:
    for _ in range(10):
        await ranking.record_outcome(stub_redis, 9, -1.0)
    factor, _ = await ranking.adjustment_factor(stub_redis, 9)
    assert factor == ranking.ADJUST_MIN
