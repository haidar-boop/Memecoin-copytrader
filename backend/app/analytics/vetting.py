"""Wallet vetting engine: fake-wallet / wash-trading-ring detection.

Composes three independent signals into one append-only
:class:`~app.db.models.WalletVetting` verdict per wallet:

1. **Counterparty concentration** (DB-only): a wallet whose tokens keep being
   traded by the same small cast of co-wallets looks like a ring pumping its
   own stats, while an organic winner meets a different crowd on every token.
2. **Funding provenance**: where the wallet's SOL came from
   (:func:`app.analytics.funding.trace_funder`). Two "independent" leaders
   funded by the same non-CEX wallet are one operator wearing two masks.
3. **Insider linkage**: profits concentrated in tokens *created by* the wallet
   itself or its funder are manufactured wins, not trading skill.

Verdicts are deliberately conservative: our trade sample is partial (sampled
firehose + follow lane), so counterparty evidence only ever *flags*, and thin
evidence plus an inconclusive funding trace yields "inconclusive" rather than
"clear". The latest verdict is mirrored to Redis (``vetting:<wallet_id>``,
plain verdict string, no TTL) for the evaluator's hot path; suspicious
verdicts also emit a ``wallet_flagged`` notification so the operator can
review starred wallets that stay copied despite the flag.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Position, Token, Trade, Wallet, WalletStats, WalletVetting
from app.logging_config import get_logger
from app.services.notifications import Notification, NotificationService

logger = get_logger(__name__)

ENGINE_VERSION = "1"

VERDICT_CLEAR = "clear"
VERDICT_SUSPICIOUS = "suspicious"
VERDICT_INCONCLUSIVE = "inconclusive"

REASON_REPEAT_CAST = "repeat_cast"
REASON_SHARED_FUNDER = "shared_funder_cluster"
REASON_INSIDER = "insider_profit_share"

# Cap on tokens sampled for the counterparty signal: bounds query cost per
# wallet and keeps the metric comparable across wallets of any activity level.
MAX_COUNTERPARTY_TOKENS = 20

# Candidates include wallets within this margin below the auto-follow bar, so
# a verdict already exists by the time a rising wallet crosses it — vetting
# must never be the thing a hot wallet waits on.
CANDIDATE_CONFIDENCE_MARGIN = 15.0


def vetting_key(wallet_id: int) -> str:
    """Redis key mirroring the latest verdict for the evaluator hot path."""
    return f"vetting:{wallet_id}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _confidence_floor(settings: Any) -> float:
    return float(settings.copy_min_wallet_confidence) - CANDIDATE_CONFIDENCE_MARGIN


async def _counterparty_signal(
    session: AsyncSession, settings: Any, wallet: Wallet
) -> tuple[dict[str, Any], list[str]]:
    """Repeat-cast detection over the wallet's most recent tokens.

    Only wallets present in *our* trade sample count as co-traders, so the
    metric understates real crowds — which is the safe direction: it can only
    make a ring look more organic, never an organic wallet look like a ring.
    """
    recent_tokens = (
        await session.execute(
            select(Trade.token_id)
            .where(Trade.wallet_id == wallet.id)
            .group_by(Trade.token_id)
            .order_by(func.max(Trade.block_time).desc())
            .limit(MAX_COUNTERPARTY_TOKENS)
        )
    ).scalars().all()
    token_ids = list(recent_tokens)
    token_count = len(token_ids)

    co_by_token: dict[int, set[int]] = {token_id: set() for token_id in token_ids}
    if token_ids:
        rows = await session.execute(
            select(Trade.token_id, Trade.wallet_id)
            .where(Trade.token_id.in_(token_ids))
            .where(Trade.wallet_id != wallet.id)
            .distinct()
        )
        for token_id, co_wallet_id in rows:
            co_by_token[token_id].add(co_wallet_id)

    avg_co_traders = (
        sum(len(co) for co in co_by_token.values()) / token_count if token_count else 0.0
    )
    # A "recurring" co-wallet shows up alongside this wallet in at least
    # vetting_repeat_cast_min_tokens of its tokens; a token is "repeat-cast"
    # when two or more of those recurring co-wallets are both present.
    appearances = Counter(
        co_wallet_id for co in co_by_token.values() for co_wallet_id in co
    )
    recurring = {
        co_wallet_id
        for co_wallet_id, count in appearances.items()
        if count >= settings.vetting_repeat_cast_min_tokens
    }
    repeat_cast_tokens = sum(
        1 for co in co_by_token.values() if len(co & recurring) >= 2
    )
    ratio = repeat_cast_tokens / token_count if token_count else 0.0

    reasons: list[str] = []
    if (
        token_count >= settings.vetting_repeat_cast_min_tokens
        and ratio >= settings.vetting_repeat_cast_ratio
    ):
        reasons.append(REASON_REPEAT_CAST)

    evidence = {
        "token_count": token_count,
        "avg_co_traders": round(avg_co_traders, 4),
        "repeat_cast_ratio": round(ratio, 4),
        "repeat_cast_tokens": repeat_cast_tokens,
        "recurring_co_wallets": len(recurring),
        "note": "sampled trade data; co-trader counts are lower bounds",
    }
    return evidence, reasons


async def _funding_signal(
    session: AsyncSession, settings: Any, wallet: Wallet, info: Any
) -> tuple[dict[str, Any], list[str]]:
    """Shared-funder cluster check against other followed/candidate wallets.

    CEX hot wallets fund thousands of unrelated users, so only ``kind ==
    "wallet"`` funders count as cluster evidence. Peers are matched on their
    *latest* vetting row (max id: the table is append-only, so insert order
    is verdict order and avoids duplicate-timestamp join fanout).
    """
    peer_ids: list[int] = []
    if info.funder and info.kind == "wallet":
        latest = (
            select(func.max(WalletVetting.id).label("max_id"))
            .group_by(WalletVetting.wallet_id)
            .subquery()
        )
        peer_ids = list(
            (
                await session.execute(
                    select(WalletVetting.wallet_id)
                    .join(latest, WalletVetting.id == latest.c.max_id)
                    .join(Wallet, Wallet.id == WalletVetting.wallet_id)
                    .outerjoin(WalletStats, WalletStats.wallet_id == Wallet.id)
                    .where(WalletVetting.wallet_id != wallet.id)
                    .where(WalletVetting.funder == info.funder)
                    .where(WalletVetting.funder_kind == "wallet")
                    .where(
                        or_(
                            Wallet.is_tracked.is_(True),
                            WalletStats.confidence_score >= _confidence_floor(settings),
                        )
                    )
                    .distinct()
                )
            ).scalars().all()
        )

    reasons = [REASON_SHARED_FUNDER] if peer_ids else []
    evidence = {
        "funder": info.funder,
        "kind": info.kind,
        "inconclusive": bool(info.inconclusive),
        "note": info.note,
        "cluster_peer_wallet_ids": peer_ids,
    }
    return evidence, reasons


async def _insider_signal(
    session: AsyncSession, settings: Any, wallet: Wallet, funder: str | None
) -> tuple[dict[str, Any], list[str]]:
    """Share of the wallet's realized winners created by itself or its funder."""
    creators = (
        await session.execute(
            select(Token.creator)
            .join(Position, Position.token_id == Token.id)
            .where(Position.wallet_id == wallet.id)
            .where(Position.status == "closed")
            .where(Position.realized_pnl_sol > 0)
        )
    ).scalars().all()
    profitable = len(creators)
    insider = sum(
        1
        for creator in creators
        if creator is not None
        and (creator == wallet.address or (funder is not None and creator == funder))
    )
    share = insider / profitable if profitable else 0.0

    reasons: list[str] = []
    if insider >= 2 and share >= settings.vetting_insider_profit_share:
        reasons.append(REASON_INSIDER)

    evidence = {
        "profitable_positions": profitable,
        "insider_positions": insider,
        "insider_share": round(share, 4),
    }
    return evidence, reasons


