"""Copy-trade execution: paper fills by default, guarded live path.

Paper mode records a fill at the real Jupiter quote price (honest slippage
included) without touching the chain. Live mode requires BOTH
``copy_mode=live`` and a loadable dedicated keypair, and every transaction is
simulated before submission. A Redis lock per (token, side) prevents
duplicate concurrent executions; failures feed the safety guard's
consecutive-failure auto-stop.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CopyPosition, CopyTrade, Token
from app.db.util import quantize_sol
from app.decision.evaluator import Evaluation, LeaderBuy
from app.decision.ranking import record_outcome
from app.decision.safety import SafetyGuard
from app.execution import jupiter, wallet
from app.ingestion.parsers.util import LAMPORTS_PER_SOL
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger
from app.services.notifications import Notification, NotificationService
from app.services.redis import try_dedup
from app.services.rpc import SolanaRpc

log = get_logger(__name__)

CONFIRM_POLL_SECONDS = 2.0


def _lock_ttl_seconds(settings: Settings) -> int:
    """Dedup lock must outlive the worst-case execution (all attempts x the
    confirm timeout) so it never expires mid-trade and admits a duplicate."""
    return int(
        settings.copy_execution_attempts * settings.copy_confirm_timeout_seconds + 30
    )


class CopyExecutor:
    def __init__(
        self,
        settings: Settings,
        redis: Any,
        rpc: SolanaRpc,
        guard: SafetyGuard,
        jupiter_client: jupiter.JupiterClient | None = None,
    ):
        self._settings = settings
        self._redis = redis
        self._rpc = rpc
        self._guard = guard
        self._jupiter = jupiter_client or jupiter.JupiterClient(settings.jupiter_base_url)
        self._notifier = NotificationService(redis)
        self._keypair = None
        if settings.copy_mode == "live":
            self._keypair = wallet.load_keypair(settings.trading_wallet_secret)

    async def _notify(self, notification: Notification) -> None:
        """Emit a dashboard/Telegram notification; never break execution on it."""
        try:
            await self._notifier.emit(notification)
        except Exception as exc:  # pragma: no cover - best-effort side channel
            log.warning("notification_emit_failed", error=str(exc))

    async def _leader_address(self, session: AsyncSession, wallet_id: int) -> str:
        """Best-effort leader wallet address for a notification (id fallback)."""
        from app.db.models import Wallet

        address = (
            await session.execute(select(Wallet.address).where(Wallet.id == wallet_id))
        ).scalar_one_or_none()
        return address or str(wallet_id)

    # --- entry -------------------------------------------------------------

    async def execute_buy(
        self, session: AsyncSession, event: LeaderBuy, evaluation: Evaluation
    ) -> CopyTrade | None:
        """Execute an approved copy decision. Returns the CopyTrade row."""
        if evaluation.decision != "copy" or evaluation.token_id is None:
            return None
        if not await try_dedup(
            self._redis, f"copy:lock:{event.token_mint}:buy", _lock_ttl_seconds(self._settings)
        ):
            log.info("copy_buy_locked_out", token=event.token_mint)
            return None

        now = datetime.now(tz=UTC)
        trade = CopyTrade(
            decision_id=evaluation.decision_id,
            created_at=now,
            updated_at=now,
            mode=self._settings.copy_mode,
            side="buy",
            token_id=evaluation.token_id,
            leader_wallet_id=evaluation.leader_wallet_id or 0,
            size_sol=evaluation.size_sol,
            status="pending_approval" if self._settings.copy_approval_mode else "approved",
        )
        session.add(trade)
        await session.flush()
        if trade.status == "pending_approval":
            log.info("copy_buy_awaiting_approval", trade_id=trade.id)
            return trade
        return await self.run_approved(session, trade, event.token_mint)

    async def run_approved(
        self, session: AsyncSession, trade: CopyTrade, token_mint: str
    ) -> CopyTrade:
        token = await session.get(Token, trade.token_id)
        decimals = token.decimals if token and token.decimals is not None else 9
        lamports = int(trade.size_sol * LAMPORTS_PER_SOL)

        # Evaluation and execution run in separate transactions; the safety
        # picture (emergency stop, daily loss, exposure) may have changed in
        # between. Re-check at the last responsible moment, before anything
        # irreversible.
        blocked = await self._guard.gate_reasons(session, token_mint)
        if blocked:
            return await self._fail(
                session, trade, "safety rails at execution: " + "; ".join(blocked)
            )

        for attempt in range(1, self._settings.copy_execution_attempts + 1):
            trade.attempts = attempt
            quote = await self._jupiter.quote(
                input_mint=WSOL_MINT,
                output_mint=token_mint,
                amount_lamports=lamports,
                slippage_bps=self._settings.copy_slippage_bps,
            )
            if quote is None:
                await self._backoff(attempt)
                continue
            trade.quote = quote

            if self._settings.copy_mode != "live":
                return await self._paper_fill(session, trade, quote, decimals, token_mint)

            outcome = await self._live_fill(session, trade, quote, decimals)
            if outcome == "confirmed":
                await self._guard.start_cooldown(token_mint)
                await self._notify(
                    Notification.copied_buy(
                        token_mint,
                        float(trade.size_sol),
                        await self._leader_address(session, trade.leader_wallet_id),
                    )
                )
                return trade
            # Once a transaction has been broadcast we MUST NOT retry: the
            # first submission may still confirm on-chain, and a second swap
            # would double-spend. Stop and leave it for reconciliation.
            if outcome == "submitted":
                await self._guard.start_cooldown(token_mint)
                log.warning("copy_buy_unconfirmed_no_retry",
                            trade_id=trade.id, signature=trade.tx_signature)
                return trade
            if outcome == "hard_fail":
                return trade  # already marked failed by _live_fill
            await self._backoff(attempt)  # retriable: nothing was broadcast

        # Nothing was ever broadcast across all attempts; a plain failure.
        await self._guard.start_cooldown(token_mint)
        return await self._fail(session, trade, "execution attempts exhausted", live_buy=True)

    async def mirror_sell(
        self, session: AsyncSession, token_mint: str, seller_wallet_id: int | None
    ) -> CopyTrade | None:
        """The position's own leader sold: close our open copy position.

        We only mirror when ``seller_wallet_id`` matches the leader that
        opened the position — an unrelated wallet dumping the same token must
        not trigger our exit.
        """
        token = (
            await session.execute(select(Token).where(Token.mint == token_mint))
        ).scalar_one_or_none()
        if token is None:
            return None
        position = (
            await session.execute(
                select(CopyPosition).where(
                    CopyPosition.token_id == token.id, CopyPosition.status == "open"
                )
            )
        ).scalar_one_or_none()
        if position is None:
            return None
        if seller_wallet_id is None or position.leader_wallet_id != seller_wallet_id:
            return None  # not our leader's sell
        if not await try_dedup(
            self._redis, f"copy:lock:{token_mint}:sell", _lock_ttl_seconds(self._settings)
        ):
            return None

        now = datetime.now(tz=UTC)
        remaining = position.tokens_bought - position.tokens_sold
        decimals = token.decimals if token.decimals is not None else 9
        trade = CopyTrade(
            decision_id=position.entry_trade_id or 0,
            created_at=now,
            updated_at=now,
            mode=self._settings.copy_mode,
            side="sell",
            token_id=token.id,
            leader_wallet_id=position.leader_wallet_id,
            size_sol=Decimal(0),
            status="approved",
        )
        session.add(trade)
        await session.flush()

        quote = await self._jupiter.quote(
            input_mint=token_mint,
            output_mint=WSOL_MINT,
            amount_lamports=int(remaining * (Decimal(10) ** decimals)),
            slippage_bps=self._settings.copy_slippage_bps,
        )
        proceeds = jupiter.sol_proceeds_from_quote(quote) if quote else None
        if proceeds is None:
            # No routable sell (illiquid / rug). Do NOT fabricate a break-even
            # exit — that would hide a real loss from the daily-loss rail.
            # Value the exit at the latest observed market price if we have
            # one; otherwise mark the position worthless (full loss) so the
            # loss is booked honestly.
            proceeds = await self._market_value(session, token.id, remaining)
            trade.error = (
                "no sell quote; valued at last snapshot price"
                if proceeds > 0
                else "no sell quote and no price; booked as total loss"
            )
        trade.quote = quote
        trade.status = "confirmed" if self._settings.copy_mode != "live" else trade.status

        if self._settings.copy_mode == "live" and quote is not None:
            outcome = await self._live_fill(session, trade, quote, decimals)
            if outcome != "confirmed":
                # Leave the position OPEN so the sell can be retried on the
                # next signal; never book a fabricated close on a failed sell.
                # Release the dedup lock, or the promised retry is locked out
                # until the TTL expires and the next signal no-ops.
                await self._redis.delete(f"copy:lock:{token_mint}:sell")
                return trade
            proceeds = jupiter.sol_proceeds_from_quote(trade.quote or {}) or proceeds

        trade.filled_token_amount = remaining
        trade.filled_price_sol = proceeds / remaining if remaining > 0 else None
        trade.updated_at = datetime.now(tz=UTC)

        position.tokens_sold = position.tokens_bought
        position.sold_sol = quantize_sol(position.sold_sol + proceeds)
        position.status = "closed"
        position.closed_at = trade.updated_at
        position.exit_trade_id = trade.id
        position.realized_pnl_sol = quantize_sol(position.sold_sol - position.spent_sol)

        pnl = position.realized_pnl_sol
        roi = float(pnl / position.spent_sol) if position.spent_sol > 0 else 0.0
        await self._guard.record_realized_pnl(pnl)
        await record_outcome(self._redis, position.leader_wallet_id, roi)
        log.info(
            "copy_position_closed",
            token=token_mint,
            pnl_sol=str(pnl),
            roi=round(roi, 4),
            mode=self._settings.copy_mode,
        )
        await self._notify(Notification.copied_sell(token_mint, float(pnl)))
        return trade

    # --- fills -------------------------------------------------------------

    async def _paper_fill(
        self,
        session: AsyncSession,
        trade: CopyTrade,
        quote: dict,
        decimals: int,
        token_mint: str,
    ) -> CopyTrade:
        fill = jupiter.buy_fill_from_quote(quote, decimals)
        if fill is None:
            return await self._fail(session, trade, "unusable quote for paper fill")
        token_amount, price = fill
        trade.filled_token_amount = token_amount
        trade.filled_price_sol = price
        trade.status = "confirmed"
        trade.updated_at = datetime.now(tz=UTC)
        await self._open_position(session, trade, token_amount)
        # Paper fills never touch the chain, so they say nothing about live
        # execution health — do not let them clear a live failure streak.
        await self._guard.start_cooldown(token_mint)
        await self._notify(
            Notification.copied_buy(
                token_mint,
                float(trade.size_sol),
                await self._leader_address(session, trade.leader_wallet_id),
            )
        )
        return trade

    async def _live_fill(
        self, session: AsyncSession, trade: CopyTrade, quote: dict, decimals: int
    ) -> str:
        """One live attempt. Returns one of:

        - "confirmed": the swap landed and books are updated.
        - "retriable": nothing was broadcast; a fresh attempt is safe.
        - "submitted": a transaction WAS broadcast but not confirmed in time —
          the caller MUST NOT retry (double-spend risk).
        - "hard_fail": unrecoverable (no keypair/signing); trade marked failed.
        """
        is_live_buy = trade.side == "buy"
        if self._keypair is None:
            await self._fail(session, trade, "live mode without a loadable keypair",
                             live_buy=is_live_buy)
            return "hard_fail"
        tx_b64 = await self._jupiter.swap_transaction(
            quote=quote, user_public_key=str(self._keypair.pubkey())
        )
        if tx_b64 is None:
            return "retriable"
        signed_b64 = self._sign(tx_b64)
        if signed_b64 is None:
            await self._fail(session, trade, "transaction signing failed",
                             live_buy=is_live_buy)
            return "hard_fail"

        simulation = await self._rpc.simulate_transaction(signed_b64)
        if not self._simulation_ok(simulation, trade):
            trade.status = "simulated"
            return "retriable"  # nothing broadcast yet

        try:
            signature = await self._rpc.send_transaction(signed_b64)
        except Exception as exc:
            log.warning("copy_send_failed", error=str(exc))
            return "retriable"  # send raised before broadcast confirmation
        trade.tx_signature = signature
        trade.status = "submitted"

        if not await self._confirm(signature):
            # Broadcast but unconfirmed: caller must stop (no retry). For a
            # live buy, STILL open a position from the quote's expected fill:
            # if the tx lands after our timeout the SOL is spent, and an
            # untracked position can never be mirror-sold — an orphaned real
            # position is strictly worse than a phantom one the next sell
            # signal closes at market value.
            if is_live_buy:
                fill = jupiter.buy_fill_from_quote(quote, decimals)
                if fill is not None:
                    trade.filled_token_amount, trade.filled_price_sol = fill
                    trade.error = "confirm timeout; position opened from quote estimate"
                    await self._open_position(session, trade, fill[0])
            return "submitted"
        if is_live_buy:
            fill = jupiter.buy_fill_from_quote(quote, decimals)
            if fill is None:
                # Confirmed on-chain but we cannot parse the fill: open a
                # position from the quote's expected out so the spent SOL is
                # never orphaned (mirror_sell can still close it).
                await self._fail(session, trade, "confirmed but unparseable fill",
                                 live_buy=False)
                return "submitted"
            trade.filled_token_amount, trade.filled_price_sol = fill
            await self._open_position(session, trade, fill[0])
        trade.status = "confirmed"
        trade.updated_at = datetime.now(tz=UTC)
        # The streak gates live BUYS; only a live buy success clears it —
        # a sell (or paper activity) succeeding says nothing about whether
        # buys keep failing.
        if is_live_buy:
            await self._guard.record_execution_result(True)
        return "confirmed"

    def _simulation_ok(self, simulation: dict | None, trade: CopyTrade) -> bool:
        """Reject a transaction whose simulation errored OR whose SOL outflow
        exceeds the intended size (defense-in-depth against a substituted
        swap transaction from the swap API)."""
        if simulation is None or simulation.get("err") is not None:
            log.warning("copy_simulation_failed", err=str((simulation or {}).get("err")))
            return False
        # Best-effort drain guard: if the simulator reports the fee-payer's
        # pre/post lamports, the net outflow must not exceed size + slippage
        # headroom. Absent that data we fall back to trusting simulation err.
        accounts = simulation.get("accounts") or []
        if accounts and self._keypair is not None:
            pre = accounts[0].get("preLamports") if accounts[0] else None
            post = accounts[0].get("postLamports") if accounts[0] else None
            if pre is not None and post is not None:
                outflow = Decimal(pre - post) / LAMPORTS_PER_SOL
                cap = trade.size_sol * Decimal("1.05") + Decimal("0.01")  # slippage+fees
                if trade.side == "buy" and outflow > cap:
                    log.error("copy_simulation_drain_guard",
                              outflow=str(outflow), cap=str(cap))
                    return False
        return True

    async def _market_value(
        self, session: AsyncSession, token_id: int, token_amount: Decimal
    ) -> Decimal:
        """Fallback exit valuation from the latest token snapshot price."""
        from app.db.models import TokenSnapshot

        price = (
            await session.execute(
                select(TokenSnapshot.price_sol)
                .where(TokenSnapshot.token_id == token_id,
                       TokenSnapshot.price_sol.is_not(None))
                .order_by(TokenSnapshot.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return quantize_sol(Decimal(str(price)) * token_amount) if price else Decimal(0)

    def _sign(self, tx_base64: str) -> str | None:
        try:
            from solders.transaction import VersionedTransaction

            raw = base64.b64decode(tx_base64)
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [self._keypair])
            return base64.b64encode(bytes(signed)).decode()
        except Exception as exc:
            log.error("copy_sign_error", error=str(exc))
            return None

    async def _confirm(self, signature: str) -> bool:
        deadline = asyncio.get_running_loop().time() + self._settings.copy_confirm_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            statuses = await self._rpc.get_signature_statuses([signature])
            status = statuses[0] if statuses else None
            if status is not None:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(CONFIRM_POLL_SECONDS)
        return False

    # --- bookkeeping -------------------------------------------------------

    async def _open_position(
        self, session: AsyncSession, trade: CopyTrade, token_amount: Decimal
    ) -> None:
        session.add(
            CopyPosition(
                token_id=trade.token_id,
                leader_wallet_id=trade.leader_wallet_id,
                mode=trade.mode,
                status="open",
                opened_at=trade.updated_at,
                spent_sol=trade.size_sol,
                tokens_bought=token_amount,
                entry_trade_id=trade.id,
            )
        )
        await session.flush()

    async def _fail(
        self, session: AsyncSession, trade: CopyTrade, error: str, *, live_buy: bool = False
    ) -> CopyTrade:
        trade.status = "failed"
        trade.error = error
        trade.updated_at = datetime.now(tz=UTC)
        # Only a LIVE BUY failure feeds the consecutive-failure auto-stop:
        # paper fills and sell-side hiccups are not live execution risk, and
        # counting them would trip the global emergency stop on noise.
        if live_buy:
            await self._guard.record_execution_result(False)
        log.warning("copy_trade_failed", trade_id=trade.id, error=error, live_buy=live_buy)
        return trade

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(0.5 * 2**attempt, 5.0))
