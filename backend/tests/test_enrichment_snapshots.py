"""Token snapshot math plus liquidity/holders/wallet-balance job tests.

All amounts are binary-exact fractions so SQLite's float NUMERIC storage
round-trips them without error and equality assertions stay exact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DexPool,
    Position,
    Token,
    TokenSnapshot,
    Trade,
    Wallet,
    WalletSnapshot,
)
from app.enrichment import holders, liquidity, snapshots, wallet_balances
from app.ingestion.programs import TOKEN_PROGRAM, USDC_MINT, WSOL_MINT
from app.services.redis import HOLDERS_KEY_PREFIX, SOL_PRICE_KEY

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)

MEME = "MemeMint11111111111111111111111111111111111"
STALE = "Sta1eMint1111111111111111111111111111111111"
OTHER = "OtherMint1111111111111111111111111111111111"
CURVE = "CurvePoo1111111111111111111111111111111111"
AMM_POOL = "AmmPoo1111111111111111111111111111111111111"
USDC_POOL = "UsdcPoo1111111111111111111111111111111111111"
WSOL_VAULT = "Wso1Vau1t11111111111111111111111111111111111"
USDC_VAULT = "UsdcVau1t1111111111111111111111111111111111"
WALLET_A = "Wa11etAaa1111111111111111111111111111111111"
WALLET_B = "Wa11etBbb1111111111111111111111111111111111"


class StubRedis:
    """Tiny dict-backed stand-in for the async Redis client."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = str(value)
        self.ttls[key] = ex
        return True


class StubRpc:
    def __init__(self) -> None:
        self.balances: dict[str, int] = {}
        self.vault_balances: dict[str, dict] = {}
        self.holder_counts: dict[str, int] = {}
        self.program_account_calls: list[tuple[str, list[dict] | None]] = []
        self.accounts: dict[str, dict] = {}
        self.multiple_calls: list[list[str]] = []

    async def get_balance(
        self, pubkey: str, budget_exempt: bool | None = None
    ) -> int | None:
        return self.balances.get(pubkey)

    async def get_token_account_balance(
        self, account: str, budget_exempt: bool | None = None
    ) -> dict | None:
        return self.vault_balances.get(account)

    async def get_program_accounts_count(
        self, program_id: str, filters: list[dict] | None = None
    ) -> int:
        self.program_account_calls.append((program_id, filters))
        mint = ""
        for entry in filters or []:
            memcmp = entry.get("memcmp")
            if memcmp:
                mint = memcmp.get("bytes", "")
        return self.holder_counts.get(mint, 0)

    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict | None]:
        self.multiple_calls.append(list(pubkeys))
        return [self.accounts.get(key) for key in pubkeys]


async def persist(session: AsyncSession, *rows: object) -> None:
    """Insert rows one at a time: batched ORM inserts of tz-aware composite
    PKs trip SQLAlchemy's RETURNING sentinel matching on SQLite."""
    for row in rows:
        session.add(row)
        await session.flush()


def make_trade(
    *,
    signature: str,
    token_id: int,
    wallet_id: int,
    side: str,
    token_amount: str,
    quote_amount: str,
    at: datetime,
    quote_mint: str = WSOL_MINT,
) -> Trade:
    token = Decimal(token_amount)
    quote = Decimal(quote_amount)
    return Trade(
        signature=signature,
        event_index=0,
        block_time=at,
        slot=1,
        wallet_id=wallet_id,
        token_id=token_id,
        dex="pumpfun",
        side=side,
        token_amount=token,
        quote_amount=quote,
        quote_mint=quote_mint,
        price_quote=quote / token,
    )


