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
from app.db.models import (
    CopyPosition,
    Report,
    Token,
    Trade,
    TradeDecision,
    Wallet,
    WalletStats,
    WalletVetting,
)
from app.decision.safety import EMERGENCY_STOP_KEY, SafetyGuard
from app.logging_config import get_logger
from app.services.redis import NOTIFICATIONS_CHANNEL
from telegram_bot.formatting import (
    format_decisions,
    format_health,
    format_portfolio,
    format_recent_trades,
    format_report,
    format_status,
    format_top_wallets,
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
        backoff = 1.0
        while True:
            # Reconnect on Redis loss — pubsub.listen() raises and never
            # resubscribes by itself; without this loop the forwarder dies
            # silently at the first blip.
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(NOTIFICATIONS_CHANNEL)
                backoff = 1.0
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
                # Clean end of listen() = deliberate close (shutdown, tests);
                # a dead connection raises instead. Only errors reconnect.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on transport errors
                log.warning("telegram_feed_disconnected", error=str(exc),
                            retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
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

    async def health_text(self) -> str:
        from datetime import UTC, datetime

        health: dict[str, Any] = {
            "db_ok": False,
            "redis_ok": False,
            "last_trade_age_minutes": None,
            "queue_depth": None,
            "credits_used": None,
            "credits_limit": self._settings.rpc_daily_credit_budget or None,
            "priority_credits_used": None,
            "priority_credits_limit": self._settings.rpc_priority_daily_credit_budget
            or None,
            "emergency_stop": None,
            "rpc_frozen": None,
        }
        try:
            async with self._session_factory() as session:
                latest = (
                    await session.execute(
                        select(func.max(Trade.block_time))
                    )
                ).scalar_one_or_none()
            health["db_ok"] = True
            if latest is not None:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=UTC)
                health["last_trade_age_minutes"] = max(
                    (datetime.now(UTC) - latest).total_seconds() / 60.0, 0.0
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_health_db_failed", error=str(exc))
        try:
            from app.services.rpc import RpcBudget

            health["queue_depth"] = await self._redis.xlen(
                self._settings.ingest_stream_key
            )
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            raw = await self._redis.get(RpcBudget.KEY_PREFIX + today)
            health["credits_used"] = int(raw) if raw is not None else 0
            raw_priority = await self._redis.get(RpcBudget.PRIORITY_KEY_PREFIX + today)
            health["priority_credits_used"] = (
                int(raw_priority) if raw_priority is not None else 0
            )
            health["redis_ok"] = True
            health["emergency_stop"] = await self._redis.get(EMERGENCY_STOP_KEY)
            from app.services.rpc import rpc_freeze_reason

            health["rpc_frozen"] = await rpc_freeze_reason(self._redis)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_health_redis_failed", error=str(exc))
        return format_health(health)

    async def wallets_text(self, limit: int = 5) -> str:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        WalletStats.wallet_id,
                        Wallet.address,
                        WalletStats.confidence_score,
                        WalletStats.total_pnl_sol,
                        WalletStats.win_rate,
                    )
                    .join(Wallet, Wallet.id == WalletStats.wallet_id)
                    .where(WalletStats.confidence_score.is_not(None))
                    .order_by(WalletStats.confidence_score.desc())
                    .limit(limit)
                )
            ).all()
            verdicts = await self._latest_verdicts(
                session, [wallet_id for wallet_id, *_ in rows]
            )
        return format_top_wallets(
            {
                "address": address,
                "confidence_score": conf,
                "total_pnl_sol": pnl,
                "win_rate": win,
                "vetting_verdict": verdicts.get(wallet_id),
            }
            for wallet_id, address, conf, pnl, win in rows
        )

    @staticmethod
    async def _latest_verdicts(
        session: AsyncSession, wallet_ids: list[int]
    ) -> dict[int, str]:
        """Latest vetting verdict per wallet — a ranked wallet may be a ring
        member flagged AFTER it climbed the leaderboard, so the chat card must
        carry the flag too, not just the web UI."""
        if not wallet_ids:
            return {}
        latest_ts = (
            select(
                WalletVetting.wallet_id.label("wallet_id"),
                func.max(WalletVetting.ts).label("max_ts"),
            )
            .where(WalletVetting.wallet_id.in_(wallet_ids))
            .group_by(WalletVetting.wallet_id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(WalletVetting.wallet_id, WalletVetting.verdict)
                .join(
                    latest_ts,
                    (WalletVetting.wallet_id == latest_ts.c.wallet_id)
                    & (WalletVetting.ts == latest_ts.c.max_ts),
                )
                # ts ties resolve by insertion order: the dict keeps the last
                # write, i.e. the highest id.
                .order_by(WalletVetting.id)
            )
        ).all()
        return {wallet_id: verdict for wallet_id, verdict in rows}

    async def trades_text(self, limit: int = 5) -> str:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Trade.side,
                        Trade.quote_amount,
                        Trade.block_time,
                        Token.mint,
                        Wallet.address,
                    )
                    .join(Token, Token.id == Trade.token_id)
                    .join(Wallet, Wallet.id == Trade.wallet_id)
                    .order_by(Trade.block_time.desc())
                    .limit(limit)
                )
            ).all()
        out = []
        for side, quote, block_time, mint, address in rows:
            if block_time.tzinfo is None:
                block_time = block_time.replace(tzinfo=UTC)
            out.append(
                {
                    "side": side,
                    "quote_amount": quote,
                    "token_mint": mint,
                    "wallet": address,
                    "age_minutes": max((now - block_time).total_seconds() / 60.0, 0.0),
                }
            )
        return format_recent_trades(out)

    async def decisions_text(self, hours: int = 2, sample: int = 200) -> str:
        """Summarize the recent copy/skip decision log — "why aren't we
        trading". Pure DB read of ``trade_decisions``; costs zero RPC credits
        and changes nothing. Surfaces the same audit trail that was previously
        only reachable via psql on the droplet.
        """
        from datetime import UTC, datetime, timedelta

        since = datetime.now(UTC) - timedelta(hours=hours)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        TradeDecision.decision,
                        TradeDecision.reasons,
                        Wallet.address,
                    )
                    .join(
                        Wallet,
                        Wallet.id == TradeDecision.leader_wallet_id,
                        isouter=True,
                    )
                    .where(TradeDecision.created_at >= since)
                    .order_by(TradeDecision.created_at.desc())
                    .limit(sample)
                )
            ).all()
        decisions = [
            {"decision": decision, "reasons": reasons, "wallet": address}
            for decision, reasons, address in rows
        ]
        # When the row count equals the fetch limit the window likely holds
        # more; say "latest N" so the header never overstates completeness.
        return format_decisions(decisions, hours=hours, capped=len(rows) == sample)

    def _is_operator(self, chat_id: str | int | None) -> bool:
        return str(chat_id) == str(self._settings.telegram_chat_id)

    async def stop(self, chat_id: str | int | None) -> str:
        """Halt EVERYTHING — trading and all credit spend — operator only.

        Trips the emergency stop (no trades) AND engages the global RPC
        freeze (no ingestion/enrichment/analytics/execution calls, zero
        credits). This is the "make it stop" button: previously /stop only
        halted trading while the data services kept burning credits.
        """
        if not self._is_operator(chat_id):
            log.warning("telegram_stop_ignored_foreign_chat", chat_id=str(chat_id))
            return ""
        from app.services.rpc import freeze_rpc

        await self._guard.trip_emergency_stop("manual stop via Telegram")
        await freeze_rpc(self._redis, "manual /stop via Telegram")
        return (
            "\U0001f6d1 FULL STOP. Copy trading halted AND all RPC frozen — "
            "credit spend is now zero. Use /resume to bring it back."
        )

    async def resume(self, chat_id: str | int | None) -> str:
        """Clear the emergency stop AND the RPC freeze — operator only."""
        if not self._is_operator(chat_id):
            log.warning("telegram_resume_ignored_foreign_chat", chat_id=str(chat_id))
            return ""
        from app.services.rpc import unfreeze_rpc

        await self._guard.clear_emergency_stop()
        await unfreeze_rpc(self._redis)
        return "✅ Emergency stop cleared and RPC unfrozen. Data + trading may resume."

    async def freeze(self, chat_id: str | int | None) -> str:
        """Freeze all RPC (zero credits) WITHOUT touching trading state."""
        if not self._is_operator(chat_id):
            log.warning("telegram_freeze_ignored_foreign_chat", chat_id=str(chat_id))
            return ""
        from app.services.rpc import freeze_rpc

        await freeze_rpc(self._redis, "manual /freeze via Telegram")
        return (
            "\U0001f9ca RPC frozen. All credit spend is now zero "
            "(ingestion paused). Use /unfreeze to resume data collection."
        )

    async def unfreeze(self, chat_id: str | int | None) -> str:
        """Release the RPC freeze — operator only."""
        if not self._is_operator(chat_id):
            log.warning("telegram_unfreeze_ignored_foreign_chat", chat_id=str(chat_id))
            return ""
        from app.services.rpc import unfreeze_rpc

        await unfreeze_rpc(self._redis)
        return "✅ RPC unfrozen. Data collection resumes (credits will be spent again)."

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

        @dp.message(Command("health"))
        async def _health(message: Message) -> None:
            await message.answer(await self.health_text())

        @dp.message(Command("budget"))
        async def _budget(message: Message) -> None:
            # The health card carries the credit line; a dedicated command
            # keeps the common question one word long.
            await message.answer(await self.health_text())

        @dp.message(Command("wallets"))
        async def _wallets(message: Message) -> None:
            await message.answer(await self.wallets_text())

        @dp.message(Command("trades"))
        async def _trades(message: Message) -> None:
            await message.answer(await self.trades_text())

        @dp.message(Command("why", "decisions"))
        async def _why(message: Message) -> None:
            await message.answer(await self.decisions_text())

        @dp.message(Command("stop"))
        async def _stop(message: Message) -> None:
            reply = await self.stop(message.chat.id)
            if reply:
                await message.answer(reply)

        @dp.message(Command("resume"))
        async def _resume(message: Message) -> None:
            reply = await self.resume(message.chat.id)
            if reply:
                await message.answer(reply)

        @dp.message(Command("freeze"))
        async def _freeze(message: Message) -> None:
            reply = await self.freeze(message.chat.id)
            if reply:
                await message.answer(reply)

        @dp.message(Command("unfreeze"))
        async def _unfreeze(message: Message) -> None:
            reply = await self.unfreeze(message.chat.id)
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
    "/health — database, ingestion, queue, RPC budget\n"
    "/budget — RPC credit usage (same card as /health)\n"
    "/portfolio — open positions and realized PnL\n"
    "/wallets — top wallets by confidence\n"
    "/trades — most recent observed trades\n"
    "/why — why trades are/aren't being copied (recent decision log)\n"
    "/report — latest weekly report\n"
    "/stop — FULL STOP: halt trading AND freeze all RPC (zero credits)\n"
    "/resume — clear the emergency stop and unfreeze RPC\n"
    "/freeze — freeze all RPC (zero credits) without changing trading\n"
    "/unfreeze — resume data collection after a /freeze\n"
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
