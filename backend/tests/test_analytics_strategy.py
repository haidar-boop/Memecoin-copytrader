"""Tests for trading-style recognition (app.analytics.strategy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import strategy
from app.db.models import Position, StrategyCluster, StrategyStat, Token, Wallet, WalletStats

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

np.random.seed(42)


async def _seed_wallet(
    session: AsyncSession,
    idx: int,
    *,
    avg_hold_seconds: int | None,
    avg_entry_delay_seconds: int | None,
    avg_position_sol: str | None = "1.5",
    trades_per_day: str | None = "10",
    partial_exit_ratio: str | None = "0.1",
    win_rate: str | None = "0.5",
    closed_position_count: int = 5,
) -> Wallet:
    wallet = Wallet(
        address=f"wallet{idx:04d}" + "x" * 30,
        first_seen_at=NOW - timedelta(days=30),
        last_seen_at=NOW,
    )
    session.add(wallet)
    await session.flush()
    session.add(
        WalletStats(
            wallet_id=wallet.id,
            computed_at=NOW,
            closed_position_count=closed_position_count,
            avg_hold_seconds=avg_hold_seconds,
            avg_entry_delay_seconds=avg_entry_delay_seconds,
            avg_position_sol=Decimal(avg_position_sol) if avg_position_sol else None,
            trades_per_day=Decimal(trades_per_day) if trades_per_day else None,
            partial_exit_ratio=Decimal(partial_exit_ratio) if partial_exit_ratio else None,
            win_rate=Decimal(win_rate) if win_rate else None,
        )
    )
    return wallet


async def _seed_position(
    session: AsyncSession,
    wallet: Wallet,
    token: Token,
    *,
    pnl: str,
    roi: str,
    hold: int,
    closed_at: datetime,
) -> None:
    session.add(
        Position(
            wallet_id=wallet.id,
            token_id=token.id,
            status="closed",
            opened_at=closed_at - timedelta(seconds=hold),
            closed_at=closed_at,
            realized_pnl_sol=Decimal(pnl),
            roi=Decimal(roi),
            hold_time_seconds=hold,
        )
    )


async def _seed_token(session: AsyncSession) -> Token:
    token = Token(mint="mint" + "t" * 40, first_seen_at=NOW - timedelta(days=30))
    session.add(token)
    await session.flush()
    return token


async def test_clustering_separates_and_names_groups(db_session: AsyncSession) -> None:
    token = await _seed_token(db_session)
    scalpers: list[Wallet] = []
    holders: list[Wallet] = []
    # 12 scalpers: hold ~5 min, entry delay well over 2 min (not snipers).
    for i in range(12):
        w = await _seed_wallet(
            db_session,
            i,
            avg_hold_seconds=300 + i * 5,
            avg_entry_delay_seconds=3600 + i * 10,
            trades_per_day="40",
            partial_exit_ratio="0.05",
            win_rate="0.4",
        )
        scalpers.append(w)
        await _seed_position(
            db_session, w, token, pnl="0.5", roi="0.2", hold=300, closed_at=NOW - timedelta(days=1)
        )
    # 12 holders: hold ~10 days, low activity, low partial exits.
    for i in range(12):
        w = await _seed_wallet(
            db_session,
            100 + i,
            avg_hold_seconds=10 * 86400 + i * 1000,
            avg_entry_delay_seconds=2 * 86400,
            trades_per_day="0.5",
            partial_exit_ratio="0.1",
            win_rate="0.6",
            avg_position_sol="20",
        )
        holders.append(w)
        await _seed_position(
            db_session, w, token, pnl="-1.0", roi="-0.3", hold=864000, closed_at=NOW - timedelta(days=2)
        )
    await db_session.commit()

    summary = await strategy.run_once(
        db_session, k=2, min_wallets=20, window_days=30, now=NOW
    )
    assert summary["wallets_labeled"] == 24
    assert summary["clusters"] == 2
    assert summary["styles"] == {"scalper": 12, "holder": 12}

    stats = (await db_session.execute(select(WalletStats))).scalars().all()
    by_wallet = {s.wallet_id: s for s in stats}
    for w in scalpers:
        assert by_wallet[w.id].style == "scalper"
        assert Decimal("0") < by_wallet[w.id].style_confidence <= Decimal("1")
    for w in holders:
        assert by_wallet[w.id].style == "holder"

    clusters = (await db_session.execute(select(StrategyCluster))).scalars().all()
    assert len(clusters) == 2
    assert {c.name for c in clusters} == {"scalper", "holder"}
    for c in clusters:
        assert c.member_count == 12
        assert c.feature_names == strategy.FEATURE_NAMES
        assert len(c.centroid) == 6
        assert c.description

    stat_rows = (await db_session.execute(select(StrategyStat))).scalars().all()
    by_style = {r.style: r for r in stat_rows}
    assert set(by_style) == {"scalper", "holder"}
    scal = by_style["scalper"]
    assert scal.wallet_count == 12
    assert scal.closed_positions == 12
    assert scal.win_rate == Decimal("1")
    assert scal.total_pnl_sol == Decimal("6")
    assert scal.profit_factor is None  # no losses
    assert scal.avg_hold_seconds == 300
    hold = by_style["holder"]
    assert hold.win_rate == Decimal("0")
    assert hold.total_pnl_sol == Decimal("-12")


async def test_deterministic_across_runs(db_session: AsyncSession) -> None:
    for i in range(10):
        await _seed_wallet(
            db_session, i, avg_hold_seconds=300, avg_entry_delay_seconds=3600
        )
    for i in range(10):
        await _seed_wallet(
            db_session,
            100 + i,
            avg_hold_seconds=10 * 86400,
            avg_entry_delay_seconds=2 * 86400,
            trades_per_day="0.5",
        )
    await db_session.commit()

    s1 = await strategy.run_once(db_session, k=3, min_wallets=20, window_days=30, now=NOW)
    labels1 = {
        s.wallet_id: (s.style, s.style_confidence)
        for s in (await db_session.execute(select(WalletStats))).scalars()
    }
    s2 = await strategy.run_once(db_session, k=3, min_wallets=20, window_days=30, now=NOW)
    labels2 = {
        s.wallet_id: (s.style, s.style_confidence)
        for s in (await db_session.execute(select(WalletStats))).scalars()
    }
    assert s1 == s2
    assert labels1 == labels2
    # Two runs -> two appended cluster batches (append-only).
    clusters = (await db_session.execute(select(StrategyCluster))).scalars().all()
    assert len(clusters) == 2 * s1["clusters"]


async def test_rule_based_path_below_min_wallets(db_session: AsyncSession) -> None:
    token = await _seed_token(db_session)
    w_sniper = await _seed_wallet(
        db_session, 1, avg_hold_seconds=600, avg_entry_delay_seconds=60
    )
    w_scalper = await _seed_wallet(
        db_session, 2, avg_hold_seconds=300, avg_entry_delay_seconds=3600
    )
    w_momentum = await _seed_wallet(
        db_session, 3, avg_hold_seconds=2 * 3600, avg_entry_delay_seconds=1800
    )
    w_accum = await _seed_wallet(
        db_session,
        4,
        avg_hold_seconds=2 * 86400,
        avg_entry_delay_seconds=2 * 86400,
        partial_exit_ratio="0.8",
    )
    w_holder = await _seed_wallet(
        db_session, 5, avg_hold_seconds=10 * 86400, avg_entry_delay_seconds=2 * 86400
    )
    await _seed_position(
        db_session, w_scalper, token, pnl="1", roi="0.5", hold=300, closed_at=NOW - timedelta(days=3)
    )
    await db_session.commit()

    summary = await strategy.run_once(
        db_session, k=5, min_wallets=20, window_days=30, now=NOW
    )
    assert summary["clusters"] == 0
    assert summary["styles"] == {
        "sniper": 1,
        "scalper": 1,
        "momentum": 1,
        "accumulator": 1,
        "holder": 1,
    }
    stats = {
        s.wallet_id: s
        for s in (await db_session.execute(select(WalletStats))).scalars()
    }
    assert stats[w_sniper.id].style == "sniper"
    assert stats[w_momentum.id].style == "momentum"
    assert stats[w_accum.id].style == "accumulator"
    assert stats[w_holder.id].style == "holder"
    assert all(s.style_confidence == Decimal("1") for s in stats.values())
    # No clusters written, but per-style stats are.
    assert (await db_session.execute(select(StrategyCluster))).scalars().all() == []
    stat_styles = {
        r.style for r in (await db_session.execute(select(StrategyStat))).scalars()
    }
    assert stat_styles == {"sniper", "scalper", "momentum", "accumulator", "holder"}


async def test_none_features_do_not_crash(db_session: AsyncSession) -> None:
    await _seed_wallet(
        db_session,
        1,
        avg_hold_seconds=None,
        avg_entry_delay_seconds=None,
        avg_position_sol=None,
        trades_per_day=None,
        partial_exit_ratio=None,
        win_rate=None,
    )
    await _seed_wallet(
        db_session, 2, avg_hold_seconds=300, avg_entry_delay_seconds=3600
    )
    await db_session.commit()
    summary = await strategy.run_once(
        db_session, k=5, min_wallets=20, window_days=30, now=NOW
    )
    assert summary["wallets_labeled"] == 2
    stats = (await db_session.execute(select(WalletStats))).scalars().all()
    assert all(s.style is not None for s in stats)


async def test_ineligible_wallets_skipped(db_session: AsyncSession) -> None:
    await _seed_wallet(
        db_session,
        1,
        avg_hold_seconds=300,
        avg_entry_delay_seconds=3600,
        closed_position_count=2,
    )
    await db_session.commit()
    summary = await strategy.run_once(
        db_session, k=5, min_wallets=20, window_days=30, now=NOW
    )
    assert summary == {"wallets_labeled": 0, "clusters": 0, "styles": {}}
