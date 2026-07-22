"""End-to-end paper copy-trading flow: evaluate -> execute -> mirror exit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    CopyPosition,
    CopyTrade,
    Token,
    TokenRiskAssessment,
    TokenSnapshot,
    TradeDecision,
    Wallet,
    WalletStats,
)
from app.db.util import quantize_sol
from app.decision.safety import DAILY_PNL_KEY_PREFIX, _today
from app.execution.copytrader import CopyTrader
from app.execution.jupiter import JupiterClient
from app.ingestion.programs import WSOL_MINT
from tests.conftest import StubRedis

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
MINT = "MemeMint11111111111111111111111111111111111"
LEADER = "LeaderWa11et11111111111111111111111111111111"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        copy_enabled=True,
        copy_mode="paper",
        copy_min_confidence=40.0,
        copy_max_risk=95.0,
        copy_min_liquidity_sol=10.0,
        copy_min_market_cap_usd=1000.0,
        copy_fixed_sol=0.1,
        ml_model_dir="/nonexistent",  # no active model -> weight redistributes
        # These tests run without an RPC client, so the rug probe can only
        # report unknowns; disable the gate except where a test targets it.
        rug_check_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeJupiterFetch:
    """Deterministic Jupiter API: buy fills at 0.00001 SOL, sells at +50%."""

    async def __call__(self, method: str, url: str, body: dict | None):
        if method == "GET" and f"inputMint={WSOL_MINT}" in url:
            amount = int(url.split("amount=")[1].split("&")[0])
            return {
                "inAmount": str(amount),
                "outAmount": str(amount * 100),  # token has 6 decimals
                "inputMint": WSOL_MINT,
                "outputMint": MINT,
            }
        if method == "GET":  # sell quote: token -> SOL at +50%
            amount = int(url.split("amount=")[1].split("&")[0])
            return {
                "inAmount": str(amount),
                "outAmount": str(int(amount / 100 * 1.5)),
                "inputMint": MINT,
                "outputMint": WSOL_MINT,
            }
        return {"swapTransaction": "unused-in-paper-mode"}


async def seed_market(session: AsyncSession) -> tuple[Wallet, Token]:
    wallet = Wallet(
        address=LEADER, first_seen_at=NOW - timedelta(days=30),
        last_seen_at=NOW, is_tracked=True,
    )
    token = Token(
        mint=MINT, decimals=6, first_seen_at=NOW - timedelta(hours=6),
        primary_dex="pumpfun",
    )
    session.add_all([wallet, token])
    await session.flush()
    session.add(
        WalletStats(
            wallet_id=wallet.id, computed_at=NOW, closed_position_count=40,
            win_count=26, win_rate=Decimal("0.65"), profit_factor=Decimal("2.1"),
            avg_roi=Decimal("0.35"), roi_std=Decimal("0.5"),
            max_drawdown_pct=Decimal("0.4"), confidence_score=Decimal("72"),
        )
    )
    session.add(
        TokenSnapshot(
            token_id=token.id, ts=NOW - timedelta(minutes=1),
            price_sol=Decimal("0.00001"), liquidity_sol=Decimal(120),
            volume_sol_1h=Decimal(400), market_cap_usd=Decimal(250_000),
            holder_count=500,
        )
    )
    await session.commit()
    return wallet, token


def buy_message(size: str = "5") -> str:
    return json.dumps(
        {
            "signature": "leader-sig-1",
            "wallet": LEADER,
            "token_mint": MINT,
            "side": "buy",
            "quote_amount": size,
            "quote_mint": WSOL_MINT,
            "block_time": NOW.isoformat(),
        }
    )


def sell_message() -> str:
    return json.dumps(
        {
            "signature": "leader-sig-2",
            "wallet": LEADER,
            "token_mint": MINT,
            "side": "sell",
            "quote_amount": "7",
            "quote_mint": WSOL_MINT,
            "block_time": (NOW + timedelta(minutes=30)).isoformat(),
        }
    )


def make_trader(factory, stub_redis: StubRedis, settings: Settings) -> CopyTrader:
    trader = CopyTrader(settings, stub_redis, rpc=None, session_factory=factory)  # type: ignore[arg-type]
    trader._executor._jupiter = JupiterClient("http://test", FakeJupiterFetch())
    return trader


async def test_full_paper_roundtrip(db_session: AsyncSession, stub_redis: StubRedis) -> None:
    await seed_market(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(factory, stub_redis, make_settings())

    evaluation = await trader.handle_message(buy_message())
    assert evaluation is not None and evaluation.decision == "copy"

    decision = (await db_session.execute(select(TradeDecision))).scalar_one()
    assert decision.decision == "copy"
    assert decision.factors and decision.reasons  # explanations persisted

    trade = (await db_session.execute(select(CopyTrade))).scalar_one()
    assert trade.status == "confirmed" and trade.mode == "paper"
    assert trade.filled_token_amount == Decimal("10000")  # 0.1 SOL * 100 / 1e-6... quote math
    position = (await db_session.execute(select(CopyPosition))).scalar_one()
    assert position.status == "open"
    # SQLite stores NUMERIC as float; compare at lamport precision.
    assert quantize_sol(position.spent_sol) == Decimal("0.1")

    # Leader sells -> mirror exit at +50%. The worker committed via its own
    # session, so drop this session's identity-map cache before re-reading.
    await trader.handle_message(sell_message())
    db_session.expire_all()
    position = (await db_session.execute(select(CopyPosition))).scalar_one()
    assert position.status == "closed"
    assert quantize_sol(position.realized_pnl_sol) == Decimal("0.05")  # 0.15 out - 0.1 in
    daily = Decimal(stub_redis.data[DAILY_PNL_KEY_PREFIX + _today()])
    assert daily > 0


async def test_skip_paths_are_recorded(db_session: AsyncSession, stub_redis: StubRedis) -> None:
    await seed_market(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    # Disabled master switch -> decision recorded as skip with the gate note.
    trader = make_trader(factory, stub_redis, make_settings(copy_enabled=False))
    evaluation = await trader.handle_message(buy_message())
    assert evaluation.decision == "skip"
    row = (await db_session.execute(select(TradeDecision))).scalar_one()
    gates = {r["gate"]: r["passed"] for r in row.reasons}
    assert gates["copy_enabled"] is False
    assert (await db_session.execute(select(CopyTrade))).first() is None


async def test_blacklist_and_duplicate_position_gates(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    await seed_market(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(
        factory, stub_redis, make_settings(copy_token_blacklist=[MINT])
    )
    evaluation = await trader.handle_message(buy_message())
    assert evaluation.decision == "skip"

    # Open position blocks a second buy of the same token.
    trader2 = make_trader(factory, stub_redis, make_settings())
    first = await trader2.handle_message(buy_message())
    assert first.decision == "copy"
    stub_redis.data.clear()  # release execution lock/cooldown for the retest
    second = await trader2.handle_message(buy_message())
    assert second.decision == "skip"
    gates = {r["gate"]: r["passed"] for r in second.reasons}
    assert gates["no_duplicate_position"] is False


async def test_unknown_wallet_skips_gracefully(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(factory, stub_redis, make_settings())
    evaluation = await trader.handle_message(buy_message())
    assert evaluation.decision == "skip"
    row = (await db_session.execute(select(TradeDecision))).scalar_one()
    assert row.reasons[0]["gate"] == "known_entities"
    assert row.leader_wallet_id is None and row.token_id is None  # no sentinel 0


async def test_stranger_sell_does_not_close_position(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    await seed_market(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(factory, stub_redis, make_settings())
    assert (await trader.handle_message(buy_message())).decision == "copy"

    # An unrelated wallet sells the same token: our position must stay open.
    stranger_sell = json.dumps({
        "signature": "stranger-sig", "wallet": "Str4nger111111111111111111111111111111111111",
        "token_mint": MINT, "side": "sell", "quote_amount": "3",
        "quote_mint": WSOL_MINT, "block_time": (NOW + timedelta(minutes=5)).isoformat(),
    })
    await trader.handle_message(stranger_sell)
    db_session.expire_all()
    position = (await db_session.execute(select(CopyPosition))).scalar_one()
    assert position.status == "open"  # stranger's sell ignored

    # The actual leader's sell does close it.
    await trader.handle_message(sell_message())
    db_session.expire_all()
    position = (await db_session.execute(select(CopyPosition))).scalar_one()
    assert position.status == "closed"


async def test_rug_gate_blocks_and_notifies(
    db_session: AsyncSession, stub_redis: StubRedis
) -> None:
    """With no RPC the probe reports unknown authorities; fail-closed must
    hard-block the copy and emit exactly one risk_blocked notification."""
    await seed_market(db_session)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(
        factory, stub_redis, make_settings(rug_check_enabled=True, rug_fail_closed=True)
    )

    evaluation = await trader.handle_message(buy_message())
    assert evaluation is not None and evaluation.decision == "skip"
    rug = next(r for r in evaluation.reasons if r["gate"] == "token_rug_risk")
    assert rug["passed"] is False
    assert "HARD BLOCK" in rug["note"]

    kinds = [json.loads(m)["kind"] for _, m in stub_redis.published]
    assert kinds.count("risk_blocked") == 1

    assessment = (
        await db_session.execute(select(TokenRiskAssessment))
    ).scalars().first()
    assert assessment is not None and assessment.hard_blocked is True


async def test_unknown_mcap_passes_by_default(db_session: AsyncSession, stub_redis: StubRedis) -> None:
    """A token whose supply was never backfilled (mcap None) must still be
    copyable — fail-on-unknown silently blocked every fresh token."""
    wallet, token = await seed_market(db_session)
    # Wipe mcap from the latest snapshot.
    from sqlalchemy import update

    from app.db.models import TokenSnapshot as TS

    await db_session.execute(update(TS).values(market_cap_usd=None))
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    trader = make_trader(factory, stub_redis, make_settings())
    evaluation = await trader.handle_message(buy_message())
    assert evaluation is not None and evaluation.decision == "copy"
    mcap_gate = next(r for r in evaluation.reasons if r["gate"] == "market_cap_band")
    assert mcap_gate["passed"] is True and "unknown" in mcap_gate["note"]

    # Strict mode blocks the same trade.
    trader2 = make_trader(factory, stub_redis, make_settings(copy_require_market_cap=True))
    evaluation2 = await trader2.handle_message(buy_message())
    assert evaluation2 is not None and evaluation2.decision == "skip"
