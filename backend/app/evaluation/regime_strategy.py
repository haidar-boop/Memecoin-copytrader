"""Per-(regime, strategy) performance: which trading styles win in which
market conditions. Append-only ``regime_strategy_stats`` rows.

Each pass joins closed :class:`Position` episodes inside a trailing window to
two things: the leader wallet's learned trading style (``WalletStats.style``)
and the :class:`MarketRegime` that was in effect at the moment the position was
OPENED (the most recent regime row whose ``ts`` is at or before ``opened_at``).
Positions opened before any regime was ever recorded bucket as ``unknown``, as
do wallets with no known style. For every (regime, style) group with at least
one closed position we insert one :class:`RegimeStrategyStat` carrying the
closed-position count, win rate, average ROI, and total realized PNL.

The regime rows are loaded once and bisected per position rather than queried
per position; positions are loaded with a window-bounded SQL predicate.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketRegime, Position, RegimeStrategyStat, WalletStats
from app.db.util import aware as _aware
from app.db.util import quantize_sol, sql_cutoff, to_decimal, to_float
from app.logging_config import get_logger

log = get_logger(__name__)

UNKNOWN = "unknown"


async def _closed_positions(session: AsyncSession, window_start: datetime) -> list[Position]:
    rows = (
        (
            await session.execute(
                select(Position).where(
                    Position.status == "closed",
                    Position.closed_at.is_not(None),
                    # Window bound in SQL so the scan stays O(window); the
                    # Python check below is the exactness belt.
                    Position.closed_at >= sql_cutoff(session, window_start),
                )
            )
        )
        .scalars()
        .all()
    )
    return [p for p in rows if _aware(p.closed_at) >= window_start]  # type: ignore[arg-type]


async def _styles(session: AsyncSession, wallet_ids: set[int]) -> dict[int, str]:
    if not wallet_ids:
        return {}
    rows = await session.execute(
        select(WalletStats.wallet_id, WalletStats.style).where(
            WalletStats.wallet_id.in_(wallet_ids)
        )
    )
    return {wid: style for wid, style in rows if style}


async def _regime_timeline(session: AsyncSession) -> tuple[list[datetime], list[str]]:
    """All regime rows ordered by ts, as parallel (timestamps, labels) lists."""
    rows = await session.execute(
        select(MarketRegime.ts, MarketRegime.regime).order_by(MarketRegime.ts)
    )
    timestamps: list[datetime] = []
    labels: list[str] = []
    for ts, regime in rows:
        timestamps.append(_aware(ts))
        labels.append(regime)
    return timestamps, labels


def _regime_at(
    opened_at: datetime, timestamps: list[datetime], labels: list[str]
) -> str:
    """The most recent regime label with ts <= opened_at, else ``unknown``."""
    idx = bisect_right(timestamps, opened_at)
    return labels[idx - 1] if idx > 0 else UNKNOWN


async def compute_regime_strategy_stats(
    session: AsyncSession,
    *,
    window_days: int,
    now: datetime | None = None,
) -> list[RegimeStrategyStat]:
    """One (regime, strategy) performance pass over the trailing window.

    Inserts one :class:`RegimeStrategyStat` per (regime, style) group with at
    least one closed position and returns the inserted rows.
    """
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    window_start = now - timedelta(days=window_days)

    positions = await _closed_positions(session, window_start)
    if not positions:
        log.info("regime_strategy_cycle", inserted=0, closed_positions=0)
        return []

    styles = await _styles(session, {p.wallet_id for p in positions})
    timestamps, labels = await _regime_timeline(session)

    # (regime, style) -> list of positions.
    grouped: dict[tuple[str, str], list[Position]] = {}
    for p in positions:
        regime = _regime_at(_aware(p.opened_at), timestamps, labels)
        style = styles.get(p.wallet_id, UNKNOWN)
        grouped.setdefault((regime, style), []).append(p)

    stats: list[RegimeStrategyStat] = []
    for (regime, style), members in sorted(grouped.items()):
        n = len(members)
        wins = sum(
            1
            for p in members
            if (pnl := to_decimal(p.realized_pnl_sol)) is not None and pnl > 0
        )
        rois = [f for p in members if (f := to_float(p.roi)) is not None]
        total_pnl = sum(
            (to_decimal(p.realized_pnl_sol) or Decimal(0) for p in members),
            Decimal(0),
        )
        stats.append(
            RegimeStrategyStat(
                ts=now,
                regime=regime,
                style=style,
                window_days=window_days,
                closed_positions=n,
                win_rate=Decimal(str(wins / n)),
                avg_roi=Decimal(str(float(np.mean(rois)))) if rois else None,
                total_pnl_sol=quantize_sol(total_pnl),
            )
        )

    session.add_all(stats)
    await session.commit()
    log.info(
        "regime_strategy_cycle",
        inserted=len(stats),
        closed_positions=len(positions),
        groups=sorted(f"{r}/{s}" for (r, s) in grouped),
    )
    return stats
