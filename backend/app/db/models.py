"""SQLAlchemy models for the Phase 1 learning database.

Design notes
------------
- Append-only philosophy: the high-volume event tables (``transactions``,
  ``trades``, ``failed_transactions``) and every ``*_snapshots`` table are
  TimescaleDB hypertables partitioned on their time column (created in Alembic
  revision 0001). Rows in those tables are never updated once written — new
  facts become new rows, so history is preserved as training data.
- TimescaleDB does not allow foreign keys *referencing* hypertables, so trades
  carry the transaction signature without a FK constraint.
- Primary keys on hypertables must include the partition (time) column, hence
  the composite PKs below. Signature-level dedup uses those composite keys via
  ON CONFLICT DO NOTHING.
- JSON columns are JSONB on PostgreSQL and plain JSON elsewhere; the unit test
  suite runs on SQLite.
- Monetary/token amounts use Numeric(40, 18); lamports use BIGINT.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base

# SQLite needs plain INTEGER for rowid autoincrement; PostgreSQL gets BIGINT.
PKBigInt = BigInteger().with_variant(Integer(), "sqlite")
JSONVariant = JSON().with_variant(JSONB(), "postgresql")
Amount = Numeric(40, 18)
TZDateTime = DateTime(timezone=True)

UTC_NOW = text("CURRENT_TIMESTAMP")


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime)
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    label: Mapped[str | None] = mapped_column(String(128))
    sol_balance_lamports: Mapped[int | None] = mapped_column(BigInteger)
    balance_updated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (Index("ix_wallets_last_seen_at", "last_seen_at"),)


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    decimals: Mapped[int | None] = mapped_column(Integer)
    supply: Mapped[Decimal | None] = mapped_column(Amount)
    creator: Mapped[str | None] = mapped_column(String(64))
    metadata_uri: Mapped[str | None] = mapped_column(Text)
    primary_dex: Mapped[str | None] = mapped_column(String(32))
    # Earliest activity we have observed for the token — proxy for token age.
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    metadata_updated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class DexPool(Base):
    __tablename__ = "dex_pools"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dex: Mapped[str] = mapped_column(String(32))
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    base_mint: Mapped[str] = mapped_column(String(64))
    quote_mint: Mapped[str] = mapped_column(String(64))
    lp_mint: Mapped[str | None] = mapped_column(String(64))
    base_vault: Mapped[str | None] = mapped_column(String(64))
    quote_vault: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class Transaction(Base):
    """One row per observed on-chain transaction touching a watched program."""

    __tablename__ = "transactions"

    signature: Mapped[str] = mapped_column(String(96), primary_key=True)
    block_time: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger)
    wallet_id: Mapped[int | None] = mapped_column(BigInteger)
    fee_lamports: Mapped[int | None] = mapped_column(BigInteger)
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    program_ids: Mapped[list | None] = mapped_column(JSONVariant)
    raw: Mapped[dict | None] = mapped_column(JSONVariant)
    ingested_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_transactions_wallet_time", "wallet_id", "block_time"),
        Index("ix_transactions_block_time", "block_time"),
    )


class Trade(Base):
    """A normalized swap event. The atom of all downstream learning."""

    __tablename__ = "trades"

    signature: Mapped[str] = mapped_column(String(96), primary_key=True)
    event_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_time: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger)
    wallet_id: Mapped[int] = mapped_column(BigInteger)
    token_id: Mapped[int] = mapped_column(BigInteger)
    pool_id: Mapped[int | None] = mapped_column(BigInteger)
    dex: Mapped[str] = mapped_column(String(32))
    aggregator: Mapped[str | None] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    token_amount: Mapped[Decimal] = mapped_column(Amount)
    quote_amount: Mapped[Decimal] = mapped_column(Amount)
    quote_mint: Mapped[str] = mapped_column(String(64))
    price_quote: Mapped[Decimal | None] = mapped_column(Amount)  # quote per token
    price_usd: Mapped[Decimal | None] = mapped_column(Amount)
    sol_price_usd: Mapped[Decimal | None] = mapped_column(Amount)
    slippage_bps: Mapped[int | None] = mapped_column(Integer)
    program_id: Mapped[str | None] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_trades_wallet_time", "wallet_id", "block_time"),
        Index("ix_trades_token_time", "token_id", "block_time"),
        Index("ix_trades_dex_time", "dex", "block_time"),
        Index("ix_trades_block_time", "block_time"),
    )


class FailedTransaction(Base):
    """Failed swaps are signal too (congestion, MEV, slippage-outs)."""

    __tablename__ = "failed_transactions"

    signature: Mapped[str] = mapped_column(String(96), primary_key=True)
    block_time: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    slot: Mapped[int] = mapped_column(BigInteger)
    wallet_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    program_ids: Mapped[list | None] = mapped_column(JSONVariant)
    fee_lamports: Mapped[int | None] = mapped_column(BigInteger)
    ingested_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_failed_transactions_wallet_time", "wallet_id", "block_time"),
        Index("ix_failed_transactions_block_time", "block_time"),
    )


class Position(Base):
    """Derived open/closed position per (wallet, token) episode.

    Positions are operational state derived from trades (the trades themselves
    remain the immutable source of truth). A wallet that fully exits and later
    re-enters gets a new position row. PNL fields are in SOL terms.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    opened_at: Mapped[datetime] = mapped_column(TZDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    bought_tokens: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    sold_tokens: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    bought_sol: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    sold_sol: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    remaining_tokens: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    avg_entry_price: Mapped[Decimal | None] = mapped_column(Amount)
    avg_exit_price: Mapped[Decimal | None] = mapped_column(Amount)
    realized_pnl_sol: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    roi: Mapped[Decimal | None] = mapped_column(Amount)
    hold_time_seconds: Mapped[int | None] = mapped_column(BigInteger)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    last_trade_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_positions_wallet_status", "wallet_id", "status"),
        Index("ix_positions_token_status", "token_id", "status"),
        # At most one open episode per (wallet, token): concurrent writers
        # racing on the same pair fail fast and retry instead of forking the
        # position history.
        Index(
            "uq_positions_open_wallet_token",
            "wallet_id",
            "token_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )


class TokenSnapshot(Base):
    """Periodic market-state snapshot per active token. Append-only."""

    __tablename__ = "token_snapshots"

    token_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    price_sol: Mapped[Decimal | None] = mapped_column(Amount)
    price_usd: Mapped[Decimal | None] = mapped_column(Amount)
    vwap_sol_5m: Mapped[Decimal | None] = mapped_column(Amount)
    market_cap_usd: Mapped[Decimal | None] = mapped_column(Amount)
    liquidity_sol: Mapped[Decimal | None] = mapped_column(Amount)
    volume_sol_5m: Mapped[Decimal | None] = mapped_column(Amount)
    volume_sol_1h: Mapped[Decimal | None] = mapped_column(Amount)
    volume_sol_24h: Mapped[Decimal | None] = mapped_column(Amount)
    trades_5m: Mapped[int | None] = mapped_column(Integer)
    trades_1h: Mapped[int | None] = mapped_column(Integer)
    buyers_5m: Mapped[int | None] = mapped_column(Integer)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class WalletSnapshot(Base):
    __tablename__ = "wallet_snapshots"

    wallet_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    sol_balance_lamports: Mapped[int | None] = mapped_column(BigInteger)
    open_position_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    ts: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    sol_price_usd: Mapped[Decimal | None] = mapped_column(Amount)
    trades_1h: Mapped[int | None] = mapped_column(BigInteger)
    volume_sol_1h: Mapped[Decimal | None] = mapped_column(Amount)
    active_wallets_1h: Mapped[int | None] = mapped_column(BigInteger)
    tokens_launched_1h: Mapped[int | None] = mapped_column(BigInteger)
    failed_tx_1h: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class LpLock(Base):
    """Best-effort LP lock observations. Append-only history of checks."""

    __tablename__ = "lp_locks"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("dex_pools.id"), index=True)
    is_locked: Mapped[bool | None] = mapped_column(Boolean)
    locked_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    unlock_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    provider: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class IngestionCheckpoint(Base):
    """Operational watermark per ingesting component (listener, writer...)."""

    __tablename__ = "ingestion_checkpoints"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_slot: Mapped[int | None] = mapped_column(BigInteger)
    last_signature: Mapped[str | None] = mapped_column(String(96))
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


# Tables promoted to TimescaleDB hypertables in migration 0001, with their
# partition column. Kept here so migrations and ops tooling share one source.
HYPERTABLES: list[tuple[str, str]] = [
    ("transactions", "block_time"),
    ("trades", "block_time"),
    ("failed_transactions", "block_time"),
    ("token_snapshots", "ts"),
    ("wallet_snapshots", "ts"),
    ("market_snapshots", "ts"),
]
