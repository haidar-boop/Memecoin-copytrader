"""Notification service: structured operator-facing events.

Emits notifications to the Redis pub/sub :data:`NOTIFICATIONS_CHANNEL` (consumed
by the WebSocket broadcaster and the Telegram bot) and keeps a bounded rolling
buffer of the most recent notifications in a Redis list for backlog replay.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from app.logging_config import get_logger
from app.services.redis import NOTIFICATIONS_CHANNEL, publish_json

log = get_logger(__name__)

Severity = Literal["info", "warning", "critical"]

# Redis list holding the most recent notifications (newest at head).
RECENT_KEY = "notifications:recent"
RECENT_MAX = 200

NOTIFICATION_KINDS: tuple[str, ...] = (
    "new_high_confidence_wallet",
    "copied_buy",
    "copied_sell",
    "stop_loss",
    "take_profit",
    "daily_summary",
    "weekly_report",
    "confidence_change",
    "large_market_move",
    "system_error",
    "emergency_stop",
    "risk_blocked",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Notification(BaseModel):
    """A single structured, operator-facing notification."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    title: str
    body: str
    severity: Severity = "info"
    ts: str = Field(default_factory=_now_iso)
    data: dict[str, Any] = Field(default_factory=dict)

    # ---- convenience builders -------------------------------------------
    @classmethod
    def copied_buy(cls, token_mint: str, size_sol: float, wallet: str) -> Notification:
        return cls(
            kind="copied_buy",
            title="Copied buy",
            body=f"Bought {size_sol:g} SOL of {token_mint} (following {wallet})",
            severity="info",
            data={"token_mint": token_mint, "size_sol": size_sol, "wallet": wallet},
        )

    @classmethod
    def copied_sell(cls, token_mint: str, pnl_sol: float) -> Notification:
        return cls(
            kind="copied_sell",
            title="Copied sell",
            body=f"Sold {token_mint} for {pnl_sol:+g} SOL PnL",
            severity="info" if pnl_sol >= 0 else "warning",
            data={"token_mint": token_mint, "pnl_sol": pnl_sol},
        )

    @classmethod
    def emergency_stop(cls, reason: str) -> Notification:
        return cls(
            kind="emergency_stop",
            title="Emergency stop triggered",
            body=f"Trading halted: {reason}",
            severity="critical",
            data={"reason": reason},
        )

    @classmethod
    def risk_blocked(
        cls, token_mint: str, score: float, reasons: list[str]
    ) -> Notification:
        detail = "; ".join(reasons) if reasons else "risk score above threshold"
        return cls(
            kind="risk_blocked",
            title="Copy blocked by rug risk",
            body=f"Skipped {token_mint}: risk {score:.0f} ({detail})",
            severity="warning",
            data={"token_mint": token_mint, "score": score, "reasons": reasons},
        )

    @classmethod
    def large_market_move(cls, pct: float) -> Notification:
        return cls(
            kind="large_market_move",
            title="Large market move",
            body=f"SOL moved {pct:+g}% in the tracked window",
            severity="warning",
            data={"pct": pct},
        )

    @classmethod
    def report_ready(cls, kind: str) -> Notification:
        # kind is expected to be "daily_summary" or "weekly_report".
        notif_kind = kind if kind in ("daily_summary", "weekly_report") else "daily_summary"
        return cls(
            kind=notif_kind,
            title="Report ready",
            body=f"{kind.replace('_', ' ').title()} is ready",
            severity="info",
            data={"report_kind": kind},
        )

    @classmethod
    def high_confidence_wallet(cls, address: str, score: float) -> Notification:
        return cls(
            kind="new_high_confidence_wallet",
            title="New high-confidence wallet",
            body=f"Wallet {address} reached confidence {score:.2f}",
            severity="info",
            data={"address": address, "score": score},
        )

    # Confidence scores are on a 0-100 scale (see app.analytics.confidence);
    # a double-digit swing is the "worth flagging" threshold.
    CONFIDENCE_CHANGE_WARNING_DELTA: ClassVar[float] = 15.0

    @classmethod
    def confidence_change(cls, address: str, old: float, new: float) -> Notification:
        delta = new - old
        return cls(
            kind="confidence_change",
            title="Wallet confidence changed",
            body=f"Wallet {address} confidence {old:.2f} -> {new:.2f} ({delta:+.2f})",
            severity=(
                "warning" if abs(delta) >= cls.CONFIDENCE_CHANGE_WARNING_DELTA else "info"
            ),
            data={"address": address, "old": old, "new": new, "delta": delta},
        )


# Module-level builder aliases (also exposed as convenience functions).
copied_buy = Notification.copied_buy
copied_sell = Notification.copied_sell
emergency_stop = Notification.emergency_stop
risk_blocked = Notification.risk_blocked
large_market_move = Notification.large_market_move
report_ready = Notification.report_ready
high_confidence_wallet = Notification.high_confidence_wallet
confidence_change = Notification.confidence_change


class NotificationService:
    """Publishes notifications and maintains the recent-notifications buffer."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def emit(self, notification: Notification) -> None:
        payload = notification.model_dump()
        await publish_json(self._redis, NOTIFICATIONS_CHANNEL, payload)
        raw = json.dumps(payload, default=str)
        await self._redis.lpush(RECENT_KEY, raw)
        await self._redis.ltrim(RECENT_KEY, 0, RECENT_MAX - 1)
        log.info(
            "notification_emitted",
            kind=notification.kind,
            severity=notification.severity,
        )

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent notifications, newest-first."""
        limit = max(1, min(limit, RECENT_MAX))
        raw_items = await self._redis.lrange(RECENT_KEY, 0, limit - 1)
        result: list[dict[str, Any]] = []
        for item in raw_items:
            try:
                result.append(json.loads(item))
            except (ValueError, TypeError):
                continue
        return result