async def vet_wallet(
    session: AsyncSession,
    rpc: Any,
    redis: Any,
    settings: Any,
    wallet: Wallet,
    now: datetime | None = None,
) -> WalletVetting:
    """Vet one wallet and persist an append-only verdict row.

    Adds + flushes but never commits — the caller owns the transaction so a
    runner can batch a whole cycle into one commit. The Redis mirror and the
    flagged notification are written here because they must track the verdict
    even when the caller batches.
    """
    # Late import: the funding module is a sibling deliverable and tests
    # monkeypatch app.analytics.funding.trace_funder — binding at call time
    # keeps this module importable and patchable either way.
    from app.analytics import funding

    now = now or _utcnow()
    counterparty, cp_reasons = await _counterparty_signal(session, settings, wallet)
    info = await funding.trace_funder(rpc, settings, wallet.address)
    funding_evidence, funding_reasons = await _funding_signal(session, settings, wallet, info)
    insider_evidence, insider_reasons = await _insider_signal(
        session, settings, wallet, info.funder
    )

    reasons = [*cp_reasons, *funding_reasons, *insider_reasons]
    if reasons:
        verdict = VERDICT_SUSPICIOUS
    elif (
        bool(info.inconclusive)
        and counterparty["token_count"] < settings.vetting_repeat_cast_min_tokens
    ):
        # Nothing exonerating either: we could not trace the money and have
        # too few tokens to judge the crowd. Refuse to call it clear.
        verdict = VERDICT_INCONCLUSIVE
    else:
        verdict = VERDICT_CLEAR

    row = WalletVetting(
        wallet_id=wallet.id,
        ts=now,
        verdict=verdict,
        funder=info.funder,
        funder_kind=info.kind,
        signals={
            "counterparty": counterparty,
            "funding": funding_evidence,
            "insider": insider_evidence,
        },
        reasons=reasons,
        engine_version=ENGINE_VERSION,
    )
    session.add(row)
    await session.flush()

    await redis.set(vetting_key(wallet.id), verdict)

    if verdict == VERDICT_SUSPICIOUS:
        try:
            await NotificationService(redis).emit(
                Notification.wallet_flagged(
                    wallet.address, reasons, tracked=wallet.is_tracked
                )
            )
        except Exception:  # noqa: BLE001 — a broken notifier must never fail vetting
            logger.warning("vetting_notify_failed", wallet_id=wallet.id, exc_info=True)

    logger.info(
        "wallet_vetted",
        wallet_id=wallet.id,
        address=wallet.address,
        verdict=verdict,
        reasons=reasons,
    )
    return row


