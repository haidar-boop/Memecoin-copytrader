"""Tests for pattern discovery (app.analytics.patterns)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import patterns
from app.db.models import DiscoveredPattern, Position, Token, TokenSnapshot, Trade, Wallet
from app.ingestion.programs import WSOL_MINT

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

np.random.seed(1234)


async def _wallet(session: AsyncSession, idx: int) -> Wallet:
    wallet = Wallet(
        address=f"wallet{idx:04d}" + "w" * 30,
        first_seen_at=NOW - timedelta(days=60),
        last_seen_at=NOW,
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _token(
    session: AsyncSession, idx: int, first_seen_at: datetime | None = None
) -> Token:
    token = Token(
        mint=f"mint{idx:04d}" + "m" * 30,
        first_seen_at=first_seen_at or (NOW - timedelta(days=10)),
    )
    session.add(token)
    await session.flush()
    return token


def _position(
    wallet_id: int,
    token_id: int,
    *,
    opened_at: datetime,
    roi: str,
    bought_sol: str = "1",
    hold_seconds: int = 600,
) -> Position:
    closed_at = opened_at + timedelta(seconds=hold_seconds)
    roi_dec = Decimal(roi)
    return Position(
        wallet_id=wallet_id,
        token_id=token_id,
        status="closed",
        opened_at=opened_at,
        closed_at=closed_at,
        bought_sol=Decimal(bought_sol),
        sold_sol=Decimal(bought_sol) * (1 + roi_dec),
        realized_pnl_sol=Decimal(bought_sol) * roi_dec,
        roi=roi_dec,
        hold_time_seconds=hold_seconds,
        trade_count=2,
    )


async def _rows(session: AsyncSession, kind: str) -> list[DiscoveredPattern]:
    return list(
        (
            await session.execute(
                select(DiscoveredPattern).where(DiscoveredPattern.kind == kind)
            )
        )
        .scalars()
        .all()
    )


async def test_hour_of_day_pattern(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    token = await _token(db_session, 0)
    # Hour 14: 8 wins / 2 losses. Hour 3: 3 wins / 7 losses.
    for i in range(10):
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW.replace(hour=14) - timedelta(days=i + 1),
                roi="0.5" if i < 8 else "-0.5",
            )
        )
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW.replace(hour=3) - timedelta(days=i + 1),
                roi="0.5" if i < 3 else "-0.5",
            )
        )
    await db_session.commit()

    inserted = await patterns.run_once(db_session, window_days=30, min_evidence=5, now=NOW)
    assert inserted > 0

    rows = await _rows(db_session, "hour_of_day")
    by_hour = {row.key["hour"]: row for row in rows}
    assert set(by_hour) == {14, 3}
    row = by_hour[14]
    assert row.evidence_count == 10
    assert row.stats["n"] == 10
    assert abs(row.stats["win_rate"] - 0.8) < 1e-9
    assert abs(row.stats["overall_win_rate"] - 0.55) < 1e-9
    assert abs(row.stats["delta_win_rate"] - 0.25) < 1e-9
    assert "14:00" in row.description and "80.0%" in row.description
    assert row.window_start is not None and row.computed_at is not None


async def test_entry_delay_buckets(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    launch = NOW - timedelta(days=5)
    token = await _token(db_session, 0, first_seen_at=launch)
    delays = [30, 300, 1800, 7200, 43200, 200000]  # one per bucket
    labels = ["<1m", "1-10m", "10-60m", "1-6h", "6-24h", ">24h"]
    for delay in delays:
        for _ in range(2):
            db_session.add(
                _position(
                    wallet.id,
                    token.id,
                    opened_at=launch + timedelta(seconds=delay),
                    roi="0.1",
                )
            )
    await db_session.commit()

    await patterns.run_once(db_session, window_days=30, min_evidence=2, now=NOW)
    rows = await _rows(db_session, "entry_delay")
    assert {row.key["bucket"] for row in rows} == set(labels)
    for row in rows:
        assert row.evidence_count == 2
        assert row.stats["overall_n"] == 12


async def test_position_size_quintiles(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    token = await _token(db_session, 0)
    sizes = [str(i) for i in range(1, 11)]  # 1..10 SOL
    for size in sizes:
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW - timedelta(days=1),
                roi="0.1",
                bought_sol=size,
            )
        )
    await db_session.commit()

    await patterns.run_once(db_session, window_days=30, min_evidence=2, now=NOW)
    rows = await _rows(db_session, "position_size")
    assert len(rows) == 5
    expected_edges = [
        float(e) for e in np.percentile(np.arange(1.0, 11.0), [0, 20, 40, 60, 80, 100])
    ]
    for row in rows:
        assert row.stats["edges"] == expected_edges
        assert row.evidence_count == 2
    quintiles = sorted(row.key["quintile"] for row in rows)
    assert quintiles == [1, 2, 3, 4, 5]


async def test_hold_time_buckets(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    token = await _token(db_session, 0)
    for hold in (60, 60, 900, 900, 7200, 7200):
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW - timedelta(days=2),
                roi="0.2",
                hold_seconds=hold,
            )
        )
    await db_session.commit()

    await patterns.run_once(db_session, window_days=30, min_evidence=2, now=NOW)
    rows = await _rows(db_session, "hold_time")
    assert {row.key["bucket"] for row in rows} == {"<5m", "5-30m", "30m-4h"}


async def test_whale_flow_net_computation(db_session: AsyncSession) -> None:
    token = await _token(db_session, 0)
    # Nine small wallets (avg 1 SOL) and one whale (avg 100 SOL).
    for i in range(9):
        small = await _wallet(db_session, i)
        db_session.add(
            _position(
                small.id, token.id, opened_at=NOW - timedelta(days=1), roi="0.1"
            )
        )
    whale = await _wallet(db_session, 9)
    db_session.add(
        _position(
            whale.id,
            token.id,
            opened_at=NOW - timedelta(days=1),
            roi="0.1",
            bought_sol="100",
        )
    )
    # Whale trades in the last 24h: 3 buys of 10 SOL, 1 sell of 4 SOL -> net +26.
    trade_rows = [
        {
            "signature": f"sig{i}" + "s" * 60,
            "event_index": 0,
            "block_time": NOW - timedelta(hours=2, minutes=i),
            "slot": 1000 + i,
            "wallet_id": whale.id,
            "token_id": token.id,
            "dex": "pumpfun",
            "side": side,
            "token_amount": Decimal("1000"),
            "quote_amount": Decimal(amount),
            "quote_mint": WSOL_MINT,
        }
        for i, (side, amount) in enumerate(
            [("buy", "10"), ("buy", "10"), ("buy", "10"), ("sell", "4")]
        )
    ]
    # A stale whale trade outside 24h must be ignored.
    trade_rows.append(
        {
            "signature": "sigstale" + "s" * 60,
            "event_index": 0,
            "block_time": NOW - timedelta(hours=30),
            "slot": 999,
            "wallet_id": whale.id,
            "token_id": token.id,
            "dex": "pumpfun",
            "side": "buy",
            "token_amount": Decimal("1000"),
            "quote_amount": Decimal("500"),
            "quote_mint": WSOL_MINT,
        }
    )
    await db_session.execute(insert(Trade), trade_rows)
    await db_session.commit()

    await patterns.run_once(db_session, window_days=30, min_evidence=4, now=NOW)
    rows = await _rows(db_session, "whale_flow")
    assert len(rows) == 1
    row = rows[0]
    assert row.key["token_id"] == token.id
    assert abs(row.stats["net_flow_sol"] - 26.0) < 1e-9
    assert row.evidence_count == 4
    assert "26.0000 SOL into" in row.description


async def test_volume_trend_detection(db_session: AsyncSession) -> None:
    rising = await _token(db_session, 0)
    falling = await _token(db_session, 1)
    snapshot_rows = []
    for i, volume in enumerate(["1", "2", "3"]):
        snapshot_rows.append(
            {
                "token_id": rising.id,
                "ts": NOW - timedelta(minutes=30 - i * 10),
                "volume_sol_1h": Decimal(volume),
            }
        )
        snapshot_rows.append(
            {
                "token_id": falling.id,
                "ts": NOW - timedelta(minutes=30 - i * 10),
                "volume_sol_1h": Decimal(str(3 - i)),
            }
        )
    await db_session.execute(insert(TokenSnapshot), snapshot_rows)
    await db_session.commit()

    await patterns.run_once(db_session, window_days=30, min_evidence=30, now=NOW)
    rows = await _rows(db_session, "volume_trend")
    assert len(rows) == 1
    row = rows[0]
    assert row.key["token_id"] == rising.id
    assert row.stats["volume_sol_1h_path"] == [1.0, 2.0, 3.0]
    assert row.evidence_count == 3


async def test_min_evidence_gate_and_window(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    token = await _token(db_session, 0)
    # Only 2 positions at hour 14 with min_evidence=3 -> no pattern.
    for i in range(2):
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW.replace(hour=14) - timedelta(days=i + 1),
                roi="0.5",
            )
        )
    # Old positions outside the window are ignored entirely.
    for i in range(5):
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW - timedelta(days=60 + i),
                roi="0.5",
            )
        )
    await db_session.commit()

    inserted = await patterns.run_once(db_session, window_days=30, min_evidence=3, now=NOW)
    assert inserted == 0
    assert await _rows(db_session, "hour_of_day") == []


async def test_run_twice_appends(db_session: AsyncSession) -> None:
    wallet = await _wallet(db_session, 0)
    token = await _token(db_session, 0)
    for i in range(5):
        db_session.add(
            _position(
                wallet.id,
                token.id,
                opened_at=NOW.replace(hour=9) - timedelta(days=i + 1),
                roi="0.3",
            )
        )
    await db_session.commit()

    first = await patterns.run_once(db_session, window_days=30, min_evidence=5, now=NOW)
    assert first > 0
    second = await patterns.run_once(db_session, window_days=30, min_evidence=5, now=NOW)
    assert second == first
    total = (
        await db_session.execute(select(func.count()).select_from(DiscoveredPattern))
    ).scalar_one()
    assert total == first + second
