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
    UniqueConstraint,
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
    # On-chain security facts (rug-risk probe). None = never checked;
    # empty string = confirmed renounced; else the authority pubkey.
    mint_authority: Mapped[str | None] = mapped_column(String(64))
    freeze_authority: Mapped[str | None] = mapped_column(String(64))
    security_checked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
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


# --------------------------------------------------------------------------
# Phase 2: wallet analysis & strategy learning (migration 0002)
# --------------------------------------------------------------------------


class WalletMetricsColumns:
    """Metric columns shared by the current-stats table and its history.

    ``wallet_stats`` is operational state (recomputed in place);
    ``wallet_stats_snapshots`` appends one row per analytics cycle so score
    evolution itself becomes training data. PNL metrics are SOL-denominated
    and derived from closed positions; trades remain the source of truth.
    """

    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    buy_count: Mapped[int] = mapped_column(Integer, default=0)
    sell_count: Mapped[int] = mapped_column(Integer, default=0)
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_position_count: Mapped[int] = mapped_column(Integer, default=0)
    win_count: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    total_pnl_sol: Mapped[Decimal | None] = mapped_column(Amount)
    total_volume_sol: Mapped[Decimal | None] = mapped_column(Amount)
    avg_roi: Mapped[Decimal | None] = mapped_column(Amount)
    median_roi: Mapped[Decimal | None] = mapped_column(Amount)
    profit_factor: Mapped[Decimal | None] = mapped_column(Amount)  # None = no losses yet
    max_drawdown_sol: Mapped[Decimal | None] = mapped_column(Amount)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Amount)
    avg_hold_seconds: Mapped[int | None] = mapped_column(BigInteger)
    median_hold_seconds: Mapped[int | None] = mapped_column(BigInteger)
    avg_position_sol: Mapped[Decimal | None] = mapped_column(Amount)
    max_position_sol: Mapped[Decimal | None] = mapped_column(Amount)
    # Seconds between a token's first observed activity and this wallet's entry.
    avg_entry_delay_seconds: Mapped[int | None] = mapped_column(BigInteger)
    trades_per_day: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # Closed positions exited via more than one sell / all closed positions.
    partial_exit_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    roi_std: Mapped[Decimal | None] = mapped_column(Amount)  # consistency proxy
    pnl_7d_sol: Mapped[Decimal | None] = mapped_column(Amount)
    pnl_30d_sol: Mapped[Decimal | None] = mapped_column(Amount)
    first_trade_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_trade_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # 0..100
    # Explanation payload: [{component, value, weight, contribution, note}].
    confidence_components: Mapped[list | None] = mapped_column(JSONVariant)
    style: Mapped[str | None] = mapped_column(String(32))
    style_confidence: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))


class WalletStats(WalletMetricsColumns, Base):
    __tablename__ = "wallet_stats"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_wallet_stats_confidence", "confidence_score"),
        Index("ix_wallet_stats_style", "style"),
    )


class WalletStatsSnapshot(WalletMetricsColumns, Base):
    """Append-only history of wallet metrics. Hypertable on ``ts``."""

    __tablename__ = "wallet_stats_snapshots"

    wallet_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


class StrategyCluster(Base):
    """One discovered trading-style cluster per clustering run. Append-only."""

    __tablename__ = "strategy_clusters"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    name: Mapped[str] = mapped_column(String(32))
    member_count: Mapped[int] = mapped_column(Integer)
    feature_names: Mapped[list | None] = mapped_column(JSONVariant)
    centroid: Mapped[list | None] = mapped_column(JSONVariant)
    description: Mapped[str | None] = mapped_column(Text)


class StrategyStat(Base):
    """Per-style performance over a trailing window. Append-only."""

    __tablename__ = "strategy_stats"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    style: Mapped[str] = mapped_column(String(32))
    window_days: Mapped[int] = mapped_column(Integer)
    wallet_count: Mapped[int] = mapped_column(Integer)
    closed_positions: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    avg_roi: Mapped[Decimal | None] = mapped_column(Amount)
    total_pnl_sol: Mapped[Decimal | None] = mapped_column(Amount)
    profit_factor: Mapped[Decimal | None] = mapped_column(Amount)
    avg_hold_seconds: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_strategy_stats_style_ts", "style", "ts"),)


