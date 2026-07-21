"""Normalized event types produced by the transaction parsers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Dex(StrEnum):
    PUMPFUN = "pumpfun"
    PUMPSWAP = "pumpswap"
    RAYDIUM_AMM = "raydium_amm"
    RAYDIUM_CLMM = "raydium_clmm"
    RAYDIUM_CPMM = "raydium_cpmm"
    ORCA_WHIRLPOOL = "orca_whirlpool"
    JUPITER = "jupiter"
    UNKNOWN = "unknown"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SwapEvent(BaseModel):
    """One normalized swap leg, always expressed from the trader's viewpoint.

    ``side`` is relative to ``token_mint`` (the non-quote asset): BUY means the
    wallet's balance of ``token_mint`` increased. ``quote_amount`` is the
    amount of ``quote_mint`` (WSOL/stable, or the counter-asset in
    token-to-token swaps) that left/entered the wallet.
    """

    signature: str
    slot: int
    block_time: datetime
    wallet: str
    dex: Dex
    side: Side
    token_mint: str
    quote_mint: str
    token_amount: Decimal = Field(gt=0)
    quote_amount: Decimal = Field(ge=0)
    price_quote_per_token: Decimal | None = None
    pool_address: str | None = None
    program_id: str | None = None
    aggregator: str | None = None
    event_index: int = 0
    raw: dict | None = None
