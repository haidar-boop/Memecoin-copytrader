"""Orca Whirlpool swap parser.

Amounts, side and price come from ``util.infer_swap_events`` (balance-delta
ground truth); this adapter contributes venue attribution and best-effort
pool identification. Whirlpool is an Anchor program, so its instruction data
starts with the 8-byte method discriminator ``sha256("global:<name>")[:8]``
and the whirlpool state account sits at a fixed position in each swap's
account list:

- ``swap``:    ``[token_program, token_authority, whirlpool, ...]`` -> index 2
- ``swap_v2``: ``[token_program_a, token_program_b, memo_program,
  token_authority, whirlpool, ...]`` -> index 4
- ``two_hop_swap`` / ``two_hop_swap_v2`` route through two whirlpools in a
  single instruction, so they never yield a single-pool attribution.

A candidate pool is only reported when every decoded swap (top-level or CPI)
agrees on one address and that address also shows up as a token-account
owner in pre/postTokenBalances — whirlpool vaults are token accounts owned
by the pool PDA, so a genuine pool always appears there. Anything else gets
``pool_address=None``: wrong pool attribution is worse than none.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from app.ingestion.events import Dex, SwapEvent
from app.ingestion.parsers import util
from app.ingestion.parsers.base import BaseDexParser
from app.ingestion.programs import ORCA_WHIRLPOOL
from app.logging_config import get_logger

log = get_logger(__name__)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: value for value, char in enumerate(_B58_ALPHABET)}


def _anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# Single-pool swap instructions: discriminator -> whirlpool account position.
_SWAP_POOL_INDEX: dict[bytes, int] = {
    _anchor_discriminator("swap"): 2,
    _anchor_discriminator("swap_v2"): 4,
}

# Multi-pool swaps: recognized only so the pool stays None instead of a guess.
_TWO_HOP_DISCRIMINATORS = frozenset(
    {_anchor_discriminator("two_hop_swap"), _anchor_discriminator("two_hop_swap_v2")}
)


def _b58decode(encoded: str) -> bytes | None:
    """Minimal base58 decoder for instruction data; ``None`` on invalid input."""
    number = 0
    for char in encoded:
        digit = _B58_INDEX.get(char)
        if digit is None:
            return None
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(encoded) - len(encoded.lstrip("1"))
    return b"\x00" * pad + body


def _iter_program_instructions(tx: dict, program_id: str) -> Iterator[dict]:
    """Top-level and inner (CPI) instructions executed by ``program_id``."""
    message_ixs = tx.get("transaction", {}).get("message", {}).get("instructions", []) or []
    inner_ixs = [
        ix
        for group in tx.get("meta", {}).get("innerInstructions", []) or []
        for ix in group.get("instructions", []) or []
    ]
    for ix in (*message_ixs, *inner_ixs):
        if str(ix.get("programId", "")) == program_id:
            yield ix


def _instruction_accounts(instruction: dict, keys: list[str]) -> list[str]:
    """Account pubkeys of one instruction.

    jsonParsed encoding carries pubkey strings; legacy json carries indices
    into the transaction account keys — resolve both.
    """
    resolved: list[str] = []
    for entry in instruction.get("accounts") or []:
        if isinstance(entry, bool):  # bool is an int subclass; never an index
            resolved.append("")
        elif isinstance(entry, int):
            resolved.append(keys[entry] if 0 <= entry < len(keys) else "")
        else:
            resolved.append(str(entry))
    return resolved


def _token_account_owners(tx: dict) -> set[str]:
    """Owners of every token account mentioned in pre/postTokenBalances."""
    meta = tx.get("meta", {})
    owners: set[str] = set()
    for entry in [*(meta.get("preTokenBalances") or []), *(meta.get("postTokenBalances") or [])]:
        owner = entry.get("owner")
        if owner:
            owners.add(str(owner))
    return owners


class OrcaWhirlpoolParser(BaseDexParser):
    """Adapter for the Orca Whirlpool concentrated-liquidity program."""

    dex = Dex.ORCA_WHIRLPOOL
    program_ids = frozenset({ORCA_WHIRLPOOL})

    def parse(self, tx: dict) -> list[SwapEvent]:
        if not util.is_success(tx):
            return []
        if ORCA_WHIRLPOOL not in util.program_ids(tx):
            log.debug(
                "orca_no_instruction",
                signature=(tx.get("transaction", {}).get("signatures") or [""])[0],
            )
            return []
        wallet = util.fee_payer(tx)
        if not wallet:
            return []
        return util.infer_swap_events(
            tx,
            wallet,
            self.dex,
            program_id=ORCA_WHIRLPOOL,
            pool_address=self._pool_address(tx, wallet),
        )

    def _pool_address(self, tx: dict, wallet: str) -> str | None:
        """The whirlpool account, only when reliably identifiable.

        Decodes every whirlpool instruction in the transaction; multi-hop
        routes, disagreeing candidates and candidates not backed by a
        pool-owned vault in the token balances all resolve to ``None``.
        """
        keys = util.account_keys(tx)
        candidates: set[str] = set()
        for instruction in _iter_program_instructions(tx, ORCA_WHIRLPOOL):
            data = instruction.get("data")
            decoded = _b58decode(data) if isinstance(data, str) else None
            if decoded is None:
                continue
            discriminator = decoded[:8]
            if discriminator in _TWO_HOP_DISCRIMINATORS:
                return None  # two pools in one instruction: never attribute one
            pool_index = _SWAP_POOL_INDEX.get(discriminator)
            if pool_index is None:
                continue
            accounts = _instruction_accounts(instruction, keys)
            if pool_index < len(accounts) and accounts[pool_index]:
                candidates.add(accounts[pool_index])
        if len(candidates) != 1:
            return None
        pool = candidates.pop()
        if pool == wallet or pool not in _token_account_owners(tx):
            return None
        return pool
