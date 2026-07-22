"""API response models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    address: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_tracked: bool
    label: str | None
    sol_balance_lamports: int | None
    # Latest fake-wallet vetting verdict ("clear" | "suspicious" |
    # "inconclusive"). Populated by GET /api/wallets/{address}; the list
    # endpoint leaves it None (a per-row lookup there would N+1 the page),
    # and None also means "never vetted".
    vetting_verdict: str | None = None


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mint: str
    symbol: str | None
    name: str | None
    decimals: int | None
    supply: Decimal | None
    primary_dex: str | None
    first_seen_at: datetime


class TradeOut(BaseModel):
    signature: str
    event_index: int
    block_time: datetime
    slot: int
    wallet_address: str
    token_mint: str
    dex: str
    aggregator: str | None
    side: str
    token_amount: Decimal
    quote_amount: Decimal
    quote_mint: str
    price_quote: Decimal | None
    price_usd: Decimal | None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    wallet_id: int
    token_id: int
    status: str
    opened_at: datetime
    closed_at: datetime | None
    bought_sol: Decimal
    sold_sol: Decimal
    remaining_tokens: Decimal
    realized_pnl_sol: Decimal
    roi: Decimal | None
    hold_time_seconds: int | None
    trade_count: int


class TokenSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token_id: int
    ts: datetime
    price_sol: Decimal | None
    price_usd: Decimal | None
    market_cap_usd: Decimal | None
    liquidity_sol: Decimal | None
    volume_sol_5m: Decimal | None
    volume_sol_1h: Decimal | None
    volume_sol_24h: Decimal | None
    trades_5m: int | None
    trades_1h: int | None
    holder_count: int | None


class Page(BaseModel):
    limit: int
    offset: int
    total: int | None = None
