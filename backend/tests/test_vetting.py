"""Tests for the wallet vetting engine (ring/fake-wallet detection)."""

from __future__ import annotations

import itertools
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import vetting
from app.db.models import Position, Token, Trade, Wallet, WalletStats, WalletVetting
from tests.conftest import StubRedis

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

_sig = itertools.count()


def make_settings(**overrides) -> SimpleNamespace:
    defaults = dict(
        vetting_enabled=True,
        vetting_interval_seconds=1_800,
        vetting_batch=10,
        vetting_stale_days=7,
        vetting_funding_max_pages=3,
        vetting_repeat_cast_min_tokens=4,
        vetting_repeat_cast_ratio=0.6,
        vetting_insider_profit_share=0.5,
        vetting_block_suspicious=True,
        copy_min_wallet_confidence=65.0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def funding_info(
    funder: str | None, kind: str = "unknown", inconclusive: bool = False, note: str = ""
) -> SimpleNamespace:
    # Duck-typed stand-in for app.analytics.funding.FundingInfo — the vetting
    # engine only reads attributes, so tests stay decoupled from that module.
    return SimpleNamespace(funder=funder, kind=kind, inconclusive=inconclusive, note=note)


def install_trace_funder(monkeypatch: pytest.MonkeyPatch, info) -> list[str]:
    """Patch app.analytics.funding.trace_funder; returns the call log.

    ``info`` is either a FundingInfo-like object or a callable(address) so a
    test can vary results per wallet (including raising for one of them). A
    stand-in module is registered if the sibling funding module is absent.
    """
    module = sys.modules.get("app.analytics.funding")
    if module is None:
        try:
            import app.analytics.funding as module  # noqa: PLC0415
        except ImportError:
            module = types.ModuleType("app.analytics.funding")
            monkeypatch.setitem(sys.modules, "app.analytics.funding", module)

    calls: list[str] = []

    async def trace_funder(rpc, settings, address: str):
        calls.append(address)
        return info(address) if callable(info) else info

    monkeypatch.setattr(module, "trace_funder", trace_funder, raising=False)
    return calls


async def add_wallet(
    session: AsyncSession,
    address: str,
    tracked: bool = False,
    confidence: float | None = None,
) -> Wallet:
    wallet = Wallet(
        address=address,
        first_seen_at=NOW - timedelta(days=30),
        last_seen_at=NOW,
        is_tracked=tracked,
    )
    session.add(wallet)
    await session.flush()
    if confidence is not None:
        session.add(
            WalletStats(
                wallet_id=wallet.id,
                computed_at=NOW,
                confidence_score=Decimal(str(confidence)),
            )
        )
        await session.flush()
    return wallet


async def add_token(session: AsyncSession, mint: str, creator: str | None = None) -> Token:
    token = Token(mint=mint, first_seen_at=NOW - timedelta(days=3), creator=creator)
    session.add(token)
    await session.flush()
    return token


async def add_trade(
    session: AsyncSession, wallet_id: int, token_id: int, ts: datetime
) -> None:
    session.add(
        Trade(
            signature=f"VetSig{next(_sig)}",
            event_index=0,
            block_time=ts,
            slot=1,
            wallet_id=wallet_id,
            token_id=token_id,
            dex="pumpfun",
            side="buy",
            token_amount=Decimal("100"),
            quote_amount=Decimal("1"),
            quote_mint="So11111111111111111111111111111111111111112",
        )
    )
    # Flush per row: SQLite's insertmanyvalues sentinel matching chokes on
    # batched composite-PK datetime keys (same as test_risk_learning).
    await session.flush()


async def add_closed_position(
    session: AsyncSession, wallet_id: int, token_id: int, pnl: str
) -> None:
    session.add(
        Position(
            wallet_id=wallet_id,
            token_id=token_id,
            status="closed",
            opened_at=NOW - timedelta(days=2),
            closed_at=NOW - timedelta(days=1),
            realized_pnl_sol=Decimal(pnl),
        )
    )
    await session.flush()


async def build_ring(
    session: AsyncSession, n_wallets: int = 3, n_tokens: int = 4
) -> list[Wallet]:
    """Ring: every member trades every token, so the cast never changes."""
    wallets = [
        await add_wallet(session, f"RingWallet{i}", tracked=True) for i in range(n_wallets)
    ]
    start = NOW - timedelta(days=1)
    for token_index in range(n_tokens):
        token = await add_token(session, f"RingMint{token_index}")
        for wallet_index, wallet in enumerate(wallets):
            await add_trade(
                session,
                wallet.id,
                token.id,
                start + timedelta(minutes=token_index * 10 + wallet_index),
            )
    return wallets


# ---------------------------------------------------------------------------
# vet_wallet
# ---------------------------------------------------------------------------


async def test_ring_flagged_repeat_cast_and_shared_funder(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(
        monkeypatch, funding_info("RingOperator", kind="wallet", note="direct transfer")
    )
    settings = make_settings()
    wallets = await build_ring(db_session)

    results = [
        await vetting.vet_wallet(
            db_session, None, stub_redis, settings, wallet, now=NOW + timedelta(minutes=i)
        )
        for i, wallet in enumerate(wallets)
    ]

    assert all(row.verdict == "suspicious" for row in results)
    assert all(vetting.REASON_REPEAT_CAST in row.reasons for row in results)
    # The funder cluster needs a persisted peer verdict, so members vetted
    # after the first also carry the shared-funder reason.
    assert vetting.REASON_SHARED_FUNDER in results[1].reasons
    assert vetting.REASON_SHARED_FUNDER in results[2].reasons

    # Re-vetting the first member now sees the other two as latest verdicts.
    again = await vetting.vet_wallet(
        db_session, None, stub_redis, settings, wallets[0], now=NOW + timedelta(minutes=9)
    )
    assert again.verdict == "suspicious"
    assert set(again.reasons) >= {vetting.REASON_REPEAT_CAST, vetting.REASON_SHARED_FUNDER}
    assert again.funder == "RingOperator"
    assert again.funder_kind == "wallet"
    assert again.engine_version == vetting.ENGINE_VERSION
    assert again.signals["counterparty"]["token_count"] == 4
    assert again.signals["counterparty"]["repeat_cast_ratio"] == 1.0
    peer_ids = set(again.signals["funding"]["cluster_peer_wallet_ids"])
    assert peer_ids == {wallets[1].id, wallets[2].id}
    assert stub_redis.data[f"vetting:{wallets[0].id}"] == "suspicious"


async def test_organic_wallet_clear(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(
        monkeypatch, funding_info("CexHotWallet", kind="cex", note="binance hop")
    )
    settings = make_settings()
    wallet = await add_wallet(db_session, "OrganicWinner", tracked=True)
    # Five tokens, each with a fresh pair of co-traders: no recurring cast.
    # Trade.wallet_id has no FK, so synthetic co-trader ids are fine.
    for i in range(5):
        token = await add_token(db_session, f"OrganicMint{i}")
        ts = NOW - timedelta(hours=5 - i)
        await add_trade(db_session, wallet.id, token.id, ts)
        await add_trade(db_session, 10_000 + 2 * i, token.id, ts + timedelta(minutes=1))
        await add_trade(db_session, 10_001 + 2 * i, token.id, ts + timedelta(minutes=2))

    row = await vetting.vet_wallet(db_session, None, stub_redis, settings, wallet, now=NOW)

    assert row.verdict == "clear"
    assert row.reasons == []
    assert row.signals["counterparty"]["token_count"] == 5
    assert row.signals["counterparty"]["repeat_cast_ratio"] == 0.0
    assert row.signals["counterparty"]["avg_co_traders"] == 2.0
    assert stub_redis.data[f"vetting:{wallet.id}"] == "clear"
    assert stub_redis.published == []  # clear verdicts stay quiet


async def test_inconclusive_funding_with_thin_evidence(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(
        monkeypatch, funding_info(None, kind="unknown", inconclusive=True, note="deep history")
    )
    settings = make_settings()

    thin = await add_wallet(db_session, "ThinWallet", tracked=True)
    token = await add_token(db_session, "ThinMint")
    await add_trade(db_session, thin.id, token.id, NOW - timedelta(hours=1))

    row = await vetting.vet_wallet(db_session, None, stub_redis, settings, thin, now=NOW)
    assert row.verdict == "inconclusive"
    assert row.reasons == []
    assert stub_redis.data[f"vetting:{thin.id}"] == "inconclusive"

    # Same inconclusive funding but a rich, varied token history: clear.
    rich = await add_wallet(db_session, "RichHistory", tracked=True)
    for i in range(4):
        token = await add_token(db_session, f"RichMint{i}")
        ts = NOW - timedelta(hours=4 - i)
        await add_trade(db_session, rich.id, token.id, ts)
        await add_trade(db_session, 20_000 + i, token.id, ts + timedelta(minutes=1))
    row = await vetting.vet_wallet(db_session, None, stub_redis, settings, rich, now=NOW)
    assert row.verdict == "clear"


async def test_insider_profit_share_flags(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(
        monkeypatch, funding_info("DevFunder", kind="wallet", note="direct transfer")
    )
    settings = make_settings()
    wallet = await add_wallet(db_session, "InsiderWallet", tracked=True)

    funder_token = await add_token(db_session, "FunderMint", creator="DevFunder")
    own_token = await add_token(db_session, "OwnMint", creator="InsiderWallet")
    other_token = await add_token(db_session, "HonestMint", creator="SomeoneElse")
    loser_token = await add_token(db_session, "LoserMint", creator="DevFunder")

    await add_closed_position(db_session, wallet.id, funder_token.id, "3")
    await add_closed_position(db_session, wallet.id, own_token.id, "2")
    await add_closed_position(db_session, wallet.id, other_token.id, "1")
    # A losing position on a funder-created token must not count either way.
    await add_closed_position(db_session, wallet.id, loser_token.id, "-1")

    row = await vetting.vet_wallet(db_session, None, stub_redis, settings, wallet, now=NOW)

    assert row.verdict == "suspicious"
    assert row.reasons == [vetting.REASON_INSIDER]
    assert row.signals["insider"] == {
        "profitable_positions": 3,
        "insider_positions": 2,
        "insider_share": 0.6667,
    }


async def test_suspicious_tracked_wallet_emits_notification(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(
        monkeypatch, funding_info("DevFunder", kind="wallet", note="direct transfer")
    )
    settings = make_settings()
    wallet = await add_wallet(db_session, "FlaggedTracked", tracked=True)
    token_a = await add_token(db_session, "NotifMintA", creator="DevFunder")
    token_b = await add_token(db_session, "NotifMintB", creator="DevFunder")
    await add_closed_position(db_session, wallet.id, token_a.id, "1")
    await add_closed_position(db_session, wallet.id, token_b.id, "2")

    await vetting.vet_wallet(db_session, None, stub_redis, settings, wallet, now=NOW)

    assert len(stub_redis.published) == 1
    channel, raw = stub_redis.published[0]
    assert channel == "events:notifications"
    payload = json.loads(raw)
    assert payload["kind"] == "wallet_flagged"
    assert payload["data"]["address"] == "FlaggedTracked"
    assert payload["data"]["tracked"] is True
    assert vetting.REASON_INSIDER in payload["data"]["reasons"]
    assert len(stub_redis.lists["notifications:recent"]) == 1


async def test_notify_failure_never_fails_vetting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingPublishRedis(StubRedis):
        async def publish(self, channel: str, message: str) -> int:
            raise RuntimeError("redis pub/sub down")

    install_trace_funder(
        monkeypatch, funding_info("DevFunder", kind="wallet", note="direct transfer")
    )
    settings = make_settings()
    redis = ExplodingPublishRedis()
    wallet = await add_wallet(db_session, "NotifyFail", tracked=True)
    token_a = await add_token(db_session, "FailMintA", creator="DevFunder")
    token_b = await add_token(db_session, "FailMintB", creator="DevFunder")
    await add_closed_position(db_session, wallet.id, token_a.id, "1")
    await add_closed_position(db_session, wallet.id, token_b.id, "2")

    row = await vetting.vet_wallet(db_session, None, redis, settings, wallet, now=NOW)

    assert row.verdict == "suspicious"
    assert redis.data[f"vetting:{wallet.id}"] == "suspicious"


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def seed_vetting(session: AsyncSession, wallet_id: int, ts: datetime) -> None:
    session.add(
        WalletVetting(
            wallet_id=wallet_id, ts=ts, verdict="clear", engine_version=vetting.ENGINE_VERSION
        )
    )


async def test_run_once_respects_batch_staleness_and_candidacy(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_trace_funder(monkeypatch, funding_info("CexHotWallet", kind="cex"))
    settings = make_settings(vetting_batch=2, vetting_stale_days=7)

    tracked_a = await add_wallet(db_session, "TrackedA", tracked=True)
    tracked_b = await add_wallet(db_session, "TrackedB", tracked=True)
    # Near the auto-follow bar (65 - 15 = 50): candidate despite not tracked.
    near_bar = await add_wallet(db_session, "NearBar", confidence=55.0)
    low_conf = await add_wallet(db_session, "LowConf", confidence=30.0)
    fresh = await add_wallet(db_session, "FreshlyVetted", tracked=True)
    stale = await add_wallet(db_session, "StaleVetted", tracked=True)
    seed_vetting(db_session, fresh.id, NOW - timedelta(days=1))
    seed_vetting(db_session, stale.id, NOW - timedelta(days=8))
    await db_session.flush()

    # Cycle 1: tracked first, in id order — batch caps at two.
    assert await vetting.run_once(db_session, None, stub_redis, settings, now=NOW) == 2
    vetted_ids = {
        wid
        for (wid,) in (
            await db_session.execute(
                select(WalletVetting.wallet_id).where(WalletVetting.ts == NOW)
            )
        ).all()
    }
    assert vetted_ids == {tracked_a.id, tracked_b.id}

    # Cycle 2: the stale tracked wallet, then the near-bar candidate.
    assert await vetting.run_once(db_session, None, stub_redis, settings, now=NOW) == 2
    # Cycle 3: everything eligible is fresh now.
    assert await vetting.run_once(db_session, None, stub_redis, settings, now=NOW) == 0

    rows = (await db_session.execute(select(WalletVetting.wallet_id))).scalars().all()
    per_wallet = {wid: rows.count(wid) for wid in set(rows)}
    assert per_wallet[stale.id] == 2  # stale seed + fresh verdict
    assert per_wallet[fresh.id] == 1  # only the seed — never re-vetted
    assert per_wallet[near_bar.id] == 1
    assert low_conf.id not in per_wallet
    assert f"vetting:{near_bar.id}" in stub_redis.data
    assert f"vetting:{low_conf.id}" not in stub_redis.data


async def test_run_once_disabled_and_failure_isolation(
    db_session: AsyncSession, stub_redis: StubRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    def per_address(address: str):
        if address == "BadWallet":
            raise RuntimeError("rpc exploded")
        return funding_info("CexHotWallet", kind="cex")

    install_trace_funder(monkeypatch, per_address)
    bad = await add_wallet(db_session, "BadWallet", tracked=True)
    good = await add_wallet(db_session, "GoodWallet", tracked=True)

    disabled = make_settings(vetting_enabled=False)
    assert await vetting.run_once(db_session, None, stub_redis, disabled, now=NOW) == 0

    settings = make_settings()
    # One wallet's failure must not kill the cycle for the other.
    assert await vetting.run_once(db_session, None, stub_redis, settings, now=NOW) == 1
    rows = (await db_session.execute(select(WalletVetting.wallet_id))).scalars().all()
    assert rows == [good.id]
    assert f"vetting:{bad.id}" not in stub_redis.data
