"""Tests for the wallet metrics engine (app.analytics.wallet_metrics)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import wallet_metrics
from app.analytics.confidence import score_wallet
from app.db.models import Position, Token, Trade, Wallet, WalletStats, WalletStatsSnapshot
from app.ingestion.programs import WSOL_MINT

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

_sig_counter = 0


def _sig() -> str:
    global _sig_counter
    _sig_counter += 1
    return f"sig{_sig_counter:05d}"


async def _wallet(session: AsyncSession, address: str) -> Wallet:
    wallet = Wallet(address=address, first_seen_at=NOW - timedelta(days=10), last_seen_at=NOW)
    session.add(wallet)
    await session.flush()
    return wallet


async def _token(session: AsyncSession, mint: str, first_seen_at: datetime) -> Token:
    token = Token(mint=mint, first_seen_at=first_seen_at)
    session.add(token)
    await session.flush()
    return token


async def _persist(session: AsyncSession, *rows: object) -> None:
    """Insert rows one at a time: batched ORM inserts of tz-aware composite
    PKs trip SQLAlchemy's RETURNING sentinel matching on SQLite."""
    for row in rows:
        session.add(row)
        await session.flush()


def _trade(
    wallet_id: int,
    token_id: int,
    side: str,
    block_time: datetime,
    quote_amount: Decimal = Decimal(1),
) -> Trade:
    return Trade(
        signature=_sig(),
        event_index=0,
        block_time=block_time,
        slot=1,
        wallet_id=wallet_id,
        token_id=token_id,
        dex="pumpfun",
        side=side,
        token_amount=Decimal(1000),
        quote_amount=quote_amount,
        quote_mint=WSOL_MINT,
    )


def _position(
    wallet_id: int,
    token_id: int,
    *,
    opened_at: datetime,
    closed_at: datetime | None,
    pnl: Decimal,
    bought_sol: Decimal = Decimal(1),
    roi: Decimal | None = None,
    hold_seconds: int | None = None,
    updated_at: datetime | None = None,
) -> Position:
    status = "closed" if closed_at is not None else "open"
    return Position(
        wallet_id=wallet_id,
        token_id=token_id,
        status=status,
        opened_at=opened_at,
        closed_at=closed_at,
        bought_sol=bought_sol,
        sold_sol=bought_sol + pnl if closed_at else Decimal(0),
        realized_pnl_sol=pnl,
        roi=roi if roi is not None else (pnl / bought_sol if closed_at else None),
        hold_time_seconds=hold_seconds
        if hold_seconds is not None
        else (int((closed_at - opened_at).total_seconds()) if closed_at else None),
        updated_at=updated_at or NOW,
    )


async def _seed_mixed_wallet(session: AsyncSession) -> Wallet:
    """6 closed positions, pnl +2,-1,+3,-2,+1,-3 in closed_at order."""
    wallet = await _wallet(session, "mixed")
    pnls = [Decimal(2), Decimal(-1), Decimal(3), Decimal(-2), Decimal(1), Decimal(-3)]
    for i, pnl in enumerate(pnls):
        token = await _token(session, f"mixmint{i}", NOW - timedelta(days=6, minutes=5))
        opened = NOW - timedelta(days=6 - i)
        closed = opened + timedelta(hours=1)
        await _persist(
            session, _position(wallet.id, token.id, opened_at=opened, closed_at=closed, pnl=pnl)

        )
        await _persist(session, _trade(wallet.id, token.id, "buy", opened, Decimal(1)))
        await _persist(session, _trade(wallet.id, token.id, "sell", closed, Decimal(1) + pnl))
    await session.flush()
    return wallet


async def test_mixed_wallet_metrics(db_session: AsyncSession) -> None:
    wallet = await _seed_mixed_wallet(db_session)
    await db_session.commit()

    count = await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=NOW
    )
    assert count == 1

    stats = await db_session.get(WalletStats, wallet.id)
    assert stats is not None
    assert stats.trade_count == 12
    assert stats.buy_count == 6
    assert stats.sell_count == 6
    assert stats.position_count == 6
    assert stats.closed_position_count == 6
    assert stats.win_count == 3
    assert Decimal(str(stats.win_rate)) == Decimal("0.5")
    assert Decimal(str(stats.total_pnl_sol)) == Decimal(0)
    # gross_profit = 6, gross_loss = 6
    assert Decimal(str(stats.profit_factor)) == Decimal(1)
    # curve: 2,1,4,2,3,0 -> peaks 2,2,4,4,4,4 -> max dd 4, pct 4/4 = 1
    assert Decimal(str(stats.max_drawdown_sol)) == Decimal(4)
    assert Decimal(str(stats.max_drawdown_pct)) == Decimal(1)
    assert stats.avg_hold_seconds == 3600
    assert stats.median_hold_seconds == 3600
    assert Decimal(str(stats.avg_position_sol)) == Decimal(1)
    assert Decimal(str(stats.max_position_sol)) == Decimal(1)
    # volume: 6 buys of 1 SOL + sells of 3,0,4,-1... sells sum = 6 + 0 = 6
    assert Decimal(str(stats.total_volume_sol)) == Decimal(12)
    # all closed within 7d window
    assert Decimal(str(stats.pnl_7d_sol)) == Decimal(0)
    assert Decimal(str(stats.pnl_30d_sol)) == Decimal(0)
    assert stats.confidence_score is not None
    assert stats.confidence_components
    assert stats.style is None  # metrics job never touches style


