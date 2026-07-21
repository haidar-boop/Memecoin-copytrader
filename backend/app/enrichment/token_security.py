"""On-chain rug-signal probe: authorities, holder concentration, LP status.

Produces :class:`TokenSecuritySignals` for the risk engine
(``app.decision.token_risk``). Every lookup is best-effort: a failed step
records an error string in ``probe_errors`` and leaves the corresponding
signal ``None`` (unknown), because the engine must distinguish "could not
verify" from "verified safe" — a probe that raised would collapse both into
a hard failure.

No caching happens here: the engine layer owns TTL reuse via
``TokenRiskAssessment`` rows, and every RPC call already flows through the
budget/rate limiter inside ``SolanaRpc``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import DexPool, Token
from app.decision.risk_contracts import TokenSecuritySignals
from app.logging_config import get_logger

log = get_logger(__name__)

# Conventional Solana burn address: tokens sent here are irrecoverable, so
# LP transferred to it is as good as burned even though supply is unchanged.
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

_TOP_HOLDERS = 10


class _Rpc(Protocol):
    async def get_account_info(
        self,
        pubkey: str,
        encoding: str = "base64",
        budget_exempt: bool | None = None,
    ) -> dict | None: ...

    async def get_token_supply(
        self, mint: str, budget_exempt: bool | None = None
    ) -> dict | None: ...

    async def get_token_largest_accounts(
        self, mint: str, budget_exempt: bool | None = None
    ) -> list[dict]: ...


def _amount(entry: dict) -> int:
    """Raw integer amount from a largest-accounts / supply payload entry."""
    return int(entry.get("amount", 0))


class TokenSecurityProbe:
    """Fetches on-chain rug signals for one token per :meth:`probe` call."""

    def __init__(self, rpc: _Rpc, settings: Settings) -> None:
        self._rpc = rpc
        self._settings = settings

    async def probe(self, session: AsyncSession, token: Token) -> TokenSecuritySignals:
        signals = TokenSecuritySignals(mint=token.mint)
        pools = list(
            (await session.execute(select(DexPool).where(DexPool.token_id == token.id)))
            .scalars()
            .all()
        )
        await self._probe_authorities(session, token, signals)
        await self._probe_holders(token, pools, signals)
        await self._probe_lp(token, pools, signals)
        if signals.probe_errors:
            log.warning(
                "token_security_probe_partial", mint=token.mint, errors=signals.probe_errors
            )
        return signals

    async def _probe_authorities(
        self, session: AsyncSession, token: Token, signals: TokenSecuritySignals
    ) -> None:
        """Read mint/freeze authority and persist to the token row.

        Persisting here (add, no commit — the caller owns the transaction)
        lets the DB act as a fallback when a later probe's RPC budget is
        exhausted. Absent/None authority in the parsed mint means renounced,
        stored as "" per the contract; RPC failure leaves None (unknown).
        """
        try:
            value = await self._rpc.get_account_info(
                token.mint, encoding="jsonParsed", budget_exempt=True
            )
        except Exception as exc:  # noqa: BLE001 - degrade to unknown, never raise
            signals.probe_errors.append(f"authorities: {exc}")
            return
        info = (
            ((value or {}).get("data") or {}).get("parsed", {}).get("info")
            if isinstance((value or {}).get("data"), dict)
            else None
        )
        if info is None:
            signals.probe_errors.append("authorities: mint account missing or not jsonParsed")
            return
        signals.mint_authority = info.get("mintAuthority") or ""
        signals.freeze_authority = info.get("freezeAuthority") or ""
        token.mint_authority = signals.mint_authority
        token.freeze_authority = signals.freeze_authority
        token.security_checked_at = datetime.now(UTC)
        session.add(token)

    async def _probe_holders(
        self, token: Token, pools: list[DexPool], signals: TokenSecuritySignals
    ) -> None:
        """Top-10 holder share of supply, excluding known pool vaults.

        Pool vaults hold the tradeable side of liquidity, not a wallet's
        stash — counting them would flag every healthy pool as a whale.
        """
        try:
            largest = await self._rpc.get_token_largest_accounts(
                token.mint, budget_exempt=True
            )
            supply = await self._rpc.get_token_supply(token.mint, budget_exempt=True)
        except Exception as exc:  # noqa: BLE001
            signals.probe_errors.append(f"holders: {exc}")
            return
        if supply is None:
            signals.probe_errors.append("holders: no supply result")
            return
        supply_amount = _amount(supply)
        if supply_amount <= 0:
            signals.probe_errors.append("holders: zero supply")
            return
        vaults = {p.base_vault for p in pools} | {p.quote_vault for p in pools}
        vaults.discard(None)
        holders = [h for h in largest if h.get("address") not in vaults]
        top = sorted((_amount(h) for h in holders), reverse=True)[:_TOP_HOLDERS]
        signals.top10_holder_pct = min(1.0, max(0.0, sum(top) / supply_amount))
        signals.holder_sample_count = len(holders)

    async def _probe_lp(
        self, token: Token, pools: list[DexPool], signals: TokenSecuritySignals
    ) -> None:
        """LP existence, burned fraction, and largest LP holder share.

        Burn heuristic: an SPL ``burn`` instruction shrinks LP supply while a
        transfer to the incinerator does not, so neither path is directly
        observable from one snapshot. We approximate both with
        ``incinerator_held / (supply + incinerator_held)``: burned supply has
        already left the denominator, and incinerator-held LP is counted in
        both terms. Zero remaining supply means everything was burned -> 1.0.
        """
        if not pools:
            if token.primary_dex == "pumpfun":
                signals.is_bonding_curve = True
            return
        pool = next((p for p in pools if p.lp_mint), None)
        if pool is None or pool.lp_mint is None:
            return
        signals.lp_exists = True
        try:
            largest = await self._rpc.get_token_largest_accounts(
                pool.lp_mint, budget_exempt=True
            )
            supply = await self._rpc.get_token_supply(pool.lp_mint, budget_exempt=True)
        except Exception as exc:  # noqa: BLE001
            signals.probe_errors.append(f"lp: {exc}")
            return
        if supply is None:
            signals.probe_errors.append("lp: no supply result")
            return
        supply_amount = _amount(supply)
        incinerated = sum(_amount(h) for h in largest if h.get("address") == INCINERATOR)
        if supply_amount <= 0:
            signals.lp_burned_pct = 1.0
            return
        signals.lp_burned_pct = min(1.0, incinerated / (supply_amount + incinerated))
        non_burned = [_amount(h) for h in largest if h.get("address") != INCINERATOR]
        if non_burned:
            signals.lp_top_holder_pct = min(1.0, max(non_burned) / supply_amount)
