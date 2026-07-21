"""Tests for the notification service and its builders."""

from __future__ import annotations

import json

import pytest

from app.services.notifications import (
    NOTIFICATION_KINDS,
    RECENT_KEY,
    Notification,
    NotificationService,
)
from app.services.redis import NOTIFICATIONS_CHANNEL


class FakeRedis:
    """In-memory async Redis stand-in capturing pub/sub and list ops."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.lists: dict[str, list[str]] = {}
        self.trims: list[tuple[str, int, int]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def lpush(self, key: str, *values: str) -> int:
        bucket = self.lists.setdefault(key, [])
        for value in values:
            bucket.insert(0, value)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        self.trims.append((key, start, stop))
        bucket = self.lists.get(key, [])
        self.lists[key] = bucket[start : stop + 1]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        bucket = self.lists.get(key, [])
        if stop == -1:
            return bucket[start:]
        return bucket[start : stop + 1]


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


async def test_emit_publishes_and_appends(fake_redis: FakeRedis) -> None:
    service = NotificationService(fake_redis)
    notif = Notification(kind="copied_buy", title="t", body="b")

    await service.emit(notif)

    assert len(fake_redis.published) == 1
    channel, message = fake_redis.published[0]
    assert channel == NOTIFICATIONS_CHANNEL
    assert json.loads(message)["kind"] == "copied_buy"

    assert len(fake_redis.lists[RECENT_KEY]) == 1
    assert fake_redis.trims and fake_redis.trims[0][0] == RECENT_KEY


async def test_recent_is_newest_first(fake_redis: FakeRedis) -> None:
    service = NotificationService(fake_redis)
    await service.emit(Notification(kind="copied_buy", title="first", body="b"))
    await service.emit(Notification(kind="copied_sell", title="second", body="b"))

    recent = await service.recent(10)
    assert [n["title"] for n in recent] == ["second", "first"]


async def test_recent_skips_malformed(fake_redis: FakeRedis) -> None:
    fake_redis.lists[RECENT_KEY] = ["not json", json.dumps({"kind": "ok"})]
    service = NotificationService(fake_redis)
    recent = await service.recent(10)
    assert recent == [{"kind": "ok"}]


def test_all_kinds_present() -> None:
    assert "copied_buy" in NOTIFICATION_KINDS
    assert "emergency_stop" in NOTIFICATION_KINDS
    assert len(NOTIFICATION_KINDS) == 11


def test_builder_copied_buy() -> None:
    n = Notification.copied_buy("MintABC", 1.5, "WalletX")
    assert n.kind == "copied_buy"
    assert n.severity == "info"
    assert n.data == {"token_mint": "MintABC", "size_sol": 1.5, "wallet": "WalletX"}


def test_builder_copied_sell_severity() -> None:
    assert Notification.copied_sell("M", 2.0).severity == "info"
    assert Notification.copied_sell("M", -2.0).severity == "warning"


def test_builder_emergency_stop() -> None:
    n = Notification.emergency_stop("risk breach")
    assert n.kind == "emergency_stop"
    assert n.severity == "critical"
    assert n.data["reason"] == "risk breach"


def test_builder_large_market_move() -> None:
    n = Notification.large_market_move(-12.5)
    assert n.kind == "large_market_move"
    assert n.severity == "warning"
    assert n.data["pct"] == -12.5


def test_builder_report_ready() -> None:
    assert Notification.report_ready("daily_summary").kind == "daily_summary"
    assert Notification.report_ready("weekly_report").kind == "weekly_report"


def test_builder_high_confidence_wallet() -> None:
    n = Notification.high_confidence_wallet("addr", 0.91)
    assert n.kind == "new_high_confidence_wallet"
    assert n.severity == "info"
    assert n.data == {"address": "addr", "score": 0.91}


def test_builder_confidence_change_severity() -> None:
    # Confidence scores are on a 0-100 scale; a small nudge is info, a
    # double-digit swing is a warning.
    small = Notification.confidence_change("addr", 70.0, 72.0)
    assert small.kind == "confidence_change"
    assert small.severity == "info"
    big = Notification.confidence_change("addr", 40.0, 70.0)
    assert big.severity == "warning"
    assert big.data["delta"] == pytest.approx(30.0)


def test_notification_has_ts() -> None:
    n = Notification(kind="copied_buy", title="t", body="b")
    assert isinstance(n.ts, str) and "T" in n.ts
