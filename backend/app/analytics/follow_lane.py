"""Publishes the followed-wallet set for the ingestion priority lane.

The copy engine follows manually tracked wallets plus any wallet whose
confidence clears the auto-follow bar. Under RPC budget caps the firehose is
sampled, so a followed wallet's buys would mostly be trimmed unfetched; the
WS listener therefore opens a dedicated logsSubscribe per followed wallet and
routes those signatures onto a priority stream. This module keeps the Redis
set the listener reads in sync with reality.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Wallet, WalletStats
from app.logging_config import get_logger
from app.services.redis import set_followed_wallets

log = get_logger(__name__)


async def publish_followed(
    session: AsyncSession, redis: Any, settings: Settings
) -> list[str]:
    """Compute and publish the followed set; returns the addresses.

    Tracked wallets always make the cut; the remainder of the
    ``follow_lane_max`` budget goes to the highest-confidence qualifiers so
    subscription count stays bounded no matter how many wallets qualify.
    """
    if not settings.follow_lane_enabled:
        await set_followed_wallets(redis, [])
        return []
    rows = (
        await session.execute(
            select(Wallet.address, Wallet.is_tracked, WalletStats.confidence_score)
            .outerjoin(WalletStats, WalletStats.wallet_id == Wallet.id)
            .where(
                or_(
                    Wallet.is_tracked.is_(True),
                    WalletStats.confidence_score
                    >= settings.copy_min_wallet_confidence,
                )
            )
        )
    ).all()
    tracked = [addr for addr, is_tracked, _ in rows if is_tracked]
    qualifiers = sorted(
        (
            (float(conf), addr)
            for addr, is_tracked, conf in rows
            if not is_tracked and conf is not None
        ),
        reverse=True,
    )
    remaining = max(settings.follow_lane_max - len(tracked), 0)
    addresses = tracked + [addr for _, addr in qualifiers[:remaining]]
    addresses = addresses[: settings.follow_lane_max]
    await set_followed_wallets(redis, addresses)
    log.info(
        "follow_lane_published",
        tracked=len(tracked),
        auto=len(addresses) - len(tracked),
    )
    return addresses
