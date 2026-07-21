"""Training-set construction for the ML models. Read-only DB access.

trade_profit rows: every WSOL-quoted BUY trade (within the training window)
whose (wallet, token) pair has a closed Position whose close happened after
the buy and within the label horizon; the label is whether that position
realized a positive PNL in SOL.

Point-in-time discipline: wallet features come from the latest
``wallet_stats_snapshots`` row at or before the buy — never from the live
``wallet_stats`` table, whose current values already encode the outcome being
predicted (look-ahead leakage that inflates every evaluation metric). Buys
with no prior snapshot train with the has_stats=0 feature variant.

wallet_persistence rows: pairs of wallet_stats_snapshots at least
``MIN_PAIR_GAP_DAYS`` apart; features come from the earlier snapshot and the
label is whether the wallet's trailing-30d PNL was still positive at the
later one.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.ml import features
from app.db.models import Position, Token, Trade, WalletStatsSnapshot
from app.db.util import aware as _aware
from app.db.util import sql_cutoff
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

MIN_PAIR_GAP_DAYS = 25
# Bound the table scans: recent behavior is what the models should learn, and
# unbounded history would grow the retrain job with total ingested volume.
TRADE_TRAINING_WINDOW_DAYS = 90
PERSISTENCE_WINDOW_DAYS = 180


def _stats_dict(row: WalletStatsSnapshot) -> dict[str, Any]:
    return {
        column: getattr(row, column)
        for column in (
            "win_rate",
            "profit_factor",
            "closed_position_count",
            "avg_hold_seconds",
            "roi_std",
            "confidence_score",
            "trade_count",
            "total_pnl_sol",
            "pnl_30d_sol",
            "trades_per_day",
            "avg_roi",
        )
    }


class _SnapshotIndex:
    """Point-in-time lookup: latest snapshot at or before a timestamp."""

    def __init__(self, snapshots: list[WalletStatsSnapshot]):
        self._by_wallet: dict[int, tuple[list[datetime], list[WalletStatsSnapshot]]] = {}
        grouped: dict[int, list[WalletStatsSnapshot]] = {}
        for snapshot in snapshots:
            grouped.setdefault(snapshot.wallet_id, []).append(snapshot)
        for wallet_id, rows in grouped.items():
            rows.sort(key=lambda s: _aware(s.ts))
            self._by_wallet[wallet_id] = ([_aware(s.ts) for s in rows], rows)

    def as_of(self, wallet_id: int, at: datetime) -> dict[str, Any] | None:
        entry = self._by_wallet.get(wallet_id)
        if entry is None:
            return None
        timestamps, rows = entry
        index = bisect_right(timestamps, at) - 1
        return _stats_dict(rows[index]) if index >= 0 else None


async def build_trade_profit_dataset(
    session: AsyncSession, *, label_horizon_hours: int, now: datetime | None = None
) -> tuple[list[str], list[list[float]], list[int], list[datetime]]:
    """Return ``(feature_names, X, y, block_times)`` sorted by block_time."""
    horizon = timedelta(hours=label_horizon_hours)
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    window_start = now - timedelta(days=TRADE_TRAINING_WINDOW_DAYS)
    cutoff = sql_cutoff(session, window_start)

    buys = (
        (
            await session.execute(
                select(Trade)
                .where(
                    Trade.side == Side.BUY.value,
                    Trade.quote_mint == WSOL_MINT,
                    Trade.block_time >= cutoff,
                )
                .order_by(Trade.block_time, Trade.signature, Trade.event_index)
            )
        )
        .scalars()
        .all()
    )
    if not buys:
        return features.trade_feature_names(), [], [], []

    closed_positions = (
        (
            await session.execute(
                select(Position)
                .where(
                    Position.status == "closed",
                    Position.closed_at.is_not(None),
                    Position.closed_at >= cutoff,
                )
                .order_by(Position.closed_at)
            )
        )
        .scalars()
        .all()
    )
    positions_by_pair: dict[tuple[int, int], list[Position]] = {}
    for position in closed_positions:
        positions_by_pair.setdefault((position.wallet_id, position.token_id), []).append(
            position
        )

    token_ids = {trade.token_id for trade in buys}
    tokens = (
        (await session.execute(select(Token).where(Token.id.in_(token_ids)))).scalars().all()
    )
    token_first_seen = {token.id: _aware(token.first_seen_at) for token in tokens}

    wallet_ids = {trade.wallet_id for trade in buys}
    snapshot_rows = (
        (
            await session.execute(
                select(WalletStatsSnapshot).where(
                    WalletStatsSnapshot.wallet_id.in_(wallet_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    snapshot_index = _SnapshotIndex(snapshot_rows)

    names = features.trade_feature_names()
    matrix: list[list[float]] = []
    labels: list[int] = []
    times: list[datetime] = []
    for trade in buys:
        block_time = _aware(trade.block_time)
        # Earliest close after the buy, within the horizon.
        closing = next(
            (
                position
                for position in positions_by_pair.get(
                    (trade.wallet_id, trade.token_id), []
                )
                if block_time
                < _aware(position.closed_at)  # type: ignore[arg-type]
                <= block_time + horizon
            ),
            None,
        )
        if closing is None:
            continue
        first_seen = token_first_seen.get(trade.token_id, block_time)
        _, vector = features.trade_features(
            wallet_stats=snapshot_index.as_of(trade.wallet_id, block_time),
            token_age_seconds=(block_time - first_seen).total_seconds(),
            buy_size_sol=float(trade.quote_amount),
            hour_of_day=block_time.hour + block_time.minute / 60.0,
            dex=trade.dex,
        )
        matrix.append(vector)
        labels.append(1 if (closing.realized_pnl_sol or 0) > 0 else 0)
        times.append(block_time)

    log.info("trade_profit_dataset_built", rows=len(matrix))
    return names, matrix, labels, times


async def build_wallet_persistence_dataset(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[list[str], list[list[float]], list[int], list[datetime]]:
    """Return ``(feature_names, X, y, ts)`` from snapshot pairs, time-sorted."""
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    window_start = now - timedelta(days=PERSISTENCE_WINDOW_DAYS)
    snapshots = (
        (
            await session.execute(
                select(WalletStatsSnapshot)
                .where(WalletStatsSnapshot.ts >= sql_cutoff(session, window_start))
                .order_by(WalletStatsSnapshot.wallet_id, WalletStatsSnapshot.ts)
            )
        )
        .scalars()
        .all()
    )
    by_wallet: dict[int, list[WalletStatsSnapshot]] = {}
    for snapshot in snapshots:
        by_wallet.setdefault(snapshot.wallet_id, []).append(snapshot)

    names = features.wallet_snapshot_feature_names()
    gap = timedelta(days=MIN_PAIR_GAP_DAYS)
    rows: list[tuple[datetime, list[float], int]] = []
    for wallet_snapshots in by_wallet.values():
        timestamps = [_aware(s.ts) for s in wallet_snapshots]
        later_index = 0
        for index, earlier in enumerate(wallet_snapshots):
            earlier_ts = timestamps[index]
            # Two-pointer walk: `later_index` only advances, keeping the
            # pairing linear instead of quadratic per wallet.
            later_index = max(later_index, index + 1)
            while (
                later_index < len(wallet_snapshots)
                and timestamps[later_index] - earlier_ts < gap
            ):
                later_index += 1
            if later_index >= len(wallet_snapshots):
                break
            later = wallet_snapshots[later_index]
            if later.pnl_30d_sol is None:
                continue
            _, vector = features.wallet_snapshot_features(_stats_dict(earlier))
            rows.append((earlier_ts, vector, 1 if later.pnl_30d_sol > 0 else 0))

    rows.sort(key=lambda item: item[0])
    log.info("wallet_persistence_dataset_built", rows=len(rows))
    return (
        names,
        [vector for _, vector, _ in rows],
        [label for _, _, label in rows],
        [ts for ts, _, _ in rows],
    )
