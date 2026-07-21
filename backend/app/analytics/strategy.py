"""Trading-style recognition: clustering, naming, per-style performance.

Each run:
- Loads ``wallet_stats`` rows with at least 3 closed positions and builds a
  behavioral feature vector per wallet (missing values -> column median).
- If enough wallets are eligible, clusters them with KMeans on standardized
  features and names each cluster from its (un-standardized) centroid via
  fixed rules; otherwise falls back to applying the same rules per wallet.
- Appends one ``StrategyCluster`` row per cluster, updates
  ``WalletStats.style`` / ``style_confidence`` (this job's only in-place
  columns), and appends per-style ``StrategyStat`` rows over the trailing
  window computed from closed positions of labeled wallets.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Position, StrategyCluster, StrategyStat, WalletStats
from app.db.util import aware as _aware
from app.db.util import sql_cutoff
from app.db.util import to_float as _f
from app.logging_config import get_logger

log = get_logger(__name__)

FEATURE_NAMES: list[str] = [
    "log1p_avg_hold_seconds",
    "log1p_avg_entry_delay_seconds",
    "log1p_avg_position_sol",
    "trades_per_day",
    "partial_exit_ratio",
    "win_rate",
]

MIN_CLOSED_POSITIONS = 3

_MINUTE = 60.0
_HOUR = 3600.0
_DAY = 86400.0


# Rule-based fallback labels lean on hold time and entry delay; wallets where
# BOTH had to be median-imputed carry little evidence for their label.
IMPUTED_STYLE_CONFIDENCE = Decimal("0.3")


def _raw_features(stats: WalletStats) -> list[float | None]:
    hold = _f(stats.avg_hold_seconds)
    delay = _f(stats.avg_entry_delay_seconds)
    size = _f(stats.avg_position_sol)
    return [
        math.log1p(hold) if hold is not None and hold >= 0 else None,
        math.log1p(delay) if delay is not None and delay >= 0 else None,
        math.log1p(size) if size is not None and size >= 0 else None,
        _f(stats.trades_per_day),
        _f(stats.partial_exit_ratio),
        _f(stats.win_rate),
    ]


def _impute(columns: list[list[float | None]]) -> np.ndarray:
    """Column-median imputation; an all-None column becomes zeros."""
    matrix = np.zeros((len(columns), len(FEATURE_NAMES)), dtype=np.float64)
    for j in range(len(FEATURE_NAMES)):
        values = [row[j] for row in columns if row[j] is not None]
        median = float(np.median(values)) if values else 0.0
        for i, row in enumerate(columns):
            matrix[i, j] = row[j] if row[j] is not None else median
    return matrix


def _style_for(
    hold_seconds: float, entry_delay_seconds: float, partial_exit_ratio: float
) -> tuple[str, str]:
    """Apply the naming rules; returns (style, one-sentence description)."""
    if entry_delay_seconds < 2 * _MINUTE:
        return "sniper", (
            f"Average entry delay {entry_delay_seconds:.0f}s is under 120s, "
            "so these wallets snipe tokens right at launch."
        )
    if hold_seconds < 15 * _MINUTE:
        return "scalper", (
            f"Average hold time {hold_seconds / _MINUTE:.1f} minutes is under "
            "15 minutes, so these wallets scalp quick in-and-out trades."
        )
    if hold_seconds < 4 * _HOUR and entry_delay_seconds < _HOUR:
        return "momentum", (
            f"Average hold time {hold_seconds / _HOUR:.1f}h is under 4h with "
            "entry delay under 1h, so these wallets ride short momentum moves."
        )
    if hold_seconds < 7 * _DAY:
        if partial_exit_ratio > 0.5:
            return "accumulator", (
                f"Partial-exit ratio {partial_exit_ratio:.2f} exceeds 0.5, so "
                "these wallets scale out of accumulated positions gradually."
            )
        return "swing", (
            f"Average hold time {hold_seconds / _DAY:.1f} days is under 7 days, "
            "so these wallets take multi-hour to multi-day swing positions."
        )
    if partial_exit_ratio > 0.5:
        return "accumulator", (
            f"Partial-exit ratio {partial_exit_ratio:.2f} exceeds 0.5, so "
            "these wallets scale out of accumulated positions gradually."
        )
    return "holder", (
        f"Average hold time {hold_seconds / _DAY:.1f} days is at least 7 days, "
        "so these wallets hold positions long term."
    )


def _style_from_features(features: np.ndarray) -> tuple[str, str]:
    hold = float(np.expm1(features[0]))
    delay = float(np.expm1(features[1]))
    partial = float(features[4])
    return _style_for(hold, delay, partial)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}-{count}")
    return result


async def _style_stats(
    session: AsyncSession,
    styles_by_wallet: dict[int, str],
    *,
    window_days: int,
    now: datetime,
) -> list[dict]:
    """Per-style performance rows over the trailing window (append-only)."""
    if not styles_by_wallet:
        return []
    cutoff = now - timedelta(days=window_days)
    positions = (
        (
            await session.execute(
                select(Position).where(
                    Position.wallet_id.in_(list(styles_by_wallet)),
                    Position.status == "closed",
                    Position.closed_at.is_not(None),
                    # Window bound in SQL: keeps the scan O(window), not
                    # O(all history). The Python check below stays as an
                    # exactness belt for dialect edge cases.
                    Position.closed_at >= sql_cutoff(session, cutoff),
                )
            )
        )
        .scalars()
        .all()
    )
    by_style: dict[str, list[Position]] = {}
    for pos in positions:
        if pos.closed_at is None or _aware(pos.closed_at) < cutoff:
            continue
        by_style.setdefault(styles_by_wallet[pos.wallet_id], []).append(pos)

    wallet_counts: dict[str, int] = {}
    for style in styles_by_wallet.values():
        wallet_counts[style] = wallet_counts.get(style, 0) + 1

    rows: list[dict] = []
    for style in sorted(wallet_counts):
        members = by_style.get(style, [])
        closed = len(members)
        pnls = [Decimal(str(p.realized_pnl_sol)) for p in members if p.realized_pnl_sol is not None]
        rois = [_f(p.roi) for p in members]
        rois = [r for r in rois if r is not None]
        holds = [p.hold_time_seconds for p in members if p.hold_time_seconds is not None]
        wins = sum(1 for p in pnls if p > 0)
        gains = sum((p for p in pnls if p > 0), Decimal(0))
        losses = sum((-p for p in pnls if p < 0), Decimal(0))
        rows.append(
            {
                "ts": now,
                "style": style,
                "window_days": window_days,
                "wallet_count": wallet_counts[style],
                "closed_positions": closed,
                # Denominator matches the numerator's population: positions
                # with NULL PnL can't win, so counting them in `closed` here
                # silently biased win_rate downward.
                "win_rate": (
                    Decimal(wins) / Decimal(len(pnls)) if pnls else None
                ),
                "avg_roi": (
                    Decimal(str(sum(rois) / len(rois))) if rois else None
                ),
                "total_pnl_sol": sum(pnls, Decimal(0)) if pnls else None,
                "profit_factor": (gains / losses) if losses > 0 else None,
                "avg_hold_seconds": (
                    int(sum(holds) / len(holds)) if holds else None
                ),
            }
        )
    return rows


async def run_once(
    session: AsyncSession,
    *,
    k: int,
    min_wallets: int,
    window_days: int,
    now: datetime | None = None,
) -> dict:
    """One style-recognition pass; returns a summary dict."""
    now = _aware(now) if now is not None else datetime.now(tz=UTC)

    stats_rows = (
        (
            await session.execute(
                select(WalletStats)
                .where(WalletStats.closed_position_count >= MIN_CLOSED_POSITIONS)
                .order_by(WalletStats.wallet_id)
            )
        )
        .scalars()
        .all()
    )
    if not stats_rows:
        log.info("strategy_cycle_empty")
        return {"wallets_labeled": 0, "clusters": 0, "styles": {}}

    unimputed = [_raw_features(row) for row in stats_rows]
    # Hold time (index 0) and entry delay (index 1) carry the labeling rules;
    # remember who had them imputed so their labels get low confidence.
    heavily_imputed = [row[0] is None and row[1] is None for row in unimputed]
    raw = _impute(unimputed)
    clusters_written = 0

    if len(stats_rows) >= min_wallets:
        # ~10 wallets per cluster, at least 2 clusters, never more clusters
        # than samples: monotone in n and safe for tiny configured minimums.
        n_clusters = max(2, min(k, len(stats_rows) // 10))
        n_clusters = min(n_clusters, len(stats_rows))
        scaler = StandardScaler()
        standardized = scaler.fit_transform(raw)
        model = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
        labels = model.fit_predict(standardized)
        centroids_raw = scaler.inverse_transform(model.cluster_centers_)

        named: list[tuple[str, str]] = [
            _style_from_features(centroids_raw[c]) for c in range(n_clusters)
        ]
        names = _dedupe([name for name, _ in named])

        for c in range(n_clusters):
            session.add(
                StrategyCluster(
                    computed_at=now,
                    name=names[c],
                    member_count=int(np.sum(labels == c)),
                    feature_names=FEATURE_NAMES,
                    centroid=[float(v) for v in centroids_raw[c]],
                    description=named[c][1],
                )
            )
        clusters_written = n_clusters

        for i, row in enumerate(stats_rows):
            cluster = int(labels[i])
            distance = float(
                np.linalg.norm(standardized[i] - model.cluster_centers_[cluster])
            )
            # Wallets get the BASE style name; the numeric dedup suffix only
            # distinguishes StrategyCluster rows, so identically-named
            # clusters aggregate as one style in stats and the API.
            row.style = named[cluster][0]
            row.style_confidence = Decimal(f"{1.0 / (1.0 + distance):.6f}")
    else:
        for i, row in enumerate(stats_rows):
            style, _ = _style_from_features(raw[i])
            row.style = style
            row.style_confidence = (
                IMPUTED_STYLE_CONFIDENCE if heavily_imputed[i] else Decimal("1.0")
            )

    styles_by_wallet = {row.wallet_id: row.style for row in stats_rows if row.style}
    stat_rows = await _style_stats(
        session, styles_by_wallet, window_days=window_days, now=now
    )
    if stat_rows:
        await session.execute(insert(StrategyStat), stat_rows)
    await session.commit()

    style_counts: dict[str, int] = {}
    for style in styles_by_wallet.values():
        style_counts[style] = style_counts.get(style, 0) + 1
    summary = {
        "wallets_labeled": len(stats_rows),
        "clusters": clusters_written,
        "styles": style_counts,
    }
    log.info("strategy_cycle", **summary)
    return summary