async def seed_active_token(session: AsyncSession) -> Token:
    """One token with WSOL/USDC trades across the 5m/1h/24h windows."""
    token = Token(mint=MEME, first_seen_at=T0 - timedelta(days=1), supply=Decimal("1000000"))
    session.add(token)
    await session.flush()
    session.add(
        DexPool(
            address=CURVE,
            dex="pumpfun",
            token_id=token.id,
            base_mint=MEME,
            quote_mint=WSOL_MINT,
            first_seen_at=T0 - timedelta(days=1),
        )
    )
    await persist(
        session,
        # 5m window: WSOL quote sum 4, token sum 1600 -> vwap 0.0025.
        make_trade(signature="s1", token_id=token.id, wallet_id=2, side="buy",
                   token_amount="500", quote_amount="1.5", at=T0 - timedelta(minutes=3)),
        make_trade(signature="s2", token_id=token.id, wallet_id=1, side="buy",
                   token_amount="1000", quote_amount="2", at=T0 - timedelta(minutes=2)),
        # Latest WSOL-quoted trade: price 0.5/100 = 0.005.
        make_trade(signature="s3", token_id=token.id, wallet_id=1, side="sell",
                   token_amount="100", quote_amount="0.5", at=T0 - timedelta(minutes=1)),
        # Newer but USDC-quoted: counts as a trade, not toward SOL math.
        make_trade(signature="s4", token_id=token.id, wallet_id=1, side="buy",
                   token_amount="200", quote_amount="30", quote_mint=USDC_MINT,
                   at=T0 - timedelta(seconds=30)),
        # 1h window only.
        make_trade(signature="s5", token_id=token.id, wallet_id=2, side="buy",
                   token_amount="400", quote_amount="1", at=T0 - timedelta(minutes=30)),
        # 24h window only.
        make_trade(signature="s6", token_id=token.id, wallet_id=1, side="sell",
                   token_amount="100", quote_amount="0.25", at=T0 - timedelta(hours=10)),
    )
    await session.commit()
    return token


async def test_snapshot_math(db_session: AsyncSession) -> None:
    token = await seed_active_token(db_session)
    # A token whose only trade is outside the active interval gets no row.
    stale = Token(mint=STALE, first_seen_at=T0 - timedelta(days=2))
    db_session.add(stale)
    await db_session.flush()
    db_session.add(
        make_trade(signature="old", token_id=stale.id, wallet_id=3, side="buy",
                   token_amount="10", quote_amount="0.5", at=T0 - timedelta(hours=2))
    )
    await db_session.commit()

    rpc = StubRpc()
    rpc.balances[CURVE] = 85_000_000_000  # 85 SOL on the bonding curve
    redis = StubRedis({SOL_PRICE_KEY: "150", HOLDERS_KEY_PREFIX + MEME: "321"})

    inserted = await snapshots.run_once(
        db_session, rpc, redis, interval_seconds=300, active_token_limit=50, now=T0
    )

    assert inserted == 1
    snap = (await db_session.execute(select(TokenSnapshot))).scalar_one()
    assert snap.token_id == token.id
    assert snap.price_sol == Decimal("0.005")
    assert snap.vwap_sol_5m == Decimal("0.0025")
    assert snap.volume_sol_5m == Decimal("4")
    assert snap.volume_sol_1h == Decimal("5")
    assert snap.volume_sol_24h == Decimal("5.25")
    assert snap.trades_5m == 4  # includes the USDC-quoted trade
    assert snap.trades_1h == 5
    assert snap.buyers_5m == 2  # wallets 1 and 2 bought; the sell does not count
    assert snap.holder_count == 321
    assert snap.liquidity_sol == Decimal("85")
    assert snap.price_usd == Decimal("0.75")  # 0.005 * 150
    assert snap.market_cap_usd == Decimal("750000")  # 0.75 * 1_000_000


async def test_snapshot_rows_are_append_only(db_session: AsyncSession) -> None:
    await seed_active_token(db_session)
    rpc = StubRpc()
    redis = StubRedis()

    first = await snapshots.run_once(
        db_session, rpc, redis, interval_seconds=300, active_token_limit=50, now=T0
    )
    second = await snapshots.run_once(
        db_session, rpc, redis, interval_seconds=300, active_token_limit=50,
        now=T0 + timedelta(seconds=60),
    )

    assert first == 1 and second == 1
    rows = (await db_session.execute(select(TokenSnapshot))).scalars().all()
    assert len(rows) == 2
    assert len({row.ts for row in rows}) == 2  # two distinct snapshot instants


async def test_snapshot_without_caches_leaves_optional_fields_null(
    db_session: AsyncSession,
) -> None:
    await seed_active_token(db_session)
    rpc = StubRpc()  # no curve balance registered -> liquidity unmeasurable
    redis = StubRedis()  # no SOL price, no holder cache

    await snapshots.run_once(
        db_session, rpc, redis, interval_seconds=300, active_token_limit=50, now=T0
    )

    snap = (await db_session.execute(select(TokenSnapshot))).scalar_one()
    assert snap.price_sol == Decimal("0.005")
    assert snap.price_usd is None
    assert snap.market_cap_usd is None
    assert snap.holder_count is None
    assert snap.liquidity_sol is None
    assert snap.volume_sol_5m == Decimal("4")


