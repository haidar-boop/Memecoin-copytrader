"""Tests for per-(regime, strategy) performance aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import MarketRegime, Position, RegimeStrategyStat, Wallet, WalletStats
from app.evaluation.regime_strategy import compute_regime_strategy_stats

NOW = datetime(2026, 7, 1, tzinfo=UTC)


async def _wallet(session, address: str, style: str | None) -> int:
    w = Wallet(address=address, first_seen_at=NOW, last_seen_at=NOW)
    session.add(w)
    await session.flush()
    if style is not None:
        session.add(
            WalletStats(wallet_id=w.id, computed_at=NOW, style=style)
        )
    await session.flush()
    return w.id


async def _position(
    session,
    *,
    wallet_id: int,
    opened_at: datetime,
    pnl: str,
    roi: str | None,
) -> None:
    session.add(
        Position(
            wallet_id=wallet_id,
            token_id=1,
            status="closed",
            opened_at=opened_at,
            closed_at=opened_at + timedelta(hours=1),
            realized_pnl_sol=Decimal(pnl),
            roi=None if roi is None else Decimal(roi),
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_groups_by_regime_and_style(db_session) -> None:
    # A bull period then a bear period.
    bull_ts = NOW - timedelta(days=5)
    bear_ts = NOW - timedelta(days=2)
    db_session.add_all(
        [
            MarketRegime(ts=bull_ts, window_minutes=60, regime="bull"),
            MarketRegime(ts=bear_ts, window_minutes=60, regime="bear"),
        ]
    )
    await db_session.flush()

    scalper = await _wallet(db_session, "scalperwallet", "scalper")
    holder = await _wallet(db_session, "holderwallet", "holder")

    # Bull period, scalper: 2 positions, 1 win.
    await _position(db_session, wallet_id=scalper, opened_at=bull_ts + timedelta(hours=1), pnl="1.0", roi="0.5")
    await _position(db_session, wallet_id=scalper, opened_at=bull_ts + timedelta(hours=2), pnl="-0.5", roi="-0.2")
    # Bull period, holder: 1 position, win.
    await _position(db_session, wallet_id=holder, opened_at=bull_ts + timedelta(hours=3), pnl="2.0", roi="1.0")
    # Bear period, scalper: 1 position, loss.
    await _position(db_session, wallet_id=scalper, opened_at=bear_ts + timedelta(hours=1), pnl="-1.0", roi="-0.5")
    await db_session.commit()

    stats = await compute_regime_strategy_stats(db_session, window_days=30, now=NOW)

    by_key = {(s.regime, s.style): s for s in stats}
    assert set(by_key) == {("bull", "scalper"), ("bull", "holder"), ("bear", "scalper")}

    bs = by_key[("bull", "scalper")]
    assert bs.closed_positions == 2
    assert bs.win_rate == Decimal("0.5")
    assert float(bs.avg_roi) == pytest.approx(0.15)  # mean(0.5, -0.2)
    assert bs.total_pnl_sol == Decimal("0.5")

    bh = by_key[("bull", "holder")]
    assert bh.closed_positions == 1
    assert bh.win_rate == Decimal("1")
    assert float(bh.avg_roi) == pytest.approx(1.0)

    br = by_key[("bear", "scalper")]
    assert br.win_rate == Decimal("0")
    assert br.total_pnl_sol == Decimal("-1")

    # Rows were persisted.
    persisted = (await db_session.execute(_select_all())).scalars().all()
    assert len(persisted) == 3


def _select_all():
    from sqlalchemy import select

    return select(RegimeStrategyStat)


@pytest.mark.asyncio
async def test_positions_before_any_regime_bucket_unknown(db_session) -> None:
    regime_ts = NOW - timedelta(days=3)
    db_session.add(MarketRegime(ts=regime_ts, window_minutes=60, regime="bull"))
    await db_session.flush()

    w = await _wallet(db_session, "earlywallet", "scalper")
    # Opened BEFORE the only regime row -> unknown.
    await _position(db_session, wallet_id=w, opened_at=regime_ts - timedelta(hours=2), pnl="1.0", roi="0.3")
    # Opened after -> bull.
    await _position(db_session, wallet_id=w, opened_at=regime_ts + timedelta(hours=2), pnl="1.0", roi="0.3")
    await db_session.commit()

    stats = await compute_regime_strategy_stats(db_session, window_days=30, now=NOW)
    keys = {(s.regime, s.style) for s in stats}
    assert ("unknown", "scalper") in keys
    assert ("bull", "scalper") in keys


@pytest.mark.asyncio
async def test_window_excludes_old_positions(db_session) -> None:
    regime_ts = NOW - timedelta(days=40)
    db_session.add(MarketRegime(ts=regime_ts, window_minutes=60, regime="bull"))
    await db_session.flush()

    w = await _wallet(db_session, "oldwallet", "holder")
    # Closed 35 days ago -> outside a 30-day window.
    old_open = NOW - timedelta(days=35)
    await _position(db_session, wallet_id=w, opened_at=old_open, pnl="1.0", roi="0.3")
    # Recent, inside window.
    await _position(db_session, wallet_id=w, opened_at=NOW - timedelta(days=1), pnl="1.0", roi="0.3")
    await db_session.commit()

    stats = await compute_regime_strategy_stats(db_session, window_days=30, now=NOW)
    assert len(stats) == 1
    assert stats[0].closed_positions == 1


@pytest.mark.asyncio
async def test_missing_style_buckets_unknown(db_session) -> None:
    db_session.add(MarketRegime(ts=NOW - timedelta(days=2), window_minutes=60, regime="bull"))
    await db_session.flush()
    w = await _wallet(db_session, "nostylewallet", None)
    await _position(db_session, wallet_id=w, opened_at=NOW - timedelta(days=1), pnl="1.0", roi="0.3")
    await db_session.commit()

    stats = await compute_regime_strategy_stats(db_session, window_days=30, now=NOW)
    assert len(stats) == 1
    assert stats[0].style == "unknown"


@pytest.mark.asyncio
async def test_no_positions_returns_empty(db_session) -> None:
    stats = await compute_regime_strategy_stats(db_session, window_days=30, now=NOW)
    assert stats == []
