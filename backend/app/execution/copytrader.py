"""Copy-trading worker loop: consume the live trade feed, evaluate, execute.

Subscribes to the ``events:trades`` Redis pub/sub channel published by the
ingest writer. Leader BUYS are evaluated (every evaluation persisted); SELLS
mirror-close any open copy position for that token. Nothing here runs unless
``copy_enabled`` is on — but even disabled, running the worker in shadow is
useful: decisions are still recorded (all skipping on the master-switch
gate), building the evidence base Phase 4 grades.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.decision.evaluator import Evaluation, Evaluator, LeaderBuy
from app.decision.safety import SafetyGuard
from app.decision.token_risk import TokenRiskEngine
from app.enrichment.token_security import TokenSecurityProbe
from app.execution.executor import CopyExecutor
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger
from app.services.notifications import Notification, NotificationService
from app.services.redis import TRADES_CHANNEL
from app.services.rpc import SolanaRpc

log = get_logger(__name__)


def parse_trade_message(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("quote_mint") != WSOL_MINT:
        return None  # position math is SOL-quoted; skip stable-quoted legs
    try:
        payload["quote_amount"] = Decimal(str(payload["quote_amount"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    if payload.get("side") not in ("buy", "sell"):
        return None
    if not payload.get("wallet") or not payload.get("token_mint"):
        return None
    return payload


class CopyTrader:
    def __init__(
        self,
        settings: Settings,
        redis: Any,
        rpc: SolanaRpc,
        session_factory: async_sessionmaker[AsyncSession],
    ):
        self._settings = settings
        self._redis = redis
        self._session_factory = session_factory
        self._guard = SafetyGuard(settings, redis)
        self._notifier = NotificationService(redis)

        # Pre-copy rug assessment: probe fetches on-chain facts through the
        # budgeted RPC client; the engine scores and persists. Wired as a
        # closure so the Evaluator stays free of RPC concerns.
        probe = TokenSecurityProbe(rpc, settings)
        engine = TokenRiskEngine(settings, redis)

        async def _assess(session: AsyncSession, token) -> Any:
            return await engine.assess(
                session, token, lambda: probe.probe(session, token)
            )

        self._evaluator = Evaluator(settings, redis, self._guard, risk_assessor=_assess)
        self._executor = CopyExecutor(settings, redis, rpc, self._guard)

    async def run(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(TRADES_CHANNEL)
        log.info("copytrader_started", mode=self._settings.copy_mode,
                 enabled=self._settings.copy_enabled)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await self.handle_message(message.get("data", ""))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("copytrader_message_error")
        finally:
            await pubsub.unsubscribe(TRADES_CHANNEL)

    async def handle_message(self, raw: str) -> Evaluation | None:
        payload = parse_trade_message(raw)
        if payload is None:
            return None

        if payload["side"] == "sell":
            # Exit runs in its own transaction; it holds RPC I/O, so it must
            # not share a transaction with anything else.
            async with self._session_factory() as session:
                async with session.begin():
                    seller_id = await self._resolve_wallet_id(session, payload["wallet"])
                    await self._executor.mirror_sell(
                        session, payload["token_mint"], seller_id
                    )
            return None

        event = LeaderBuy(
            signature=str(payload.get("signature", "")),
            wallet_address=str(payload["wallet"]),
            token_mint=str(payload["token_mint"]),
            quote_amount_sol=payload["quote_amount"],
            block_time=_parse_time(payload.get("block_time")),
        )
        # Persist the evaluation in its OWN transaction and commit it before
        # touching the chain, so the every-evaluation-recorded guarantee holds
        # even if execution later raises. Execution then runs in a separate
        # transaction and never holds a DB transaction open across network I/O.
        async with self._session_factory() as session:
            async with session.begin():
                evaluation = await self._evaluator.evaluate_buy(session, event)
        if evaluation.decision != "copy":
            await self._notify_if_rug_blocked(event, evaluation)
            return evaluation
        async with self._session_factory() as session:
            async with session.begin():
                await self._executor.execute_buy(session, event, evaluation)
        return evaluation

    async def _notify_if_rug_blocked(
        self, event: LeaderBuy, evaluation: Evaluation
    ) -> None:
        """Surface skips caused specifically by the rug gate.

        Only fires when every OTHER gate passed — a rug warning on a trade
        that was going to be skipped anyway (copy disabled, blacklist...)
        would be pure noise.
        """
        rug_failed = False
        others_passed = True
        for reason in evaluation.reasons:
            failed = not reason.get("passed", True)
            if reason.get("gate") == "token_rug_risk":
                rug_failed = failed
            elif failed:
                others_passed = False
        if not (rug_failed and others_passed):
            return
        blocked = [
            str(r)
            for factor in evaluation.factors or []
            if factor.get("factor") == "token_rug_risk"
            for r in (factor.get("note") or {}).get("blocked_reasons", [])
        ]
        rug_score = next(
            (
                float(f.get("value") or 0.0)
                for f in evaluation.factors or []
                if f.get("factor") == "token_rug_risk"
            ),
            0.0,
        )
        try:
            await self._notifier.emit(
                Notification.risk_blocked(event.token_mint, rug_score, blocked)
            )
        except Exception:  # noqa: BLE001 - notification failure must not break the loop
            log.exception("risk_blocked_notify_failed")

    async def _resolve_wallet_id(self, session, address: str) -> int | None:
        from sqlalchemy import select

        from app.db.models import Wallet

        return (
            await session.execute(select(Wallet.id).where(Wallet.address == address))
        ).scalar_one_or_none()


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(tz=UTC)
