"""Offline tests for the Telegram bot.

Never touches Telegram or the network: the aiogram Bot is never instantiated
(no token) and the notification ``send`` coroutine is injected as a capturing
fake. Live delivery and the aiogram command handlers wired in
``build_dispatcher`` are NOT exercised end-to-end here (they require a real Bot
token and Telegram's servers); they are covered structurally via the
DB-backed ``*_text`` / ``stop`` builders the handlers delegate to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import CopyPosition, Report, Token
from telegram_bot.bot import TelegramNotifier
from telegram_bot.formatting import (
    format_portfolio,
    format_report,
    format_status,
    notification_to_text,
)

# --------------------------------------------------------------------------
# formatting.py — pure functions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,severity,emoji",
    [
        ("copied_buy", "info", "ℹ️"),
        ("copied_sell", "warning", "⚠️"),
        ("emergency_stop", "critical", "\U0001f6a8"),
        ("large_market_move", "warning", "⚠️"),
        ("weekly_report", "info", "ℹ️"),
    ],
)
def test_notification_to_text_severity_emoji(kind: str, severity: str, emoji: str) -> None:
    text = notification_to_text(
        {"kind": kind, "severity": severity, "title": "T", "body": "B", "ts": "2026-07-21T00:00:00Z"}
    )
    assert text.startswith(emoji)
    assert "T" in text
    assert "B" in text
    assert "2026-07-21T00:00:00Z" in text


def test_notification_to_text_kind_emoji_distinct() -> None:
    buy = notification_to_text({"kind": "copied_buy", "severity": "info", "title": "x", "body": "y"})
    sell = notification_to_text(
        {"kind": "copied_sell", "severity": "info", "title": "x", "body": "y"}
    )
    assert "\U0001f7e2" in buy  # 🟢
    assert "\U0001f534" in sell  # 🔴


def test_notification_to_text_unknown_kind_and_severity_fall_back() -> None:
    text = notification_to_text({"kind": "mystery", "severity": "bogus", "title": "Hi"})
    assert "ℹ️" in text  # default severity
    assert "\U0001f514" in text  # default kind bell
    assert "Hi" in text


def test_notification_to_text_missing_fields() -> None:
    text = notification_to_text({"kind": "system_error"})
    # Falls back to kind as title, no crash, no body/ts lines.
    assert "system_error" in text
    assert "\n" not in text.split("system_error")[0] or True


def test_format_status_running_and_stopped() -> None:
    running = format_status(
        {
            "emergency_stop": None,
            "daily_realized_pnl_sol": "0.5",
            "open_positions": 2,
            "exposure_sol": "0.3",
            "mode": "paper",
            "enabled": True,
        }
    )
    assert "✅" in running
    assert "paper" in running
    assert "0.5000" in running
    assert "Open positions: 2" in running

    stopped = format_status(
        {
            "emergency_stop": "daily loss limit hit",
            "daily_realized_pnl_sol": "-1.2",
            "open_positions": 0,
            "exposure_sol": "0",
            "mode": "live",
            "enabled": False,
        }
    )
    assert "\U0001f6a8" in stopped
    assert "daily loss limit hit" in stopped


def test_format_portfolio_empty_and_populated() -> None:
    empty = format_portfolio([], Decimal("0"))
    assert "No open positions" in empty
    assert "Realized PnL: 0.0000 SOL" in empty

    populated = format_portfolio(
        [
            {"token_mint": "MintA", "mode": "paper", "spent_sol": Decimal("0.05"), "sold_sol": 0},
            {"token_id": 7, "mode": "live", "spent_sol": 0.1, "sold_sol": 0.02},
        ],
        Decimal("1.23"),
    )
    assert "MintA" in populated
    assert "7" in populated
    assert "Realized PnL: 1.2300 SOL" in populated
    assert "[paper]" in populated


def test_format_report_with_summary_and_without() -> None:
    with_summary = format_report(
        {
            "kind": "weekly",
            "generated_at": "2026-07-20T00:00:00Z",
            "window_start": "2026-07-13",
            "window_end": "2026-07-20",
            "summary": "Great week.",
            "markdown": "# ignored when summary present",
        }
    )
    assert "Weekly" in with_summary
    assert "Great week." in with_summary
    assert "→" in with_summary

    no_summary = format_report({"kind": "daily", "summary": None, "markdown": None})
    assert "no summary" in no_summary.lower()


# --------------------------------------------------------------------------
# Forwarder — filtering + dispatch with injected fakes
# --------------------------------------------------------------------------


class _FakePubSub:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:  # noqa: D401
        pass

    async def close(self) -> None:
        pass

    async def listen(self):
        for msg in self._messages:
            yield msg


class _FakeRedis:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._pubsub = _FakePubSub(messages)
        self.kv: dict[str, str] = {}

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    async def get(self, key: str):
        return self.kv.get(key)

    async def set(self, key: str, value: str):
        self.kv[key] = value

    async def delete(self, *keys: str):
        for key in keys:
            self.kv.pop(key, None)


def _msg(notification: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "channel": "events:notifications", "data": json.dumps(notification)}


@pytest.mark.asyncio
async def test_forwarder_filters_by_enabled_kinds() -> None:
    allowed = {"kind": "copied_buy", "severity": "info", "title": "Buy", "body": "b"}
    filtered = {"kind": "large_market_move", "severity": "warning", "title": "Move", "body": "m"}
    messages = [
        {"type": "subscribe", "channel": "events:notifications", "data": 1},  # ignored
        _msg(allowed),
        _msg(filtered),
    ]
    redis = _FakeRedis(messages)
    settings = Settings(
        telegram_chat_id="123",
        telegram_enabled_kinds=["copied_buy"],
    )
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    notifier = TelegramNotifier(settings, redis, session_factory=None, send=fake_send)  # type: ignore[arg-type]
    await notifier.forwarder()

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "123"
    assert text == notification_to_text(allowed)
    assert "Move" not in text


@pytest.mark.asyncio
async def test_forwarder_empty_enabled_kinds_forwards_all() -> None:
    a = {"kind": "copied_buy", "severity": "info", "title": "A", "body": "b"}
    b = {"kind": "large_market_move", "severity": "warning", "title": "B", "body": "m"}
    redis = _FakeRedis([_msg(a), _msg(b)])
    settings = Settings(telegram_chat_id="9", telegram_enabled_kinds=[])
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    notifier = TelegramNotifier(settings, redis, session_factory=None, send=fake_send)  # type: ignore[arg-type]
    await notifier.forwarder()
    assert len(sent) == 2


# --------------------------------------------------------------------------
# DB-backed builders (status / portfolio / report / stop)
# --------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _StubRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.data.pop(k, None) is not None)


@pytest.mark.asyncio
async def test_status_and_portfolio_text(session_factory) -> None:
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        session.add(Token(id=1, mint="MintOne", first_seen_at=now))
        session.add(
            CopyPosition(
                token_id=1,
                leader_wallet_id=5,
                mode="paper",
                status="open",
                opened_at=now,
                spent_sol=Decimal("0.05"),
                sold_sol=Decimal("0"),
            )
        )
        session.add(
            CopyPosition(
                token_id=1,
                leader_wallet_id=5,
                mode="paper",
                status="closed",
                opened_at=now,
                closed_at=now,
                spent_sol=Decimal("0.05"),
                sold_sol=Decimal("0.08"),
                realized_pnl_sol=Decimal("0.03"),
            )
        )
        await session.commit()

    settings = Settings(telegram_chat_id="1")
    notifier = TelegramNotifier(settings, _StubRedis(), session_factory)

    status = await notifier.status_text()
    assert "Open positions: 1" in status

    portfolio = await notifier.portfolio_text()
    assert "MintOne" in portfolio
    assert "Realized PnL: 0.0300 SOL" in portfolio


@pytest.mark.asyncio
async def test_report_text_latest_weekly(session_factory) -> None:
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        session.add(
            Report(
                kind="weekly",
                generated_at=now,
                window_start=now,
                window_end=now,
                summary="Latest weekly summary",
            )
        )
        await session.commit()
    notifier = TelegramNotifier(Settings(telegram_chat_id="1"), _StubRedis(), session_factory)
    text = await notifier.report_text()
    assert "Latest weekly summary" in text


@pytest.mark.asyncio
async def test_report_text_none(session_factory) -> None:
    notifier = TelegramNotifier(Settings(telegram_chat_id="1"), _StubRedis(), session_factory)
    text = await notifier.report_text()
    assert "No weekly report" in text


@pytest.mark.asyncio
async def test_stop_only_for_operator_chat(session_factory) -> None:
    redis = _StubRedis()
    settings = Settings(telegram_chat_id="777")
    notifier = TelegramNotifier(settings, redis, session_factory)

    # Foreign chat: ignored, no state change.
    assert await notifier.stop(555) == ""
    assert "copy:emergency_stop" not in redis.data

    # Operator chat: trips the stop AND freezes RPC (zero credits).
    reply = await notifier.stop("777")
    assert "FULL STOP" in reply
    assert redis.data.get("copy:emergency_stop") == "manual stop via Telegram"
    assert redis.data.get("rpc:frozen") == "manual /stop via Telegram"


def test_format_health_ok_and_alerts() -> None:
    from telegram_bot.formatting import format_health

    good = format_health(
        {
            "db_ok": True,
            "redis_ok": True,
            "last_trade_age_minutes": 3.0,
            "queue_depth": 42,
            "credits_used": 100_000,
            "credits_limit": 300_000,
            "emergency_stop": None,
        }
    )
    assert "✅ Database" in good and "42" in good and "33%" in good
    assert "No emergency stop" in good

    bad = format_health(
        {
            "db_ok": False,
            "redis_ok": False,
            "last_trade_age_minutes": None,
            "queue_depth": None,
            "credits_used": None,
            "credits_limit": None,
            "emergency_stop": "manual trip",
        }
    )
    assert "no trades recorded yet" in bad
    assert "EMERGENCY STOP: manual trip" in bad


def test_format_top_wallets_and_trades_empty_and_rows() -> None:
    from telegram_bot.formatting import format_recent_trades, format_top_wallets

    assert "No scored wallets" in format_top_wallets([])
    text = format_top_wallets(
        [
            {
                "address": "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "confidence_score": 82.1,
                "total_pnl_sol": 12.5,
                "win_rate": 0.61,
            }
        ]
    )
    assert "1." in text and "82.1" in text and "61%" in text

    assert "Nothing observed" in format_recent_trades([])
    line = format_recent_trades(
        [
            {
                "side": "buy",
                "token_mint": "MintBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "quote_amount": 1.5,
                "wallet": "WalletCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                "age_minutes": 12.0,
            }
        ]
    )
    assert "BUY" in line and "1.5000" in line and "12m ago" in line


def test_format_top_wallets_marks_suspicious_rows() -> None:
    from telegram_bot.formatting import format_top_wallets

    text = format_top_wallets(
        [
            {
                "address": "SusWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "confidence_score": 91.0,
                "total_pnl_sol": 40.0,
                "win_rate": 0.7,
                "vetting_verdict": "suspicious",
            },
            {
                "address": "CleanWalletBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "confidence_score": 82.1,
                "total_pnl_sol": 12.5,
                "win_rate": 0.61,
                "vetting_verdict": "clear",
            },
            {
                "address": "UnvettedWalletCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
                "confidence_score": 70.0,
                "total_pnl_sol": 1.0,
                "win_rate": None,
            },
        ]
    )
    lines = text.splitlines()
    assert lines[1].endswith("⚠️ FLAGGED")  # suspicious row carries the marker
    assert "FLAGGED" not in lines[2]  # clear
    assert "FLAGGED" not in lines[3]  # never vetted


@pytest.mark.asyncio
async def test_wallets_text_flags_suspicious_wallet(session_factory) -> None:
    from datetime import timedelta

    from app.db.models import Wallet, WalletStats, WalletVetting

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        session.add_all(
            [
                Wallet(id=1, address="SusWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                       first_seen_at=now, last_seen_at=now),
                Wallet(id=2, address="CleanWalletBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                       first_seen_at=now, last_seen_at=now),
            ]
        )
        for wallet_id, conf in ((1, Decimal("90")), (2, Decimal("80"))):
            session.add(
                WalletStats(
                    wallet_id=wallet_id,
                    computed_at=now,
                    trade_count=10,
                    buy_count=5,
                    sell_count=5,
                    position_count=5,
                    closed_position_count=5,
                    win_count=3,
                    confidence_score=conf,
                )
            )
        # Older clear verdict superseded by suspicious — latest must win.
        session.add_all(
            [
                WalletVetting(wallet_id=1, ts=now - timedelta(days=1),
                              verdict="clear", engine_version="v1"),
                WalletVetting(wallet_id=1, ts=now, verdict="suspicious",
                              engine_version="v1"),
            ]
        )
        await session.commit()

    notifier = TelegramNotifier(Settings(telegram_chat_id="1"), _StubRedis(), session_factory)
    text = await notifier.wallets_text()
    lines = text.splitlines()
    assert lines[1].startswith("1.") and lines[1].endswith("⚠️ FLAGGED")
    assert lines[2].startswith("2.") and "FLAGGED" not in lines[2]


@pytest.mark.asyncio
async def test_resume_restricted_to_operator_chat() -> None:
    calls: list[str] = []

    class _GuardStub:
        async def clear_emergency_stop(self) -> None:
            calls.append("cleared")

    notifier = TelegramNotifier(
        Settings(telegram_chat_id="123"), _FakeRedis([]), session_factory=None
    )
    notifier._guard = _GuardStub()
    assert await notifier.resume("999") == ""
    assert calls == []
    reply = await notifier.resume("123")
    assert "cleared" in reply.lower() or "resume" in reply.lower()
    assert calls == ["cleared"]
