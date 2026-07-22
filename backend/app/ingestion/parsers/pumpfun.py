"""Pump.fun adapter: bonding-curve trades and PumpSwap AMM trades.

One parser covers both pump.fun programs because they share trader behavior
and log conventions:

- ``PUMPFUN`` (bonding curve) settles in **native SOL**: the trader has no
  WSOL token account in the transaction, so the quote leg only shows up in
  the fee payer's lamport delta. ``util.infer_swap_events`` handles that
  fallback (fee added back) and stays the single source of truth for
  amounts and side.
- ``PUMPSWAP`` (the pump.fun AMM after graduation) settles through WSOL
  token accounts like any SPL AMM.

The ``Program log: Instruction: Buy`` / ``Instruction: Sell`` lines emitted
by both programs are used purely as a consistency signal: a mismatch against
the balance-delta-inferred side is logged, never trusted over the deltas.

``pool_address`` is read from the swap instruction's account list — the
bonding-curve state account (index 3) for PUMPFUN, the pool account
(index 0) for PUMPSWAP — and validated against the token-balance owners:
both venues keep reserves in token accounts owned by that state account, so
a genuine state/pool account always appears as an owner in
pre/postTokenBalances. If no instruction yields a validated candidate the
pool is reported as ``None``.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.parsers import util
from app.ingestion.parsers.base import BaseDexParser
from app.ingestion.programs import DEX_BY_PROGRAM, PUMPFUN, PUMPSWAP, WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

_BUY_LOG = "Program log: Instruction: Buy"
_SELL_LOG = "Program log: Instruction: Sell"

# Position of the venue state account in the swap instruction's account list:
# pump.fun buy/sell:  [global, fee_recipient, mint, bonding_curve, ...]
# PumpSwap buy/sell:  [pool, user, global_config, base_mint, quote_mint, ...]
_STATE_ACCOUNT_INDEX: dict[str, int] = {PUMPFUN: 3, PUMPSWAP: 0}
_PUMPSWAP_BASE_MINT_INDEX = 3

# If both programs appear in one transaction (e.g. graduation bundles),
# attribute the trade to the bonding curve first.
_PROGRAM_PRIORITY: tuple[str, ...] = (PUMPFUN, PUMPSWAP)


def _instruction_account_lists(tx: dict, program_id: str) -> Iterator[list[str]]:
    """Account pubkeys of every top-level or inner instruction of ``program_id``.

    Handles both jsonParsed instructions (accounts as pubkey strings) and
    raw-index encodings (accounts as integers into the account-key list).
    """
    keys = util.account_keys(tx)
    message_ixs = tx.get("transaction", {}).get("message", {}).get("instructions", []) or []
    inner_ixs = [
        ix
        for group in tx.get("meta", {}).get("innerInstructions", []) or []
        for ix in group.get("instructions", []) or []
    ]
    for ix in (*message_ixs, *inner_ixs):
        if str(ix.get("programId", "")) != program_id:
            continue
        resolved: list[str] = []
        for account in ix.get("accounts") or []:
            if isinstance(account, int):
                if 0 <= account < len(keys):
                    resolved.append(keys[account])
            else:
                resolved.append(str(account))
        if resolved:
            yield resolved


def _token_account_owners(tx: dict) -> set[str]:
    """Owners of every token account mentioned in pre/postTokenBalances."""
    meta = tx.get("meta", {})
    owners: set[str] = set()
    for entry in [*(meta.get("preTokenBalances") or []), *(meta.get("postTokenBalances") or [])]:
        owner = entry.get("owner")
        if owner:
            owners.add(str(owner))
    return owners


def _logged_sides(tx: dict) -> set[Side]:
    """Sides claimed by pump.fun/PumpSwap Anchor instruction logs, if any."""
    sides: set[Side] = set()
    for line in util.log_messages(tx):
        if line == _BUY_LOG:
            sides.add(Side.BUY)
        elif line == _SELL_LOG:
            sides.add(Side.SELL)
    return sides


class PumpFunParser(BaseDexParser):
    """Bonding curve (``Dex.PUMPFUN``) and PumpSwap AMM (``Dex.PUMPSWAP``)."""

    dex = Dex.PUMPFUN
    program_ids = frozenset({PUMPFUN, PUMPSWAP})

    def parse(self, tx: dict) -> list[SwapEvent]:
        if not util.is_success(tx):
            return []
        program_id = self._priority_program_id(tx)
        if program_id is None:
            return []
        trader = util.fee_payer(tx)
        if not trader:
            return []
        events = util.infer_swap_events(
            tx,
            trader,
            DEX_BY_PROGRAM[program_id],
            program_id=program_id,
            pool_address=self._pool_address(tx, program_id, trader),
        )
        self._check_log_consistency(tx, events)
        return events

    def _priority_program_id(self, tx: dict) -> str | None:
        """The matched pump.fun program, bonding curve winning over the AMM."""
        present = util.program_ids(tx)
        return next((pid for pid in _PROGRAM_PRIORITY if pid in present), None)

    def _pool_address(self, tx: dict, program_id: str, trader: str) -> str | None:
        """Bonding-curve state (PUMPFUN) / pool account (PUMPSWAP), else None."""
        index = _STATE_ACCOUNT_INDEX[program_id]
        owners = _token_account_owners(tx)
        for accounts in _instruction_account_lists(tx, program_id):
            if index < len(accounts):
                candidate = accounts[index]
                if candidate != trader and candidate in owners:
                    return candidate
        return None

    def _wsol_base_pool(self, tx: dict) -> bool:
        """True when a PumpSwap instruction's BASE mint is wrapped SOL.

        PumpSwap "Buy"/"Sell" logs are phrased relative to the pool's base
        asset. Most pools use TOKEN as base ("Buy" = buy the memecoin), but
        WSOL-base pools exist and invert the meaning: "Buy" there means
        buying WSOL — i.e. SELLING the memecoin. Our events are always
        phrased relative to the token, so the logged side must be flipped
        before comparison for these pools.
        """
        for accounts in _instruction_account_lists(tx, PUMPSWAP):
            if len(accounts) > _PUMPSWAP_BASE_MINT_INDEX:
                return accounts[_PUMPSWAP_BASE_MINT_INDEX] == WSOL_MINT
        return False

    def _check_log_consistency(self, tx: dict, events: list[SwapEvent]) -> None:
        """Warn when Buy/Sell logs disagree with the balance-delta side.

        Balance deltas remain the source of truth; the log lines only flag
        transactions worth a second look (bundles, self-trades, log spoofing).
        """
        logged = _logged_sides(tx)
        if not logged:
            return
        if self._wsol_base_pool(tx):
            flip = {Side.BUY: Side.SELL, Side.SELL: Side.BUY}
            logged = {flip[side] for side in logged}
        for event in events:
            if event.side not in logged:
                log.warning(
                    "pumpfun_side_log_mismatch",
                    signature=event.signature,
                    inferred_side=event.side.value,
                    logged_sides=sorted(side.value for side in logged),
                )