class MlModel(Base):
    """Registry of trained model artifacts and their evaluation metrics."""

    __tablename__ = "ml_models"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    algo: Mapped[str] = mapped_column(String(64))
    trained_at: Mapped[datetime] = mapped_column(TZDateTime)
    training_rows: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict | None] = mapped_column(JSONVariant)
    metrics: Mapped[dict | None] = mapped_column(JSONVariant)  # auc/brier/calibration...
    feature_names: Mapped[list | None] = mapped_column(JSONVariant)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_ml_models_name_version"),
        Index("ix_ml_models_name_active", "name", "is_active"),
    )


class Prediction(Base):
    """Every model prediction, kept forever for Phase 4 error analysis."""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    subject_type: Mapped[str] = mapped_column(String(16))  # trade | wallet
    signature: Mapped[str | None] = mapped_column(String(96))
    wallet_id: Mapped[int | None] = mapped_column(BigInteger)
    token_id: Mapped[int | None] = mapped_column(BigInteger)
    predicted: Mapped[dict | None] = mapped_column(JSONVariant)
    context: Mapped[dict | None] = mapped_column(JSONVariant)

    __table_args__ = (
        Index("ix_predictions_subject_created", "subject_type", "created_at"),
        Index("ix_predictions_wallet_created", "wallet_id", "created_at"),
    )


class DiscoveredPattern(Base):
    """Evidence-backed recurring pattern (time-of-day, lifecycle, whale flow...)."""

    __tablename__ = "patterns"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48))
    key: Mapped[dict | None] = mapped_column(JSONVariant)  # bucket identity, e.g. {"hour": 14}
    stats: Mapped[dict | None] = mapped_column(JSONVariant)
    evidence_count: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    window_end: Mapped[datetime | None] = mapped_column(TZDateTime)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_patterns_kind_computed", "kind", "computed_at"),)


# Hypertables added by migration 0002 (0001 owns HYPERTABLES above).
PHASE2_HYPERTABLES: list[tuple[str, str]] = [
    ("wallet_stats_snapshots", "ts"),
]


# --------------------------------------------------------------------------
# Phase 3: decision engine & copy trading (migration 0003)
# --------------------------------------------------------------------------


class TradeDecision(Base):
    """Every copy/skip evaluation, persisted forever — skips are evidence too.

    ``factors`` explains the score composition; ``reasons`` records every
    gate (filters, safety checks) with its outcome, so any decision can be
    audited and Phase 4 can grade the decision policy itself.
    """

    __tablename__ = "trade_decisions"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    source_signature: Mapped[str | None] = mapped_column(String(96))
    # NULL when the leader/token was not yet in the database at decision time
    # (an unknown-entity skip) — never a sentinel 0 that would collide in the
    # per-leader decision index.
    leader_wallet_id: Mapped[int | None] = mapped_column(BigInteger)
    token_id: Mapped[int | None] = mapped_column(BigInteger)
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    mode: Mapped[str] = mapped_column(String(8))  # paper | live
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # 0..100
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))  # 0..100
    expected_reward: Mapped[Decimal | None] = mapped_column(Amount)  # expected ROI
    expected_drawdown: Mapped[Decimal | None] = mapped_column(Amount)
    p_profit: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    decision: Mapped[str] = mapped_column(String(8))  # copy | skip
    size_sol: Mapped[Decimal | None] = mapped_column(Amount)
    reasons: Mapped[list | None] = mapped_column(JSONVariant)
    factors: Mapped[list | None] = mapped_column(JSONVariant)

    __table_args__ = (
        Index("ix_trade_decisions_created", "created_at"),
        Index("ix_trade_decisions_wallet_created", "leader_wallet_id", "created_at"),
        Index("ix_trade_decisions_decision_created", "decision", "created_at"),
    )


