"""Central configuration.

Every runtime knob lives here and is overridable via environment variables
(or a .env file). Complex types (lists) are parsed from JSON, e.g.
ENABLED_DEXES=["pumpfun","raydium_amm"].
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- general -----------------------------------------------------------
    app_env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # --- storage -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://copytrader:copytrader@localhost:5432/copytrader"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # --- solana rpc --------------------------------------------------------
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"
    rpc_timeout_seconds: float = 30.0
    rpc_max_retries: int = 5
    rpc_requests_per_second: float = 8.0
    # Hard daily cap on RPC calls (= provider credits), shared across all
    # services via Redis. 0 disables the cap. Non-critical calls block until
    # the next UTC day once spent; execution-critical calls are exempt.
    rpc_daily_credit_budget: int = 0

    # --- ingestion ---------------------------------------------------------
    # Which venues the listener subscribes to. Values are Dex enum values.
    enabled_dexes: list[str] = [
        "pumpfun",
        "pumpswap",
        "raydium_amm",
        "raydium_clmm",
        "raydium_cpmm",
        "orca_whirlpool",
        "jupiter",
    ]
    ingest_stream_key: str = "ingest:signatures"
    ingest_group: str = "writers"
    ingest_batch_size: int = 32
    ingest_fetch_concurrency: int = 8
    ingest_max_attempts: int = 5
    ingest_dedup_ttl_seconds: int = 3600
    ingest_stream_maxlen: int = 1_000_000
    # Store the full raw transaction JSON on the transactions table. Costly at
    # scale; enable only when deep offline analysis is worth the disk.
    ingest_store_raw: bool = False

    # --- enrichment --------------------------------------------------------
    token_snapshot_interval_seconds: int = 60
    market_snapshot_interval_seconds: int = 60
    wallet_balance_interval_seconds: int = 300
    metadata_refresh_interval_seconds: int = 120
    holders_enabled: bool = False  # getProgramAccounts is heavy on public RPC
    holders_interval_seconds: int = 900
    holders_cache_ttl_seconds: int = 1800
    snapshot_active_token_limit: int = 200
    wallet_balance_batch: int = 100
    metadata_batch: int = 50
    sol_price_url: str = (
        "https://lite-api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
    )

    # --- analytics (Phase 2) -----------------------------------------------
    wallet_stats_interval_seconds: int = 300
    strategy_interval_seconds: int = 3600
    patterns_interval_seconds: int = 3600
    ml_retrain_interval_seconds: int = 86400
    # Wallets need this many closed positions before a confidence score is
    # meaningful; below it, scores shrink hard toward the prior.
    analytics_min_closed_positions: int = 5
    analytics_wallet_batch: int = 2000  # wallets recomputed per stats cycle
    strategy_min_wallets: int = 20  # below this, rule-based styles only
    strategy_clusters_k: int = 5
    strategy_window_days: int = 30
    patterns_window_days: int = 30
    patterns_min_evidence: int = 30
    ml_min_training_rows: int = 500
    ml_label_horizon_hours: int = 24
    ml_model_dir: str = "./models"

    # --- optimization & learning (Phase 4) ---------------------------------
    evaluation_interval_seconds: int = 900
    regime_interval_seconds: int = 900
    report_interval_seconds: int = 86400
    # A prediction is resolvable once the referenced position has closed (or
    # the label horizon has elapsed for a still-open position).
    eval_resolve_batch: int = 1000
    eval_window_days: int = 30
    eval_min_resolved: int = 20  # min resolved predictions to publish metrics
    eval_calibration_bins: int = 10
    regime_window_minutes: int = 60
    regime_lookback_windows: int = 24  # windows compared for trend detection
    report_wallet_top_n: int = 10
    regime_strategy_window_days: int = 30

    # --- copy trading (Phase 3) --------------------------------------------
    # Master switch. Even when enabled, mode defaults to paper: live trading
    # additionally requires copy_mode="live" AND a funded keypair.
    copy_enabled: bool = False
    copy_mode: str = "paper"  # paper | live
    # Base58 or JSON-array secret key for the DEDICATED trading wallet.
    # Never a main wallet. Only read when copy_mode == "live".
    trading_wallet_secret: str | None = None
    # Which leaders to follow: manually tracked wallets, plus (optionally)
    # any wallet whose confidence score clears the auto-follow bar.
    copy_auto_follow: bool = True
    copy_min_wallet_confidence: float = 65.0
    # Decision thresholds and filters.
    copy_min_confidence: float = 60.0
    copy_max_risk: float = 70.0
    copy_min_liquidity_sol: float = 25.0
    copy_min_market_cap_usd: float = 10_000.0
    copy_max_market_cap_usd: float = 50_000_000.0
    copy_token_blacklist: list[str] = []
    copy_wallet_blacklist: list[str] = []
    # Sizing.
    copy_size_mode: str = "fixed"  # fixed | percent (of leader's size)
    copy_fixed_sol: float = 0.05
    copy_percent_of_leader: float = 2.0  # percent when copy_size_mode=percent
    copy_max_position_sol: float = 0.5
    copy_max_open_positions: int = 10
    # Execution.
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    copy_slippage_bps: int = 300
    copy_execution_attempts: int = 3
    copy_confirm_timeout_seconds: float = 45.0
    # Safety rails.
    copy_daily_loss_limit_sol: float = 1.0
    copy_max_exposure_sol: float = 2.0
    copy_token_cooldown_seconds: int = 900
    copy_max_consecutive_failures: int = 5
    copy_approval_mode: bool = False
    # Shared secret for state-changing copytrading endpoints (resume,
    # approvals). Unset = endpoints refuse in anything but dev.
    admin_token: str | None = None

    # --- token rug-risk engine --------------------------------------------
    # Pre-copy structural risk assessment. Hard filters (active mint/freeze
    # authority, extreme holder concentration) block regardless of score and
    # are never subject to learned weighting.
    rug_check_enabled: bool = True
    rug_max_score: float = 60.0
    rug_block_mint_authority: bool = True
    rug_block_freeze_authority: bool = True
    rug_block_top10_pct: float = 0.70  # hard block above this supply share
    # Treat probe-unknown authorities as blocking (fail closed) or only as
    # elevated component risk (fail open). Closed is the safe default.
    rug_fail_closed: bool = True
    # Reuse a recent assessment instead of re-probing on every leader buy.
    rug_assessment_ttl_seconds: int = 600
    # Learning loop: label outcomes and tune soft weights within bounds.
    rug_learning_enabled: bool = True
    rug_learning_interval_seconds: int = 21_600
    rug_outcome_min_age_hours: int = 12
    rug_learning_min_samples: int = 50

    # --- api ---------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = ["*"]

    # --- auth (Phase 5) ----------------------------------------------------
    # HS256 signing secret for dashboard JWTs. MUST be overridden in prod;
    # the API refuses to start auth with the default outside dev.
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 720
    # Single dashboard operator. Password is a bcrypt hash (never plaintext);
    # if unset, login is disabled and protected endpoints are inaccessible.
    admin_username: str = "admin"
    admin_password_hash: str | None = None

    # --- notifications / telegram (Phase 5) --------------------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Which notification kinds the bot forwards (empty = all).
    telegram_enabled_kinds: list[str] = []
    notify_large_market_move_pct: float = 5.0
    notify_confidence_change_min: float = 15.0  # min score delta to notify

    # --- monitoring --------------------------------------------------------
    metrics_enabled: bool = True
    metrics_port: int = 9100

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def database_url_sync(self) -> str:
        """Sync driver URL for Alembic."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
