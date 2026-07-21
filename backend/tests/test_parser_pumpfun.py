"""Tests for the pump.fun parser (bonding curve + PumpSwap AMM).

Fixtures are crafted so the bonding-curve trades settle in native SOL (no
WSOL token-balance entries for the trader), forcing the fee-payer
lamport-delta fallback in ``util.infer_swap_events``, with balances tuned so
the fee-added-back delta is an exact round quote amount.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingestion.events import Dex, Side
from app.ingestion.parsers import parse_transaction
from app.ingestion.parsers.pumpfun import PumpFunParser
from app.ingestion.programs import PUMPFUN, PUMPSWAP, WSOL_MINT

# Addresses baked into the fixtures (see tests/fixtures/pumpfun_*.json).
TRADER = "4W13wLVG8obJz6DSXFvGoNTFdHakgdaJpbWuhau1AReM"
MINT_BONDING = "SeQSoj4JAdwh7i6xGjVLkuyR83ZH6QUzGNV4DoNxXkn"
BONDING_CURVE = "2oCEitAjaBaNZSDyiqbPuAiR8kZbMv2FD7uKoeYhpzni"
TRADER2 = "6aJ3ZGyYEmkouwzUWAMFHLvNoAynXcZF9Z1pXuCyZEC9"
MINT_SWAP = "Gn1uz78tfvarBWapkhwDHiEZzG1B5mbM38GnUR4vMnGJ"
POOL = "7Yr3Zj1SSk2fQVKWoDzXvBizpGoRcHpvEZ7QGbGqmhQV"

LAMPORTS_PER_SOL = Decimal(10) ** 9


@pytest.fixture
def parser() -> PumpFunParser:
    return PumpFunParser()


def _has_wsol_entry_for(tx: dict, owner: str) -> bool:
    meta = tx["meta"]
    return any(
        entry["mint"] == WSOL_MINT and entry["owner"] == owner
        for entry in [*meta["preTokenBalances"], *meta["postTokenBalances"]]
    )


def test_matches_both_pump_programs(parser, load_tx) -> None:
    assert parser.program_ids == frozenset({PUMPFUN, PUMPSWAP})
    assert parser.matches(load_tx("pumpfun_bonding_buy"))
    assert parser.matches(load_tx("pumpfun_bonding_sell"))
    assert parser.matches(load_tx("pumpfun_pumpswap_buy"))
    assert not parser.matches(load_tx("pumpfun_failed"))


def test_bonding_curve_buy_native_sol(parser, load_tx) -> None:
    tx = load_tx("pumpfun_bonding_buy")
    # Guard the fixture's intent: trader settles in native SOL, no WSOL leg.
    assert not _has_wsol_entry_for(tx, TRADER)

    events = parser.parse(tx)
    assert len(events) == 1
    event = events[0]

    assert event.dex is Dex.PUMPFUN
    assert event.program_id == PUMPFUN
    assert event.side is Side.BUY
    assert event.wallet == TRADER
    assert event.token_mint == MINT_BONDING
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("17500000")
    # Quote = fee payer lamport delta with the tx fee added back, exactly.
    meta = tx["meta"]
    spent_lamports = meta["preBalances"][0] - meta["postBalances"][0] - meta["fee"]
    assert event.quote_amount == Decimal(spent_lamports) / LAMPORTS_PER_SOL
    assert event.quote_amount == Decimal("0.5")
    assert event.price_quote_per_token == Decimal("0.5") / Decimal("17500000")
    assert event.pool_address == BONDING_CURVE
    assert event.signature == tx["transaction"]["signatures"][0]
    assert event.slot == tx["slot"]
    assert event.block_time == datetime.fromtimestamp(tx["blockTime"], tz=UTC)


def test_bonding_curve_buy_via_registry(load_tx) -> None:
    events = parse_transaction(load_tx("pumpfun_bonding_buy"))
    assert len(events) == 1
    event = events[0]
    assert event.dex is Dex.PUMPFUN
    assert event.side is Side.BUY
    assert event.quote_mint == WSOL_MINT
    assert event.quote_amount == Decimal("0.5")
    assert event.token_amount == Decimal("17500000")
    assert event.event_index == 0
    assert event.aggregator is None


def test_bonding_curve_sell_native_sol(parser, load_tx) -> None:
    tx = load_tx("pumpfun_bonding_sell")
    assert not _has_wsol_entry_for(tx, TRADER)

    events = parser.parse(tx)
    assert len(events) == 1
    event = events[0]

    assert event.dex is Dex.PUMPFUN
    assert event.program_id == PUMPFUN
    assert event.side is Side.SELL
    assert event.wallet == TRADER
    assert event.token_mint == MINT_BONDING
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("5000000")
    # Quote = SOL received: fee payer lamport delta with the tx fee added back.
    meta = tx["meta"]
    received_lamports = meta["postBalances"][0] - meta["preBalances"][0] + meta["fee"]
    assert event.quote_amount == Decimal(received_lamports) / LAMPORTS_PER_SOL
    assert event.quote_amount == Decimal("0.2")
    assert event.price_quote_per_token == Decimal("0.2") / Decimal("5000000")
    assert event.pool_address == BONDING_CURVE


def test_bonding_curve_sell_via_registry(load_tx) -> None:
    events = parse_transaction(load_tx("pumpfun_bonding_sell"))
    assert len(events) == 1
    event = events[0]
    assert event.dex is Dex.PUMPFUN
    assert event.side is Side.SELL
    assert event.quote_amount == Decimal("0.2")
    assert event.token_amount == Decimal("5000000")
    assert event.event_index == 0


def test_pumpswap_amm_trade(parser, load_tx) -> None:
    tx = load_tx("pumpfun_pumpswap_buy")
    events = parser.parse(tx)
    assert len(events) == 1
    event = events[0]

    assert event.dex is Dex.PUMPSWAP
    assert event.program_id == PUMPSWAP
    assert event.side is Side.BUY
    assert event.wallet == TRADER2
    assert event.token_mint == MINT_SWAP
    assert event.quote_mint == WSOL_MINT
    # AMM leg settles through WSOL token accounts, not native lamports.
    assert event.quote_amount == Decimal("0.35")
    assert event.token_amount == Decimal("1000000")
    assert event.pool_address == POOL


def test_pumpswap_amm_trade_via_registry(load_tx) -> None:
    events = parse_transaction(load_tx("pumpfun_pumpswap_buy"))
    assert len(events) == 1
    event = events[0]
    assert event.dex is Dex.PUMPSWAP
    assert event.program_id == PUMPSWAP
    assert event.pool_address == POOL
    assert event.aggregator is None


def test_failed_tx_yields_no_events(parser, load_tx) -> None:
    tx = load_tx("pumpfun_failed")
    assert tx["meta"]["err"] is not None
    assert parse_transaction(tx) == []
    assert parser.parse(tx) == []
    assert not parser.matches(tx)


def test_log_side_mismatch_keeps_delta_side(parser, load_tx) -> None:
    """Buy/Sell logs are a consistency signal; balance deltas stay the truth."""
    tx = copy.deepcopy(load_tx("pumpfun_bonding_buy"))
    tx["meta"]["logMessages"] = [
        "Program log: Instruction: Sell" if line == "Program log: Instruction: Buy" else line
        for line in tx["meta"]["logMessages"]
    ]
    events = parser.parse(tx)
    assert len(events) == 1
    assert events[0].side is Side.BUY
    assert events[0].quote_amount == Decimal("0.5")


def test_pool_address_none_when_unidentifiable(parser, load_tx) -> None:
    tx = copy.deepcopy(load_tx("pumpfun_bonding_buy"))
    for ix in tx["transaction"]["message"]["instructions"]:
        if ix["programId"] == PUMPFUN:
            ix["accounts"] = ix["accounts"][:3]  # bonding curve index gone
    events = parser.parse(tx)
    assert len(events) == 1
    assert events[0].pool_address is None
    assert events[0].quote_amount == Decimal("0.5")
