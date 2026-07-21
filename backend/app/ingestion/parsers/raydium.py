"""Raydium swap parser covering AMM v4, CPMM and CLMM.

All three programs move funds through SPL token accounts, so amounts, side
and price come from ``util.infer_swap_events`` (balance-delta ground truth).
This adapter contributes venue attribution — which Raydium program actually
executed — and best-effort pool identification by decoding the matched swap
instruction (top-level or inner):

- AMM v4 is a native program: instruction data starts with a one-byte tag
  (9 = swapBaseIn, 11 = swapBaseOut) and the amm/pool state is the second
  account, right after the token program.
- CPMM and CLMM are Anchor programs: data starts with the 8-byte method
  discriminator ``sha256("global:<name>")[:8]`` and the pool state sits at a
  fixed position in the swap account list (CPMM index 3, CLMM index 2).

If no Raydium instruction decodes as a known swap, or several decoded swaps
disagree on the pool, ``pool_address`` stays ``None``: wrong pool
attribution is worse than none. CLMM ``swap_router_base_in`` is deliberately
not decoded for the same reason — it hops multiple pools in one instruction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from app.ingestion.events import Dex, SwapEvent
from app.ingestion.parsers import util
from app.ingestion.parsers.base import BaseDexParser
from app.ingestion.programs import (
    DEX_BY_PROGRAM,
    RAYDIUM_AMM_V4,
    RAYDIUM_CLMM,
    RAYDIUM_CPMM,
)
from app.logging_config import get_logger

log = get_logger(__name__)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: value for value, char in enumerate(_B58_ALPHABET)}

# AMM v4 native-program instruction tags that are swaps.
_AMM_V4_SWAP_TAGS = frozenset({9, 11})  # swapBaseIn, swapBaseOut


def _anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


_SWAP_DISCRIMINATORS: dict[str, frozenset[bytes]] = {
    RAYDIUM_CPMM: frozenset(
        {_anchor_discriminator("swap_base_input"), _anchor_discriminator("swap_base_output")}
    ),
    RAYDIUM_CLMM: frozenset({_anchor_discriminator("swap"), _anchor_discriminator("swap_v2")}),
}

# Position of the pool/amm state account in each program's swap account list.
_POOL_ACCOUNT_INDEX: dict[str, int] = {
    RAYDIUM_AMM_V4: 1,
    RAYDIUM_CPMM: 3,
    RAYDIUM_CLMM: 2,
}


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


def _iter_instructions(tx: dict) -> Iterator[dict]:
    """Yield top-level and inner instructions in execution order."""
    message = tx.get("transaction", {}).get("message", {})
    top_level: list[dict] = list(message.get("instructions", []) or [])
    inner: dict[int, list[dict]] = {}
    for group in tx.get("meta", {}).get("innerInstructions", []) or []:
        index = group.get("index")
        key = index if isinstance(index, int) else -1
        inner.setdefault(key, []).extend(group.get("instructions", []) or [])
    for i, instruction in enumerate(top_level):
        yield instruction
        yield from inner.pop(i, [])
    for index in sorted(inner):  # malformed/orphan groups, defensively last
        yield from inner[index]


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


def _decode_swap(instruction: dict, keys: list[str]) -> tuple[str, str | None] | None:
    """Return ``(program_id, pool_address)`` if this is a known Raydium swap."""
    program_id = str(instruction.get("programId") or "")
    if program_id not in _POOL_ACCOUNT_INDEX:
        return None
    data = instruction.get("data")
    decoded = _b58decode(data) if isinstance(data, str) else None
    if decoded is None:
        return None
    if program_id == RAYDIUM_AMM_V4:
        is_swap = len(decoded) >= 1 and decoded[0] in _AMM_V4_SWAP_TAGS
    else:
        is_swap = decoded[:8] in _SWAP_DISCRIMINATORS[program_id]
    if not is_swap:
        return None
    accounts = _instruction_accounts(instruction, keys)
    pool_index = _POOL_ACCOUNT_INDEX[program_id]
    pool = accounts[pool_index] if len(accounts) > pool_index else None
    return program_id, pool or None


class RaydiumParser(BaseDexParser):
    """Adapter for the three Raydium swap programs.

    The ``dex`` and ``program_id`` on emitted events always reflect the
    Raydium program that actually appeared in the transaction (AMM v4,
    CPMM or CLMM); the class-level ``dex`` is only a registry label.
    """

    dex = Dex.RAYDIUM_AMM
    program_ids = frozenset({RAYDIUM_AMM_V4, RAYDIUM_CPMM, RAYDIUM_CLMM})

    def parse(self, tx: dict) -> list[SwapEvent]:
        keys = util.account_keys(tx)
        first_program: str | None = None
        swaps: list[tuple[str, str | None]] = []
        for instruction in _iter_instructions(tx):
            program_id = str(instruction.get("programId") or "")
            if program_id not in self.program_ids:
                continue
            if first_program is None:
                first_program = program_id
            decoded = _decode_swap(instruction, keys)
            if decoded is not None:
                swaps.append(decoded)

        if first_program is None:
            log.debug(
                "raydium_no_instruction",
                signature=(tx.get("transaction", {}).get("signatures") or [""])[0],
            )
            return []

        # Attribute to the first decoded swap's program; if nothing decoded
        # as a swap, fall back to the first Raydium program invoked.
        program_id = swaps[0][0] if swaps else first_program

        # Pool only when it is unambiguous: every decoded swap agrees on one
        # program and one pool. Multi-hop routes get None, never a guess.
        pool_address: str | None = None
        if swaps:
            programs = {pid for pid, _ in swaps}
            pools = {pool for _, pool in swaps}
            if len(programs) == 1 and len(pools) == 1:
                pool_address = swaps[0][1]

        wallet = util.fee_payer(tx)
        if not wallet:
            return []
        return util.infer_swap_events(
            tx,
            wallet,
            DEX_BY_PROGRAM[program_id],
            program_id=program_id,
            pool_address=pool_address,
        )
