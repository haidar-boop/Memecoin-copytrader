"""SOL price parsing and market snapshot aggregate tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FailedTransaction, MarketSnapshot, Token, Trade
from app.enrichment import market
from app.ingestion.programs import USDC_MINT, WSOL_MINT
from app.services.redis import SOL_PRICE_KEY

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
PRICE_URL = "https://price.test/v2?ids=sol"


class StubRedis:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = str(value)
        return True


def make_fetch(payload: object) -> tuple[market.PriceFetch, list[str]]:
    calls: list[str] = []

    async def fetch(url: str) -> object:
        calls.append(url)
        return payload

    return fetch, calls


def valid_payload(price: object) -> dict:
    return {"data": {WSOL_MINT: {"id": WSOL_MINT, "type": "derivedPrice", "price": price}}}


def make_trade(
    *, signature: str, wallet_id: int, quote_amount: str, quote_mint: str, at: datetime
) -> Trade:
    return Trade(
        signature=signature,
        event_index=0,
        block_time=at,
        slot=1,
        wallet_id=wallet_id,
        token_id=1,
        dex="pumpfun",
        side="buy",
        token_amount=Decimal("100"),
        quote_amount=Decimal(quote_amount),
        quote_mint=quote_mint,
        price_quote=Decimal(quote_amount) / Decimal("100"),
    )


async def persist(session: AsyncSession, *rows: object) -> None:
    """Insert rows one at a time: batched ORM inserts of tz-aware composite
    PKs trip SQLAlchemy's RETURNING sentinel matching on SQLite."""
    for row in rows:
        session.add(row)
        await session.flush()


async def seed_market(session: AsyncSession) -> None:
    session.add_all(
        [
            Token(mint="NewMint111111111111111111111111111111111111",
                  first_seen_at=T0 - timedelta(minutes=30)),
            Token(mint="O1dMint111111111111111111111111111111111111",
                  first_seen_at=T0 - timedelta(hours=3)),
        ]
    )
    await persist(
        session,
        make_trade(signature="m1", wallet_id=1, quote_amount="2",
                   quote_mint=WSOL_MINT, at=T0 - timedelta(minutes=10)),
        make_trade(signature="m2", wallet_id=2, quote_amount="1.5",
                   quote_mint=WSOL_MINT, at=T0 - timedelta(minutes=20)),
        # USDC-quoted: counted as a trade, excluded from SOL volume.
        make_trade(signature="m3", wallet_id=1, quote_amount="30",
                   quote_mint=USDC_MINT, at=T0 - timedelta(minutes=15)),
        # Outside the 1h window.
        make_trade(signature="m4", wallet_id=1, quote_amount="9",
                   quote_mint=WSOL_MINT, at=T0 - timedelta(hours=2)),
        FailedTransaction(signature="f1", block_time=T0 - timedelta(minutes=10), slot=1),
        FailedTransaction(signature="f2", block_time=T0 - timedelta(minutes=90), slot=1),
    )
    await session.commit()


# --- pure price parsing -----------------------------------------------------


def test_parse_sol_price_valid_string() -> None:
    assert market.parse_sol_price(valid_payload("150.25")) == Decimal("150.25")


def test_parse_sol_price_valid_numeric() -> None:
    assert market.parse_sol_price(valid_payload(150.25)) == Decimal("150.25")


def test_parse_sol_price_falls_back_to_first_entry() -> None:
    assert market.parse_sol_price({"data": {"SOL": {"price": "149.5"}}}) == Decimal("149.5")


def test_parse_sol_price_malformed_variants() -> None:
    malformed: list[object] = [
        None,
        [],
        "wat",
        {},
        {"data": None},
        {"data": []},
        {"data": "wat"},
        {"data": {}},
        {"data": {WSOL_MINT: None}},
        {"data": {WSOL_MINT: {}}},
        {"data": {WSOL_MINT: {"price": None}}},
        {"data": {WSOL_MINT: {"price": "abc"}}},
        {"data": {WSOL_MINT: {"price": "NaN"}}},
        {"data": {WSOL_MINT: {"price": "0"}}},
        {"data": {WSOL_MINT: {"price": "-3"}}},
    ]
    for payload in malformed:
        assert market.parse_sol_price(payload) is None, repr(payload)


# --- run_once ---------------------------------------------------------------


async def test_run_once_stores_price_and_aggregates(db_session: AsyncSession) -> None:
    await seed_market(db_session)
    redis = StubRedis()
    fetch, calls = make_fetch(valid_payload("150.25"))

    snap = await market.run_once(
        db_session, redis, price_url=PRICE_URL, fetch=fetch, now=T0
    )

    assert calls == [PRICE_URL]
    assert redis.data[SOL_PRICE_KEY] == "150.25"
    assert snap.sol_price_usd == Decimal("150.25")
    assert snap.trades_1h == 3
    assert snap.volume_sol_1h == Decimal("3.5")  # WSOL-quoted only
    assert snap.active_wallets_1h == 2
    assert snap.tokens_launched_1h == 1
    assert snap.failed_tx_1h == 1
    stored = (await db_session.execute(select(MarketSnapshot))).scalar_one()
    assert stored.sol_price_usd == Decimal("150.25")


async def test_run_once_http_error_keeps_previous_price(db_session: AsyncSession) -> None:
    redis = StubRedis({SOL_PRICE_KEY: "140"})

    async def failing_fetch(url: str) -> object:
        raise httpx.ConnectError("connection refused")

    snap = await market.run_once(
        db_session, redis, price_url=PRICE_URL, fetch=failing_fetch, now=T0
    )

    assert snap.sol_price_usd == Decimal("140")
    assert redis.data[SOL_PRICE_KEY] == "140"  # untouched by the failed fetch


async def test_run_once_malformed_payload_keeps_previous_price(
    db_session: AsyncSession,
) -> None:
    redis = StubRedis({SOL_PRICE_KEY: "141.5"})
    fetch, _calls = make_fetch({"data": {}})

    snap = await market.run_once(
        db_session, redis, price_url=PRICE_URL, fetch=fetch, now=T0
    )

    assert snap.sol_price_usd == Decimal("141.5")
    assert redis.data[SOL_PRICE_KEY] == "141.5"


async def test_run_once_malformed_payload_without_previous_price(
    db_session: AsyncSession,
) -> None:
    redis = StubRedis()
    fetch, _calls = make_fetch({"data": "wat"})

    snap = await market.run_once(
        db_session, redis, price_url=PRICE_URL, fetch=fetch, now=T0
    )

    assert snap.sol_price_usd is None
    assert SOL_PRICE_KEY not in redis.data
    # Empty database: aggregates are zero, and the row still lands.
    assert snap.trades_1h == 0
    assert snap.volume_sol_1h == Decimal(0)
    assert snap.active_wallets_1h == 0
    assert snap.tokens_launched_1h == 0
    assert snap.failed_tx_1h == 0
    stored = (await db_session.execute(select(MarketSnapshot))).scalar_one()
    assert stored.sol_price_usd is None
    assert stored.trades_1h == 0
