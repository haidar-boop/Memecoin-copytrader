"""Wallet SOL balance refresh plus append-only wallet snapshots.

Each cycle refreshes wallets active since the previous cycle (``last_seen_at``
watermark, capped by the batch setting), batching ``getMultipleAccounts`` in
chunks of 100 — the RPC method's own limit. The current balance is written to
the wallet row (operational state) and to a new ``wallet_snapshots`` row
(training history; never updated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Position, Wallet, WalletSnapshot
from app.logging_config import get_logger

log = get_logger(__name__)

_CHUNK_SIZE = 100  # getMultipleAccounts hard limit


class _Rpc(Protocol):
    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict | None]: ...


async def _open_position_counts(session: AsyncSession, wallet_ids: list[int]) -> dict[int, int]:
    rows = await session.execute(
        select(Position.wallet_id, func.count())
        .where(Position.wallet_id.in_(wallet_ids), Position.status == "open")
        .group_by(Position.wallet_id)
    )
    return {row[0]: int(row[1]) for row in rows}


async def run_once(
    session: AsyncSession,
    rpc: _Rpc,
    *,
    since: datetime,
    batch: int,
    now: datetime | None = None,
) -> int:
    """Refresh balances for wallets seen since ``since``; returns wallets updated."""
    now = now or datetime.now(tz=UTC)
    wallets = (
        (
            await session.execute(
                select(Wallet)
                .where(Wallet.last_seen_at >= since)
                .order_by(Wallet.last_seen_at.desc())
                .limit(batch)
            )
        )
        .scalars()
        .all()
    )
    if not wallets:
        return 0

    open_counts = await _open_position_counts(session, [wallet.id for wallet in wallets])

    snapshot_rows: list[dict] = []
    for start in range(0, len(wallets), _CHUNK_SIZE):
        chunk = wallets[start : start + _CHUNK_SIZE]
        try:
            accounts = await rpc.get_multiple_accounts([wallet.address for wallet in chunk])
        except Exception as exc:
            log.warning("wallet_balance_fetch_failed", wallets=len(chunk), error=str(exc))
            continue
        for wallet, account in zip(chunk, accounts, strict=False):
            # A missing account means no lamports are allocated: balance zero.
            lamports = int(account.get("lamports", 0)) if isinstance(account, dict) else 0
            wallet.sol_balance_lamports = lamports
            wallet.balance_updated_at = now
            snapshot_rows.append(
                {
                    "wallet_id": wallet.id,
                    "ts": now,
                    "sol_balance_lamports": lamports,
                    "open_position_count": open_counts.get(wallet.id, 0),
                }
            )
    if snapshot_rows:
        # Core executemany INSERT: append-only, and sidesteps ORM sentinel
        # matching that SQLite's naive datetimes break on composite time PKs.
        await session.execute(insert(WalletSnapshot), snapshot_rows)
    await session.commit()
    log.info("wallet_balance_cycle", eligible=len(wallets), updated=len(snapshot_rows))
    return len(snapshot_rows)