async def run_once(
    session: AsyncSession,
    rpc: Any,
    redis: Any,
    settings: Any,
    now: datetime | None = None,
) -> int:
    """One vetting cycle: pick stale candidates, vet them, commit once.

    Candidates are the wallets whose verdicts can actually gate copying:
    tracked wallets plus anything near the auto-follow confidence bar.
    Tracked wallets go first — a bad verdict there is actionable *today*.
    Returns the number of wallets vetted.
    """
    if not settings.vetting_enabled:
        return 0
    now = now or _utcnow()
    cutoff = now - timedelta(days=settings.vetting_stale_days)

    recently_vetted = select(WalletVetting.wallet_id).where(WalletVetting.ts >= cutoff)
    wallets = (
        await session.execute(
            select(Wallet)
            .outerjoin(WalletStats, WalletStats.wallet_id == Wallet.id)
            .where(
                or_(
                    Wallet.is_tracked.is_(True),
                    WalletStats.confidence_score >= _confidence_floor(settings),
                )
            )
            .where(Wallet.id.not_in(recently_vetted))
            .order_by(
                Wallet.is_tracked.desc(),
                WalletStats.confidence_score.desc().nulls_last(),
                Wallet.id,
            )
            .limit(settings.vetting_batch)
        )
    ).scalars().all()

    vetted = 0
    for wallet in wallets:
        try:
            await vet_wallet(session, rpc, redis, settings, wallet, now=now)
            vetted += 1
        except Exception:  # noqa: BLE001 — one bad wallet must not kill the cycle
            logger.warning("wallet_vetting_failed", wallet_id=wallet.id, exc_info=True)

    await session.commit()
    if wallets:
        logger.info("vetting_cycle_done", candidates=len(wallets), vetted=vetted)
    return vetted
