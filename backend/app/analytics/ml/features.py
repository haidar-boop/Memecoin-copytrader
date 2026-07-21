"""Feature engineering for the ML models. Pure functions, no I/O.

``trade_features`` builds the feature vector for the "trade_profit" model:
wallet-quality metrics as of prediction time, token age, buy size, cyclic
hour-of-day, and a dex one-hot over the :class:`app.ingestion.events.Dex`
enum. ``wallet_snapshot_features`` builds the "wallet_persistence" vector
from a wallet_stats_snapshot row's metric dict.
"""

from __future__ import annotations

import math
from typing import Any

from app.analytics.confidence import PRIOR_SCORE
from app.ingestion.events import Dex

DEX_VALUES: tuple[str, ...] = tuple(d.value for d in Dex)

_WALLET_STAT_KEYS = (
    "win_rate",
    "profit_factor",
    "closed_position_count",
    "avg_hold_seconds",
    "roi_std",
    "confidence_score",
)


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def trade_feature_names() -> list[str]:
    names = [
        "has_stats",
        "wallet_win_rate",
        "wallet_profit_factor",
        "wallet_closed_positions",
        "wallet_avg_hold_seconds",
        "wallet_roi_std",
        "wallet_confidence_score",
        "token_age_seconds",
        "buy_size_sol",
        "hour_sin",
        "hour_cos",
    ]
    names.extend(f"dex_{value}" for value in DEX_VALUES)
    return names


def trade_features(
    *,
    wallet_stats: dict[str, Any] | None,
    token_age_seconds: float,
    buy_size_sol: float,
    hour_of_day: float,
    dex: str,
) -> tuple[list[str], list[float]]:
    """Build the trade_profit feature vector.

    ``wallet_stats`` uses WalletStats column names; None means the wallet has
    no stats row yet (has_stats=0, wallet features zeroed, confidence at the
    scoring prior).
    """
    if wallet_stats is not None:
        stats_part = [
            1.0,
            _num(wallet_stats.get("win_rate")),
            _num(wallet_stats.get("profit_factor")),
            _num(wallet_stats.get("closed_position_count")),
            _num(wallet_stats.get("avg_hold_seconds")),
            _num(wallet_stats.get("roi_std")),
            _num(wallet_stats.get("confidence_score"), default=PRIOR_SCORE),
        ]
    else:
        stats_part = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, PRIOR_SCORE]

    angle = 2.0 * math.pi * (hour_of_day % 24.0) / 24.0
    vector = [
        *stats_part,
        max(_num(token_age_seconds), 0.0),
        max(_num(buy_size_sol), 0.0),
        math.sin(angle),
        math.cos(angle),
        *[1.0 if dex == value else 0.0 for value in DEX_VALUES],
    ]
    return trade_feature_names(), vector


def wallet_snapshot_feature_names() -> list[str]:
    return [
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
    ]


def wallet_snapshot_features(stats: dict[str, Any]) -> tuple[list[str], list[float]]:
    """Build the wallet_persistence vector from a snapshot metric dict."""
    vector = [
        _num(stats.get("win_rate")),
        _num(stats.get("profit_factor")),
        _num(stats.get("closed_position_count")),
        _num(stats.get("avg_hold_seconds")),
        _num(stats.get("roi_std")),
        _num(stats.get("confidence_score"), default=PRIOR_SCORE),
        _num(stats.get("trade_count")),
        _num(stats.get("total_pnl_sol")),
        _num(stats.get("pnl_30d_sol")),
        _num(stats.get("trades_per_day")),
        _num(stats.get("avg_roi")),
    ]
    return wallet_snapshot_feature_names(), vector
