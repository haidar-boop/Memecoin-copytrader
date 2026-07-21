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

    # --- api ---------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = ["*"]

    # --- monitoring --------------------------------------------------------
    metrics_enabled: bool = True
    metrics_port: int = 9100

    @property
    def database_url_sync(self) -> str:
        """Sync driver URL for Alembic."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