async def test_fetch_liquidity_prefers_deepest_measurable_pool(
    db_session: AsyncSession,
) -> None:
    token = Token(mint=MEME, first_seen_at=T0)
    other = Token(mint=OTHER, first_seen_at=T0)
    db_session.add_all([token, other])
    await db_session.flush()
    db_session.add_all(
        [
            DexPool(address=CURVE, dex="pumpfun", token_id=token.id, base_mint=MEME,
                    quote_mint=WSOL_MINT, first_seen_at=T0),
            DexPool(address=AMM_POOL, dex="pumpswap", token_id=token.id, base_mint=MEME,
                    quote_mint=WSOL_MINT, quote_vault=WSOL_VAULT, first_seen_at=T0),
            # USDC-quoted: not measurable in SOL, must stay absent.
            DexPool(address=USDC_POOL, dex="raydium_amm", token_id=other.id, base_mint=OTHER,
                    quote_mint=USDC_MINT, quote_vault=USDC_VAULT, first_seen_at=T0),
        ]
    )
    await db_session.commit()

    rpc = StubRpc()
    rpc.balances[CURVE] = 2_000_000_000  # 2 SOL
    rpc.vault_balances[WSOL_VAULT] = {
        "amount": "12500000000",
        "decimals": 9,
        "uiAmountString": "12.5",
    }

    result = await liquidity.fetch_liquidity(db_session, rpc, [token.id, other.id])

    assert result == {token.id: Decimal("12.5")}  # deepest pool wins, USDC pool absent


async def test_holders_job_caches_counts_with_ttl(db_session: AsyncSession) -> None:
    token = Token(mint=MEME, first_seen_at=T0)
    db_session.add(token)
    await db_session.flush()
    db_session.add(
        make_trade(signature="h1", token_id=token.id, wallet_id=1, side="buy",
                   token_amount="10", quote_amount="0.5", at=T0 - timedelta(minutes=1))
    )
    await db_session.commit()

    rpc = StubRpc()
    rpc.holder_counts[MEME] = 42
    redis = StubRedis()

    cached = await holders.run_once(
        db_session, rpc, redis,
        active_since=T0 - timedelta(minutes=15), limit=10, cache_ttl_seconds=1800,
    )

    assert cached == 1
    assert redis.data[HOLDERS_KEY_PREFIX + MEME] == "42"
    assert redis.ttls[HOLDERS_KEY_PREFIX + MEME] == 1800
    program_id, filters = rpc.program_account_calls[0]
    assert program_id == TOKEN_PROGRAM
    assert filters == [{"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": MEME}}]


async def test_wallet_balance_refresh_updates_and_snapshots(
    db_session: AsyncSession,
) -> None:
    recent = Wallet(address=WALLET_A, first_seen_at=T0 - timedelta(days=1),
                    last_seen_at=T0 - timedelta(minutes=1))
    idle = Wallet(address=WALLET_B, first_seen_at=T0 - timedelta(days=1),
                  last_seen_at=T0 - timedelta(minutes=30))
    token = Token(mint=MEME, first_seen_at=T0)
    db_session.add_all([recent, idle, token])
    await db_session.flush()
    db_session.add(Position(wallet_id=recent.id, token_id=token.id, status="open", opened_at=T0))
    await db_session.commit()

    rpc = StubRpc()
    rpc.accounts[WALLET_A] = {"lamports": 5_000_000_000, "owner": "11111111111111111111111111111111"}

    updated = await wallet_balances.run_once(
        db_session, rpc, since=T0 - timedelta(minutes=5), batch=100, now=T0
    )

    assert updated == 1
    assert recent.sol_balance_lamports == 5_000_000_000
    assert recent.balance_updated_at == T0
    assert idle.sol_balance_lamports is None  # not seen since the watermark
    snaps = (await db_session.execute(select(WalletSnapshot))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].wallet_id == recent.id
    assert snaps[0].sol_balance_lamports == 5_000_000_000
    assert snaps[0].open_position_count == 1
    assert rpc.multiple_calls == [[WALLET_A]]
