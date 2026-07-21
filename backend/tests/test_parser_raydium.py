"""Tests for the Raydium parser adapter (AMM v4 / CPMM / CLMM).

Every swap fixture is asserted through both entry points: the registry's
``parse_transaction`` and a direct ``RaydiumParser().parse`` call. Fixture
balances are internally consistent (lamport sums differ by exactly the fee;
WSOL account lamports carry the wrapped amount plus rent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.parsers import parse_transaction
from app.ingestion.parsers.raydium import RaydiumParser
from app.ingestion.programs import RAYDIUM_AMM_V4, RAYDIUM_CLMM, RAYDIUM_CPMM, WSOL_MINT

# Pubkeys baked into the fixtures (deterministically generated).
WALLET = "5UFfuWfofbs2xPS9jnwP2rG6LmEpQ95xSfYkoFpsFzxR"
TOKEN_MINT = "FxN4hr4jwCJnnpNTy4gUQnWagRSNEdKn9HrnSpxxSK3v"
PUP_MINT = "CtzKjC3Y3kwkNrydud4WgQdbJT5Jn86WZiLj99wzt8MD"
DOGE2_MINT = "BCAR74LgkWUXvaE5BE1nb5jbL7bCbH9gZMxuiAMXPxYY"
AMM_POOL = "DVHCrrphWX85yJFFf97CtQxTw3fhByyyHRgVNCSPXMXU"
CPMM_POOL = "Bh7Ycy85E46E9maNuvr43ukwSexAu8TNCo2a5mftaxZd"
CLMM_POOL = "C5g54EFnHkx7Qk3r3Z4dZ2tVJWCoALkuakUB6JjsXp4T"

# Each swap fixture must produce identical events via the registry and via a
# direct parser call.
PARSE_PATHS = [
    pytest.param(lambda tx: RaydiumParser().parse(tx), id="direct"),
    pytest.param(parse_transaction, id="registry"),
]


def _single_event(events: list[SwapEvent]) -> SwapEvent:
    assert len(events) == 1
    return events[0]


def _assert_common(event: SwapEvent, tx: dict) -> None:
    assert event.signature == tx["transaction"]["signatures"][0]
    assert event.slot == tx["slot"]
    assert event.block_time == datetime.fromtimestamp(tx["blockTime"], tz=UTC)
    assert event.wallet == WALLET
    assert event.event_index == 0
    assert event.aggregator is None


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_amm_v4_buy(load_tx, parse) -> None:
    tx = load_tx("raydium_amm_v4_buy")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.dex is Dex.RAYDIUM_AMM
    assert event.dex.value == "raydium_amm"
    assert event.program_id == RAYDIUM_AMM_V4
    assert event.side is Side.BUY
    assert event.token_mint == TOKEN_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("3000")
    assert event.quote_amount == Decimal("1.5")
    assert event.price_quote_per_token == Decimal("0.0005")
    assert event.pool_address == AMM_POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_amm_v4_sell(load_tx, parse) -> None:
    # Routed through an opaque third-party router: the Raydium swap only
    # appears as an inner (CPI) instruction, and the pool must still be found.
    tx = load_tx("raydium_amm_v4_sell")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.dex is Dex.RAYDIUM_AMM
    assert event.dex.value == "raydium_amm"
    assert event.program_id == RAYDIUM_AMM_V4
    assert event.side is Side.SELL
    assert event.token_mint == TOKEN_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("3000")
    assert event.quote_amount == Decimal("1.2")
    assert event.price_quote_per_token == Decimal("0.0004")
    assert event.pool_address == AMM_POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_cpmm_buy(load_tx, parse) -> None:
    tx = load_tx("raydium_cpmm_buy")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.dex is Dex.RAYDIUM_CPMM
    assert event.dex.value == "raydium_cpmm"
    assert event.program_id == RAYDIUM_CPMM
    assert event.side is Side.BUY
    assert event.token_mint == PUP_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("42000")
    assert event.quote_amount == Decimal("0.21")
    assert event.price_quote_per_token == Decimal("0.000005")
    assert event.pool_address == CPMM_POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_clmm_sell(load_tx, parse) -> None:
    tx = load_tx("raydium_clmm_sell")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.dex is Dex.RAYDIUM_CLMM
    assert event.dex.value == "raydium_clmm"
    assert event.program_id == RAYDIUM_CLMM
    assert event.side is Side.SELL
    assert event.token_mint == DOGE2_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("10000")
    assert event.quote_amount == Decimal("0.8")
    assert event.price_quote_per_token == Decimal("0.00008")
    assert event.pool_address == CLMM_POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_amm_v4_lp_deposit_is_not_a_swap(load_tx, parse) -> None:
    # Deposit-and-stake: token and WSOL both leave the wallet (same-sign
    # deltas, LP tokens land in a farm-owned account) -> no swap events.
    tx = load_tx("raydium_amm_v4_deposit")
    assert parse(tx) == []


def test_matches_all_raydium_fixtures(load_tx) -> None:
    parser = RaydiumParser()
    for name in (
        "raydium_amm_v4_buy",
        "raydium_amm_v4_sell",
        "raydium_cpmm_buy",
        "raydium_clmm_sell",
        "raydium_amm_v4_deposit",
    ):
        assert parser.matches(load_tx(name)), name


def test_does_not_match_foreign_transaction() -> None:
    tx = {
        "slot": 1,
        "blockTime": 1_750_000_000,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": ["3xyz"],
            "message": {
                "accountKeys": [
                    {"pubkey": WALLET, "signer": True, "writable": True, "source": "transaction"}
                ],
                "instructions": [
                    {"programId": "11111111111111111111111111111111", "accounts": [], "data": ""}
                ],
            },
        },
    }
    parser = RaydiumParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []


def test_failed_transaction_yields_nothing(load_tx) -> None:
    tx = load_tx("raydium_amm_v4_buy")
    tx["meta"]["err"] = {"InstructionError": [0, {"Custom": 30}]}
    parser = RaydiumParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []
    assert parse_transaction(tx) == []


def test_undecodable_swap_data_gives_no_pool_but_still_parses(load_tx) -> None:
    # Corrupt the instruction data: venue attribution must survive (program
    # id is still present) but the pool must be dropped rather than guessed.
    tx = load_tx("raydium_amm_v4_buy")
    tx["transaction"]["message"]["instructions"][0]["data"] = "0OIl"  # invalid base58
    event = _single_event(RaydiumParser().parse(tx))
    assert event.dex is Dex.RAYDIUM_AMM
    assert event.program_id == RAYDIUM_AMM_V4
    assert event.pool_address is None
    assert event.token_amount == Decimal("3000")
