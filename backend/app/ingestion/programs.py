"""Well-known Solana program IDs and mint constants.

Program IDs are configuration-grade constants: if a venue ships a new program
id, add it to ``DEX_BY_PROGRAM`` and (if it should be subscribed) the listener
picks it up via ``listen_program_ids``.
"""

from __future__ import annotations

from app.ingestion.events import Dex

# --- DEX programs ----------------------------------------------------------
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMPFUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
JUPITER_V4 = "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"

# --- system programs -------------------------------------------------------
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
METAPLEX_METADATA = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# --- mints -----------------------------------------------------------------
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

STABLE_MINTS = frozenset({USDC_MINT, USDT_MINT})
QUOTE_MINTS = frozenset({WSOL_MINT, *STABLE_MINTS})

DEX_BY_PROGRAM: dict[str, Dex] = {
    RAYDIUM_AMM_V4: Dex.RAYDIUM_AMM,
    RAYDIUM_CLMM: Dex.RAYDIUM_CLMM,
    RAYDIUM_CPMM: Dex.RAYDIUM_CPMM,
    PUMPFUN: Dex.PUMPFUN,
    PUMPSWAP: Dex.PUMPSWAP,
    ORCA_WHIRLPOOL: Dex.ORCA_WHIRLPOOL,
    JUPITER_V6: Dex.JUPITER,
    JUPITER_V4: Dex.JUPITER,
}

JUPITER_PROGRAM_IDS = frozenset({JUPITER_V6, JUPITER_V4})


def listen_program_ids(enabled_dexes: list[str]) -> dict[str, str]:
    """Map program_id -> dex value for the venues enabled in settings."""
    enabled = set(enabled_dexes)
    return {pid: dex.value for pid, dex in DEX_BY_PROGRAM.items() if dex.value in enabled}
