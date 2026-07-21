"""Telegram bot (aiogram 3): forwards notifications and serves commands.

Two concurrent responsibilities:

1. **Forwarder** — subscribes to :data:`NOTIFICATIONS_CHANNEL`, filters by
   ``settings.telegram_enabled_kinds`` (empty = forward all), formats each
   notification and pushes it to ``settings.telegram_chat_id`` via an injectable
   ``send`` coroutine (default wraps :meth:`aiogram.Bot.send_message`).
2. **Commands** — an aiogram Dispatcher exposing /status /portfolio /report
   /stop /help. Read-only commands query the DB; /stop trips the emergency stop
   but only for the configured operator chat.

When no bot token is configured, :meth:`run` idles forever harmlessly so the
worker can be deployed unconditionally.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import CopyPosition, Report, Token
from app.decision.safety import SafetyGuard
from app.logging_config import get_logger
from app.services.redis import NOTIFICATIONS_CHANNEL
from telegram_bot.formatting import (
    format_portfolio,
    format_report,
    format_status,
    notification_to_text,
)

log = get_logger(__name__)

SendFn = Callable[[str, str], Awaitable[None]]
SessionFactory = async_sessionmaker[AsyncSession]


class TelegramNotifier:
    """Bridges the notification bus and Telegram, and serves bot commands."""

    def __init__(
        self,
        settings: Settings,
        redis: Any,
        session_factory: SessionFactory,
        send: SendFn | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._session_factory = session_factory
        self._guard = SafetyGuard(settings, redis)
        self._bot: Any = None  # lazily built aiogram Bot (only with a token)
        self._send: SendFn | None = send

    # -- notification forwarding ------------------------------------------

    def _allowed(self, kind: str) -> bool:
        enabled = self._settings.telegram_enabled_kinds
        return not enabled or kind in enabled

    async def _ensure_send(self) -> SendFn:
        if self._send is not None:
            return self._send
        # Default: wrap the aiogram Bot. Only reached when a token exists.
        from aiogram import Bot

        if self._bot is None:
            self._bot = Bot(token=self._settings.telegram_bot_token or "")
        bot = self._bot

        async def _send(chat_id: str, text: str) -> None:
            await bot.send_message(chat_id, text)

        self._send = _send
        return self._send

    async def forwarder(self) -> None:
        """Subscribe to notifications and forward matching ones to Telegram."""
        chat_id = self._settings.telegram_chat_id
        if not chat_id:
            log.warning("telegram_chat_id_unset_forwarder_idle")
            await asyncio.Event().wait()
            return

        send = await self._ensure_send()
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(NOTIFICATIONS_CHANNEL)
        try:
            async for message in pubsub.listen():
                if not message or message.get("type") != "message":
                    continue
                payload = _decode(message.get("data"))
                if payload is None:
                    continue
                if not self._allowed(str(payload.get("kind", ""))):
                    continue
                try:
                    await send(chat_id, notification_to_text(payload))
                except Exception as exc:  # noqa: BLE001
                    log.error("telegram_send_failed", error=str(exc))
        finally:
            try:
                await pubsub.unsubscribe(NOTIFICATIONS_CHANNEL)
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass

    # -- command payload builders (DB-backed, reused by handlers) ----------

    async def status_text(self) -> str:
        async with self._session_factory() as session:
            status = await self._guard.status(session)
        return format_status(status)

    async def portfolio_text(self) -> str:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(CopyPosition, Token.mint)
                    .join(Token, Token.id == CopyPosition.token_id, isouter=True)
                    .where(CopyPosition.status == "open")
                    .order_by(CopyPosition.opened_at.desc())
                )
            ).all()
            realized = (
                await session.execute(
                    select(func.coalesce(func.sum(CopyPosition.realized_pnl_sol), 0)).where(
                        CopyPosition.status == "closed"
                    )
                )
            ).scalar_one()
        positions = [
            {
                "token_mint": mint,
                "token_id": pos.token_id,
                "mode": pos.mode,
                "spent_sol": pos.spent_sol,
                "sold_sol": pos.sold_sol,
            }
            for pos, mint in rows
        ]
        return format_portfolio(positions, Decimal(str(realized)))

    async def report_text(self) -> str:
        async with self._session_factory() as session:
            report = (
                await session.execute(
                    select(Report)
                    .where(Report.kind == "weekly")
                    .order_by(Report.generated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if report is None:
            return "\U0001f4c4 No weekly report available yet."
        return format_report(
            {
                "kind": report.kind,
                "generated_at": report.generated_at,
                "window_start": report.window_start,
                "window_end": report.window_end,
                "summary": report.summary,
                "markdown": report.markdown,
            }
        )

    async def stop(self, chat_id: str | int | None) -> str:
        """Trip the emergency stop, but only for the operator chat."""
        if str(chat_id) != str(self._settings.telegram_chat_id):
            log.warning("telegram_stop_ignored_foreign_chat", chat_id=str(chat_id))
            return ""
        await self._guard.trip_emergency_stop("manual stop via Telegram")
        return "\U0001f6d1 Emergency stop activated. Copy trading halted."

    # -- dispatcher --------------------------------------------------------

    def build_dispatcher(self) -> Any:
        from aiogram import Dispatcher
        from aiogram.filters import Command
        from aiogram.types import Message

        dp = Dispatcher()

        @dp.message(Command("status"))
        async def _status(message: Message) -> None:
            await message.answer(await self.status_text())

        @dp.message(Command("portfolio"))
        async def _portfolio(message: Message) -> None:
            await message.answer(await self.portfolio_text())

        @dp.message(Command("report"))
        async def _report(message: Message) -> None:
            await message.answer(await self.report_text())

        @dp.message(Command("stop"))
        async def _stop(message: Message) -> None:
            reply = await self.stop(message.chat.id)
            if reply:
                await message.answer(reply)

        @dp.message(Command("help", "start"))
        async def _help(message: Message) -> None:
            await message.answer(HELP_TEXT)

        return dp

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        if not self._settings.telegram_bot_token:
            log.info("telegram_bot_token_unset_idle")
            await asyncio.Event().wait()
            return

        from aiogram import Bot

        if self._bot is None:
            self._bot = Bot(token=self._settings.telegram_bot_token)
        dp = self.build_dispatcher()

        log.info("telegram_bot_starting")
        forwarder_task = asyncio.ensure_future(self.forwarder())
        polling_task = asyncio.ensure_future(dp.start_polling(self._bot))
        try:
            await asyncio.gather(forwarder_task, polling_task)
        except asyncio.CancelledError:
            pass
        finally:
            for task in (forwarder_task, polling_task):
                task.cancel()
            for task in (forwarder_task, polling_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            try:
                await self._bot.session.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("telegram_bot_stopped")


HELP_TEXT = (
    "\U0001f916 Copy-trader bot commands:\n"
    "/status — copy-trading state and exposure\n"
    "/portfolio — open positions and realized PnL\n"
    "/report — latest weekly report\n"
    "/stop — activate the emergency stop\n"
    "/help — show this message"
)


def _decode(data: Any) -> dict[str, Any] | None:
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(data, dict):
        return data
    return None


__all__ = ["TelegramNotifier"]
