"""Periodic enrichment jobs: metadata, balances, holders, snapshots, market.

``run_enrichment_loop`` runs one asyncio task per enabled job. Each task
sleeps a staggered initial delay (so cold start does not burst the RPC), then
alternates run/sleep forever. Every cycle runs in its own DB session and is
wrapped in try/except: a failing cycle is logged and counted in the
``JOB_RUNS`` metric but never kills the loop. Cancellation (worker
shutdown) tears all job tasks down and returns cleanly.

Each job module also exposes a ``run_once``-style function taking explicit
dependencies, so tests (and ad-hoc backfills) can invoke a single cycle
without the loop machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.enrichment import holders, market, snapshots, token_metadata, wallet_balances
from app.logging_config import get_logger
from app.services.jobs import JobSpec as _JobSpec
from app.services.jobs import run_jobs

log = get_logger(__name__)

__all__ = ["run_enrichment_loop"]


class EnrichmentRpc(Protocol):
    """The RPC surface the enrichment jobs need (tests inject stubs)."""

    async def get_token_supply(self, mint: str) -> dict | None: ...

    async def get_account_info(self, pubkey: str, encoding: str = "base64") -> dict | None: ...

    async def get_balance(self, pubkey: str) -> int | None: ...

    async def get_token_account_balance(self, account: str) -> dict | None: ...

    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict | None]: ...

    async def get_program_accounts_count(
        self, program_id: str, filters: list[dict] | None = None
    ) -> int: ...


class EnrichmentRedis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None) -> object: ...


def _build_job_specs(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    rpc: EnrichmentRpc,
    redis: EnrichmentRedis,
) -> list[_JobSpec]:
    specs: list[_JobSpec] = []

    async def metadata_cycle() -> None:
        async with session_factory() as session:
            await token_metadata.run_once(session, rpc, batch=settings.metadata_batch)

    specs.append(
        ("token_metadata", float(settings.metadata_refresh_interval_seconds), metadata_cycle)
    )

    async def snapshot_cycle() -> None:
        async with session_factory() as session:
            await snapshots.run_once(
                session,
                rpc,
                redis,
                interval_seconds=settings.token_snapshot_interval_seconds,
                active_token_limit=settings.snapshot_active_token_limit,
            )

    specs.append(
        ("token_snapshots", float(settings.token_snapshot_interval_seconds), snapshot_cycle)
    )

    async def market_cycle() -> None:
        async with session_factory() as session:
            await market.run_once(session, redis, price_url=settings.sol_price_url)

    specs.append(
        ("market_snapshot", float(settings.market_snapshot_interval_seconds), market_cycle)
    )

    # Watermark: each cycle refreshes wallets seen since the previous cycle.
    wallet_state = {
        "since": datetime.now(tz=UTC)
        - timedelta(seconds=settings.wallet_balance_interval_seconds)
    }

    async def wallet_cycle() -> None:
        cycle_started = datetime.now(tz=UTC)
        async with session_factory() as session:
            await wallet_balances.run_once(
                session,
                rpc,
                since=wallet_state["since"],
                batch=settings.wallet_balance_batch,
                now=cycle_started,
            )
        wallet_state["since"] = cycle_started

    specs.append(
        ("wallet_balances", float(settings.wallet_balance_interval_seconds), wallet_cycle)
    )

    if settings.holders_enabled:

        async def holders_cycle() -> None:
            now = datetime.now(tz=UTC)
            async with session_factory() as session:
                await holders.run_once(
                    session,
                    rpc,
                    redis,
                    active_since=now - timedelta(seconds=settings.holders_interval_seconds),
                    limit=settings.snapshot_active_token_limit,
                    cache_ttl_seconds=settings.holders_cache_ttl_seconds,
                )

        specs.append(("holders", float(settings.holders_interval_seconds), holders_cycle))

    return specs


async def run_enrichment_loop(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    rpc: EnrichmentRpc,
    redis: EnrichmentRedis,
) -> None:
    """Start every periodic enrichment job; returns cleanly on cancellation."""
    await run_jobs("enrichment", _build_job_specs(settings, session_factory, rpc, redis))