class CopyTrade(Base):
    """One execution attempt for a copy decision (paper or live)."""

    __tablename__ = "copy_trades"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("trade_decisions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)
    mode: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(4))
    token_id: Mapped[int] = mapped_column(BigInteger)
    leader_wallet_id: Mapped[int] = mapped_column(BigInteger)
    size_sol: Mapped[Decimal] = mapped_column(Amount)
    # pending_approval -> approved -> simulated -> submitted -> confirmed
    # (paper fills jump straight to confirmed) | failed | rejected
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    tx_signature: Mapped[str | None] = mapped_column(String(96))
    quote: Mapped[dict | None] = mapped_column(JSONVariant)
    filled_token_amount: Mapped[Decimal | None] = mapped_column(Amount)
    filled_price_sol: Mapped[Decimal | None] = mapped_column(Amount)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_copy_trades_status_created", "status", "created_at"),
        Index("ix_copy_trades_token_created", "token_id", "created_at"),
    )


class CopyPosition(Base):
    """Our own (paper or live) position opened by copying a leader."""

    __tablename__ = "copy_positions"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(BigInteger, index=True)
    leader_wallet_id: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(8), default="open")  # open | closed
    opened_at: Mapped[datetime] = mapped_column(TZDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    spent_sol: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    tokens_bought: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    sold_sol: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    tokens_sold: Mapped[Decimal] = mapped_column(Amount, default=Decimal(0))
    realized_pnl_sol: Mapped[Decimal | None] = mapped_column(Amount)
    entry_trade_id: Mapped[int | None] = mapped_column(BigInteger)
    exit_trade_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_copy_positions_status_token", "status", "token_id"),
        # One open copy position per token: mirrors an exit unambiguously.
        Index(
            "uq_copy_positions_open_token",
            "token_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )


# --------------------------------------------------------------------------
# Phase 4: optimization & continuous learning (migration 0004)
# --------------------------------------------------------------------------


class PredictionOutcome(Base):
    """Resolved prediction: what the model said vs what actually happened.

    Joins a Prediction to the realized outcome of the position it referenced,
    so the evaluation engine can score calibration and error over time. Kept
    forever; one row per resolved prediction.
    """

    __tablename__ = "prediction_outcomes"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True)
    model_id: Mapped[int] = mapped_column(BigInteger, index=True)
    resolved_at: Mapped[datetime] = mapped_column(TZDateTime)
    subject_type: Mapped[str] = mapped_column(String(16))  # trade | wallet
    # Predicted probability / value taken from the Prediction row.
    predicted_prob: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    predicted_roi: Mapped[Decimal | None] = mapped_column(Amount)
    predicted_hold_seconds: Mapped[int | None] = mapped_column(BigInteger)
    # Realized outcome.
    actual_label: Mapped[int | None] = mapped_column(Integer)  # 1 profitable else 0
    actual_roi: Mapped[Decimal | None] = mapped_column(Amount)
    actual_hold_seconds: Mapped[int | None] = mapped_column(BigInteger)
    # Errors (actual - predicted); brier for the classifier.
    roi_error: Mapped[Decimal | None] = mapped_column(Amount)
    hold_error_seconds: Mapped[int | None] = mapped_column(BigInteger)
    brier: Mapped[Decimal | None] = mapped_column(Numeric(10, 8))

    __table_args__ = (
        UniqueConstraint("prediction_id", name="uq_prediction_outcomes_prediction"),
        Index("ix_prediction_outcomes_model_resolved", "model_id", "resolved_at"),
    )


class ModelPerformance(Base):
    """Rolling accuracy metrics per model over an evaluation window. Append-only."""

    __tablename__ = "model_performance"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    model_id: Mapped[int] = mapped_column(BigInteger, index=True)
    model_name: Mapped[str] = mapped_column(String(64))
    window_days: Mapped[int] = mapped_column(Integer)
    resolved_count: Mapped[int] = mapped_column(Integer)
    auc: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    brier: Mapped[Decimal | None] = mapped_column(Numeric(10, 8))
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    base_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    mean_roi_error: Mapped[Decimal | None] = mapped_column(Amount)
    # Calibration bins: [{p_bin, predicted, observed, n}].
    calibration: Mapped[list | None] = mapped_column(JSONVariant)

    __table_args__ = (Index("ix_model_performance_name_ts", "model_name", "ts"),)


class MarketRegime(Base):
    """A detected market-condition label over a time window. Append-only."""

    __tablename__ = "market_regimes"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    window_minutes: Mapped[int] = mapped_column(Integer)
    # Primary label: bull | bear | sideways plus modifier flags below.
    regime: Mapped[str] = mapped_column(String(24))
    high_volatility: Mapped[bool] = mapped_column(Boolean, default=False)
    low_liquidity: Mapped[bool] = mapped_column(Boolean, default=False)
    whale_accumulation: Mapped[bool] = mapped_column(Boolean, default=False)
    panic_selling: Mapped[bool] = mapped_column(Boolean, default=False)
    launch_wave: Mapped[bool] = mapped_column(Boolean, default=False)
    trend_exhaustion: Mapped[bool] = mapped_column(Boolean, default=False)
    features: Mapped[dict | None] = mapped_column(JSONVariant)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_market_regimes_regime_ts", "regime", "ts"),)


class RegimeStrategyStat(Base):
    """Per-(regime, strategy) performance: which styles win in which conditions."""

    __tablename__ = "regime_strategy_stats"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    regime: Mapped[str] = mapped_column(String(24))
    style: Mapped[str] = mapped_column(String(32))
    window_days: Mapped[int] = mapped_column(Integer)
    closed_positions: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    avg_roi: Mapped[Decimal | None] = mapped_column(Amount)
    total_pnl_sol: Mapped[Decimal | None] = mapped_column(Amount)

    __table_args__ = (Index("ix_regime_strategy_regime_style_ts", "regime", "style", "ts"),)


class Report(Base):
    """A generated AI report (weekly/daily) as structured JSON + rendered text.

    Every conclusion in ``sections`` carries its supporting numbers, so the
    report explains its own reasoning. Append-only.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24))  # daily | weekly
    generated_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    window_start: Mapped[datetime] = mapped_column(TZDateTime)
    window_end: Mapped[datetime] = mapped_column(TZDateTime)
    summary: Mapped[str | None] = mapped_column(Text)
    sections: Mapped[dict | None] = mapped_column(JSONVariant)
    markdown: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_reports_kind_generated", "kind", "generated_at"),)


class TokenRiskAssessment(Base):
    """One rug-risk evaluation of a token at a moment in time. Append-only.

    ``components``/``signals`` preserve the full evidence trail; ``outcome``
    is filled in later by the risk-learning loop once the token's fate is
    observable (rug / loss / profit), closing the loop for weight tuning.
    """

    __tablename__ = "token_risk_assessments"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    score: Mapped[Decimal] = mapped_column(Amount)
    hard_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reasons: Mapped[list | None] = mapped_column(JSONVariant)
    components: Mapped[dict | None] = mapped_column(JSONVariant)
    signals: Mapped[dict | None] = mapped_column(JSONVariant)
    engine_version: Mapped[str] = mapped_column(String(16))
    weights_version: Mapped[int] = mapped_column(Integer, default=0)
    # Filled by the learning loop: rug | loss | profit | unknown.
    outcome: Mapped[str | None] = mapped_column(String(16), index=True)
    outcome_roi: Mapped[Decimal | None] = mapped_column(Amount)
    outcome_resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)

    __table_args__ = (
        Index("ix_risk_assessments_token_ts", "token_id", "ts"),
    )


class RiskWeightSnapshot(Base):
    """A learned soft-component weight set for the risk engine. Append-only.

    The newest row is the active set (mirrored to Redis for cheap reads).
    Weights only ever rescale BASE_WEIGHTS within contract bounds — hard
    filters are not represented here and cannot be influenced by learning.
    """

    __tablename__ = "risk_weight_snapshots"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    weights: Mapped[dict] = mapped_column(JSONVariant)
    sample_count: Mapped[int] = mapped_column(Integer)
    notes: Mapped[dict | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=UTC_NOW)


# Phase 4 has no hypertables (all rows are periodic aggregates, low volume).
