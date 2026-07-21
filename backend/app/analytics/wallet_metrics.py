"""Wallet metrics engine (Phase 2).

Recomputes per-wallet performance metrics from positions and trades, scores
them with :func:`app.analytics.confidence.score_wallet`, upserts the
``wallet_stats`` row in place, and ALWAYS appends a full
``wallet_stats_snapshots`` row so score evolution becomes training data.

This job owns every metric column plus ``confidence_score`` /
``confidence_components``; it never touches ``style`` / ``style_confidence``
(the strategy job owns those) beyond copying their current values into the
append-only snapshot.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.confidence import score_wallet
from app.db.models import Position, Token, Trade, WalletStats, WalletStatsSnapshot
from app.db.util import aware as _aware
from app.db.util import bulk_append
from app.db.util import to_decimal as _dec
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

# Every column this job computes (all of WalletMetricsColumns except the
# style fields, which the strategy job owns).
METRIC_FIELDS: tuple[str, ...] = (
    "trade_count",
    "buy_count",
    "sell_count",
    "position_count",
    "closed_position_count",
    "win_count",
    "win_rate",
    "total_pnl_sol",
    "total_volume_sol",
    "avg_roi",
    "median_roi",
    "profit_factor",
    "max_drawdown_sol",
    "max_drawdown_pct",
    "avg_hold_seconds",
    "median_hold_seconds",
    "avg_position_sol",
    "max_position_sol",
    "avg_entry_delay_seconds",
    "trades_per_day",
    "partial_exit_ratio",
    "roi_std",
    "pnl_7d_sol",
    "pnl_30d_sol",
    "first_trade_at",
    "last_trade_at",
    "confidence_score",
    "confidence_components",
)

SNAPSHOT_FIELDS: tuple[str, ...] = METRIC_FIELDS + ("style", "style_confidence")


def _median_dec(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


async def _target_wallet_ids(session: AsyncSession, limit: int) -> list[int]:
    """Distinct wallet_ids ordered by most recent Position.updated_at."""
    rows = await session.execute(
        select(Position.wallet_id, func.max(Position.updated_at).label("latest"))
        .group_by(Position.wallet_id)
        .order_by(func.max(Position.updated_at).desc(), Position.wallet_id)
        .limit(limit)
    )
    return [wallet_id for wallet_id, _ in rows]


async def _trade_aggregates(
    session: AsyncSession, wallet_id: int
) -> dict[str, Any]:
    """buy/sell counts, WSOL volume and trade-time bounds in a few queries."""
    counts = {
        side: int(count)
        for side, count in await session.execute(
            select(Trade.side, func.count())
            .where(Trade.wallet_id == wallet_id)
            .group_by(Trade.side)
        )
    }
    volume = _dec(
        (
            await session.execute(
                select(func.sum(Trade.quote_amount)).where(
                    Trade.wallet_id == wallet_id, Trade.quote_mint == WSOL_MINT
                )
            )
        ).scalar_one()
    )
    first_at, last_at = (
        await session.execute(
            select(func.min(Trade.block_time), func.max(Trade.block_time)).where(
                Trade.wallet_id == wallet_id
            )
        )
    ).one()
    return {
        "buy_count": counts.get(Side.BUY.value, 0),
        "sell_count": counts.get(Side.SELL.value, 0),
        "total_volume_sol": volume,
        "first_trade_at": _aware(first_at) if first_at is not None else None,
        "last_trade_at": _aware(last_at) if last_at is not None else None,
    }


async def _sell_times_by_token(
    session: AsyncSession, wallet_id: int
) -> dict[int, list[datetime]]:
    """All sell-trade block_times per token for one wallet (single query)."""
    rows = await session.execute(
        select(Trade.token_id, Trade.block_time).where(
            Trade.wallet_id == wallet_id, Trade.side == Side.SELL.value
        )
    )
    result: dict[int, list[datetime]] = {}
    for token_id, block_time in rows:
        result.setdefault(token_id, []).append(_aware(block_time))
    return result


def _drawdown(
    closed: list[Position],
) -> tuple[Decimal | None, Decimal | None]:
    """Max drawdown over the cumulative realized-pnl curve of closed positions."""
    if not closed:
        return None, None
    ordered = sorted(closed, key=lambda p: _aware(p.closed_at or p.opened_at))
    curve = Decimal(0)
    peak = Decimal(0)
    max_dd = Decimal(0)
    max_dd_pct: Decimal | None = None
    for position in ordered:
        curve += _dec(position.realized_pnl_sol) or Decimal(0)
        peak = max(peak, curve)
        dd = peak - curve
        if dd > max_dd:
            max_dd = dd
        # A drawdown *fraction* only means something once profits existed:
        # without a positive peak there is no base to measure against.
        if peak > 0:
            pct = dd / peak
            if max_dd_pct is None or pct > max_dd_pct:
                max_dd_pct = pct
    return max_dd, max_dd_pct


async def _compute_wallet_metrics(
    session: AsyncSession, wallet_id: int, now: datetime
) -> dict[str, Any]:
    positions = (
        (await session.execute(select(Position).where(Position.wallet_id == wallet_id)))
        .scalars()
        .all()
    )
    closed = [p for p in positions if p.status == "closed"]
    trade_agg = await _trade_aggregates(session, wallet_id)
    trade_count = trade_agg["buy_count"] + trade_agg["sell_count"]

    wins = [p for p in closed if (_dec(p.realized_pnl_sol) or Decimal(0)) > 0]
    win_rate = (
        Decimal(len(wins)) / Decimal(len(closed)) if closed else None
    )
    total_pnl = (
        sum((_dec(p.realized_pnl_sol) or Decimal(0) for p in closed), Decimal(0))
        if closed
        else None
    )

    rois = [_dec(p.roi) for p in closed if p.roi is not None]
    rois = [r for r in rois if r is not None]
    avg_roi = sum(rois, Decimal(0)) / Decimal(len(rois)) if rois else None
    median_roi = _median_dec(rois)
    roi_std = (
        _dec(statistics.pstdev([float(r) for r in rois])) if len(rois) >= 2 else None
    )

    gross_profit = sum(
        (_dec(p.realized_pnl_sol) or Decimal(0) for p in wins), Decimal(0)
    )
    gross_loss = sum(
        (
            -(_dec(p.realized_pnl_sol) or Decimal(0))
            for p in closed
            if (_dec(p.realized_pnl_sol) or Decimal(0)) < 0
        ),
        Decimal(0),
    )
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    max_dd_sol, max_dd_pct = _drawdown(closed)

    holds = [int(p.hold_time_seconds) for p in closed if p.hold_time_seconds is not None]
    avg_hold = round(sum(holds) / len(holds)) if holds else None
    median_hold = round(statistics.median(holds)) if holds else None

    sizes = [_dec(p.bought_sol) or Decimal(0) for p in positions]
    avg_position = sum(sizes, Decimal(0)) / Decimal(len(sizes)) if sizes else None
    max_position = max(sizes) if sizes else None

    # Entry delay: position.opened_at - token.first_seen_at, clamped >= 0.
    token_ids = {p.token_id for p in positions}
    first_seen: dict[int, datetime] = {}
    if token_ids:
        for token_id, seen_at in await session.execute(
            select(Token.id, Token.first_seen_at).where(Token.id.in_(token_ids))
        ):
            first_seen[token_id] = _aware(seen_at)
    delays = [
        max((_aware(p.opened_at) - first_seen[p.token_id]).total_seconds(), 0.0)
        for p in positions
        if p.token_id in first_seen
    ]
    avg_entry_delay = round(sum(delays) / len(delays)) if delays else None

    first_at = trade_agg["first_trade_at"]
    last_at = trade_agg["last_trade_at"]
    trades_per_day: Decimal | None = None
    if trade_count > 0 and first_at is not None and last_at is not None:
        active_days = max((last_at - first_at).total_seconds() / 86400.0, 1.0)
        trades_per_day = (Decimal(trade_count) / Decimal(str(active_days))).quantize(
            Decimal("0.0001")
        )

    # Partial exits: closed positions with >1 sell trade inside the episode.
    # Each sell is attributed to the EARLIEST episode whose window contains
    # it, so back-to-back episodes sharing a boundary timestamp never
    # double-count the closing sell.
    partial_exit_ratio: Decimal | None = None
    if closed:
        sell_times = await _sell_times_by_token(session, wallet_id)
        episodes_by_token: dict[int, list[Position]] = {}
        for position in closed:
            if position.closed_at is None:
                continue  # unattributable window; count as single-exit
            episodes_by_token.setdefault(position.token_id, []).append(position)
        partial = 0
        for token_id, episodes in episodes_by_token.items():
            episodes.sort(key=lambda p: _aware(p.opened_at))
            counts = {id(p): 0 for p in episodes}
            for t in sorted(sell_times.get(token_id, [])):
                for position in episodes:
                    if _aware(position.opened_at) <= t <= _aware(position.closed_at):
                        counts[id(position)] += 1
                        break
            partial += sum(1 for c in counts.values() if c > 1)
        partial_exit_ratio = Decimal(partial) / Decimal(len(closed))

    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    pnl_7d = sum(
        (
            _dec(p.realized_pnl_sol) or Decimal(0)
            for p in closed
            if p.closed_at is not None and _aware(p.closed_at) >= cutoff_7d
        ),
        Decimal(0),
    )
    pnl_30d = sum(
        (
            _dec(p.realized_pnl_sol) or Decimal(0)
            for p in closed
            if p.closed_at is not None and _aware(p.closed_at) >= cutoff_30d
        ),
        Decimal(0),
    )

    metrics: dict[str, Any] = {
        "trade_count": trade_count,
        "buy_count": trade_agg["buy_count"],
        "sell_count": trade_agg["sell_count"],
        "position_count": len(positions),
        "closed_position_count": len(closed),
        "win_count": len(wins),
        "win_rate": win_rate,
        "total_pnl_sol": total_pnl,
        "total_volume_sol": trade_agg["total_volume_sol"],
        "avg_roi": avg_roi,
        "median_roi": median_roi,
        "profit_factor": profit_factor,
        "max_drawdown_sol": max_dd_sol,
        "max_drawdown_pct": max_dd_pct,
        "avg_hold_seconds": avg_hold,
        "median_hold_seconds": median_hold,
        "avg_position_sol": avg_position,
        "max_position_sol": max_position,
        "avg_entry_delay_seconds": avg_entry_delay,
        "trades_per_day": trades_per_day,
        "partial_exit_ratio": partial_exit_ratio,
        "roi_std": roi_std,
        "pnl_7d_sol": pnl_7d if closed else None,
        "pnl_30d_sol": pnl_30d if closed else None,
        "first_trade_at": first_at,
        "last_trade_at": last_at,
    }
    score, components = score_wallet(metrics)
    metrics["confidence_score"] = score
    metrics["confidence_components"] = components
    return metrics


async def run_once(
    session: AsyncSession,
    *,
    min_closed_positions: int,
    wallet_batch: int,
    now: datetime | None = None,
) -> int:
    """One metrics pass; returns the number of wallets recomputed.

    ``min_closed_positions`` is informational for downstream consumers — every
    targeted wallet still gets a row, since :func:`score_wallet` already
    shrinks low-evidence wallets toward the prior.
    """
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    wallet_ids = await _target_wallet_ids(session, wallet_batch)
    if not wallet_ids:
        return 0

    existing = {
        stats.wallet_id: stats
        for stats in (
            await session.execute(
                select(WalletStats).where(WalletStats.wallet_id.in_(wallet_ids))
            )
        )
        .scalars()
        .all()
    }

    snapshot_rows: list[dict[str, Any]] = []
    below_min = 0
    for wallet_id in wallet_ids:
        metrics = await _compute_wallet_metrics(session, wallet_id, now)
        if metrics["closed_position_count"] < min_closed_positions:
            below_min += 1
        stats = existing.get(wallet_id)
        if stats is None:
            stats = WalletStats(wallet_id=wallet_id, computed_at=now)
            session.add(stats)
            existing[wallet_id] = stats
        for field in METRIC_FIELDS:
            setattr(stats, field, metrics[field])
        stats.computed_at = now
        stats.updated_at = now

        snapshot = {field: metrics[field] for field in METRIC_FIELDS}
        snapshot["style"] = stats.style
        snapshot["style_confidence"] = stats.style_confidence
        snapshot["wallet_id"] = wallet_id
        snapshot["ts"] = now
        snapshot_rows.append(snapshot)

    # ignore_conflicts: replaying a cycle with the same `now` (crash
    # recovery) must not abort the whole batch on the (wallet_id, ts) PK.
    await bulk_append(session, WalletStatsSnapshot, snapshot_rows, ignore_conflicts=True)
    await session.commit()
    log.info(
        "wallet_metrics_cycle",
        wallets=len(wallet_ids),
        below_min_closed=below_min,
    )
    return len(wallet_ids)
