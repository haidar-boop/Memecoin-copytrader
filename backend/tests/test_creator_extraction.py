"""Tests for pump.fun creator extraction in the ingest writer.

A pump.fun creation transaction logs "Program log: Instruction: Create" and
its fee payer is the token creator. The writer stamps that on the Token row
exactly once — later trades (or replayed create txs from other payers) must
never overwrite it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token
from app.ingestion import writer
from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.programs import WSOL_MINT

CREATOR = "Creator1111111111111111111111111111111111111"
BUYER = "Buyer11111111111111111111111111111111111111"
MEME = "MemeMint11111111111111111111111111111111111111"

CREATE_LOGS = [
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
    "Program log: Instruction: Create",
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success",
]
BUY_LOGS = [
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
    "Program log: Instruction: Buy",
    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success",
]


def make_tx(payer: str, logs: list[str], signature: str) -> dict:
    """Minimal getTransaction dict shaped like the pumpfun fixtures."""
    return {
        "blockTime": 1753000000,
        "slot": 352100400,
        "meta": {
            "err": None,
            "fee": 105000,
            "logMessages": list(logs),
            "preBalances": [2_000_000_000],
            "postBalances": [1_999_895_000],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "innerInstructions": [],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    {
                        "pubkey": payer,
                        "signer": True,
                        "source": "transaction",
                        "writable": True,
                    }
                ],
                "instructions": [],
            },
            "signatures": [signature],
        },
    }


def make_event(wallet: str, signature: str) -> SwapEvent:
    return SwapEvent(
        signature=signature,
        slot=352100400,
        block_time=datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC),
        wallet=wallet,
        dex=Dex.PUMPFUN,
        side=Side.BUY,
        token_mint=MEME,
        quote_mint=WSOL_MINT,
        token_amount=Decimal("1000"),
        quote_amount=Decimal("1"),
        price_quote_per_token=Decimal("0.001"),
    )


async def get_token(session: AsyncSession) -> Token:
    return (await session.execute(select(Token).where(Token.mint == MEME))).scalar_one()


async def test_create_tx_sets_creator(db_session: AsyncSession) -> None:
    tx = make_tx(CREATOR, CREATE_LOGS, "create-sig")
    await writer.persist_parsed_transaction(
        db_session, tx, [make_event(CREATOR, "create-sig")], sol_price_usd=None
    )
    await db_session.commit()
    token = await get_token(db_session)
    assert token.creator == CREATOR


async def test_later_trade_does_not_set_or_overwrite_creator(
    db_session: AsyncSession,
) -> None:
    # Creation first, then a plain buy from another wallet.
    await writer.persist_parsed_transaction(
        db_session,
        make_tx(CREATOR, CREATE_LOGS, "create-sig"),
        [make_event(CREATOR, "create-sig")],
        sol_price_usd=None,
    )
    await writer.persist_parsed_transaction(
        db_session,
        make_tx(BUYER, BUY_LOGS, "buy-sig"),
        [make_event(BUYER, "buy-sig")],
        sol_price_usd=None,
    )
    await db_session.commit()
    token = await get_token(db_session)
    assert token.creator == CREATOR


async def test_buy_without_create_log_leaves_creator_null(
    db_session: AsyncSession,
) -> None:
    await writer.persist_parsed_transaction(
        db_session,
        make_tx(BUYER, BUY_LOGS, "buy-sig"),
        [make_event(BUYER, "buy-sig")],
        sol_price_usd=None,
    )
    await db_session.commit()
    token = await get_token(db_session)
    assert token.creator is None


async def test_existing_creator_never_overwritten_by_second_create(
    db_session: AsyncSession,
) -> None:
    # A second create-shaped tx (replay/edge) from a different payer must not
    # clobber the recorded creator.
    await writer.persist_parsed_transaction(
        db_session,
        make_tx(CREATOR, CREATE_LOGS, "create-sig"),
        [make_event(CREATOR, "create-sig")],
        sol_price_usd=None,
    )
    await writer.persist_parsed_transaction(
        db_session,
        make_tx(BUYER, CREATE_LOGS, "create-sig-2"),
        [make_event(BUYER, "create-sig-2")],
        sol_price_usd=None,
    )
    await db_session.commit()
    token = await get_token(db_session)
    assert token.creator == CREATOR