async def test_all_wins_profit_factor_none(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, "winner")
    for i in range(5):
        token = await _token(db_session, f"winmint{i}", NOW - timedelta(days=5))
        opened = NOW - timedelta(days=5 - i)
        closed = opened + timedelta(hours=2)
        await _persist(
            db_session, _position(wallet.id, token.id, opened_at=opened, closed_at=closed, pnl=Decimal(1))

        )
        await _persist(db_session, _trade(wallet.id, token.id, "buy", opened))
        await _persist(db_session, _trade(wallet.id, token.id, "sell", closed, Decimal(2)))
    await db_session.commit()

    await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=NOW
    )
    stats = await db_session.get(WalletStats, wallet.id)
    assert stats is not None
    assert stats.profit_factor is None
    assert Decimal(str(stats.win_rate)) == Decimal(1)
    assert Decimal(str(stats.max_drawdown_sol)) == Decimal(0)
    assert Decimal(str(stats.total_pnl_sol)) == Decimal(5)


async def test_partial_exit_and_entry_delay(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, "partial")
    # Position 1: two sells inside the episode -> partial exit.
    token1 = await _token(db_session, "pmint1", NOW - timedelta(days=3, seconds=100))
    opened1 = NOW - timedelta(days=3)
    closed1 = opened1 + timedelta(hours=1)
    await _persist(
        db_session, _position(wallet.id, token1.id, opened_at=opened1, closed_at=closed1, pnl=Decimal(1))

    )
    await _persist(db_session, _trade(wallet.id, token1.id, "buy", opened1))
    await _persist(db_session, _trade(wallet.id, token1.id, "sell", opened1 + timedelta(minutes=30)))
    await _persist(db_session, _trade(wallet.id, token1.id, "sell", closed1))
    # Position 2: single sell -> not partial. Entry delay 300s.
    token2 = await _token(db_session, "pmint2", NOW - timedelta(days=2, seconds=300))
    opened2 = NOW - timedelta(days=2)
    closed2 = opened2 + timedelta(hours=1)
    await _persist(
        db_session, _position(wallet.id, token2.id, opened_at=opened2, closed_at=closed2, pnl=Decimal(-1))

    )
    await _persist(db_session, _trade(wallet.id, token2.id, "buy", opened2))
    await _persist(db_session, _trade(wallet.id, token2.id, "sell", closed2))
    await db_session.commit()

    await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=NOW
    )
    stats = await db_session.get(WalletStats, wallet.id)
    assert stats is not None
    assert Decimal(str(stats.partial_exit_ratio)) == Decimal("0.5")
    # delays: 100s and 300s -> mean 200
    assert stats.avg_entry_delay_seconds == 200
    assert stats.buy_count == 2
    assert stats.sell_count == 3


async def test_snapshot_appended_and_second_run_updates_in_place(
    db_session: AsyncSession,
) -> None:
    wallet = await _seed_mixed_wallet(db_session)
    await db_session.commit()

    await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=NOW
    )
    snap_count = (
        await db_session.execute(select(func.count()).select_from(WalletStatsSnapshot))
    ).scalar_one()
    assert snap_count == 1

    later = NOW + timedelta(minutes=5)
    await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=later
    )
    stats_rows = (
        (await db_session.execute(select(WalletStats))).scalars().all()
    )
    assert len(stats_rows) == 1
    assert stats_rows[0].wallet_id == wallet.id
    computed_at = stats_rows[0].computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=UTC)
    assert computed_at == later

    snaps = (
        (await db_session.execute(select(WalletStatsSnapshot))).scalars().all()
    )
    assert len(snaps) == 2
    assert all(s.wallet_id == wallet.id for s in snaps)
    assert snaps[0].confidence_score == snaps[1].confidence_score


async def test_below_min_closed_positions_gets_shrunk_score(
    db_session: AsyncSession,
) -> None:
    wallet = await _wallet(db_session, "newbie")
    token = await _token(db_session, "newmint", NOW - timedelta(days=1))
    opened = NOW - timedelta(hours=12)
    closed = opened + timedelta(hours=1)
    await _persist(
        db_session, _position(wallet.id, token.id, opened_at=opened, closed_at=closed, pnl=Decimal(5))

    )
    await _persist(db_session, _trade(wallet.id, token.id, "buy", opened))
    await _persist(db_session, _trade(wallet.id, token.id, "sell", closed, Decimal(6)))
    await db_session.commit()

    count = await wallet_metrics.run_once(
        db_session, min_closed_positions=5, wallet_batch=100, now=NOW
    )
    assert count == 1  # still gets a row
    stats = await db_session.get(WalletStats, wallet.id)
    assert stats is not None
    assert stats.closed_position_count == 1
    # One lucky win shrinks hard toward the prior (30), well below a real score.
    score = Decimal(str(stats.confidence_score))
    assert Decimal(20) < score < Decimal(45)
    # And matches the pure scorer applied to the same metrics.
    metrics = {
        "closed_position_count": 1,
        "win_count": 1,
        "profit_factor": None,
        "roi_std": None,
        "pnl_30d_sol": Decimal(5),
    }
    expected, _ = score_wallet(metrics)
    assert score == expected
