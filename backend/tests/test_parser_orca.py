"""Tests for the Orca Whirlpool parser adapter.

Every swap fixture is asserted through both entry points: the registry's
``parse_transaction`` and a direct ``OrcaWhirlpoolParser().parse`` call.
Fixture balances are internally consistent (lamport sums differ by exactly
the fee debited from the fee payer at index 0; WSOL account lamports carry
the wrapped amount plus rent; whirlpool vaults are owned by the pool PDA).
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.parsers import parse_transaction
from app.ingestion.parsers.orca import OrcaWhirlpoolParser
from app.ingestion.programs import ORCA_WHIRLPOOL, USDC_MINT, WSOL_MINT

# Pubkeys baked into the fixtures (deterministically generated).
WALLET = "5UFfuWfofbs2xPS9jnwP2rG6LmEpQ95xSfYkoFpsFzxR"
TOKEN_MINT = "J7up97rB1uhMgvZ42Eb5fJiVrXmsP4R468FPv5PPWCC3"
TOKEN2_MINT = "ErSSewTtXQ4BJrZYdNriPqyy6r6bGzeJWXxCxaKkmdRL"
POOL = "65JZLBRiyoZogJrtL3SQo4e9TNJtLNyw5f8QiuorXB89"
POOL_USDC = "FUucpsPZt2q5yN226gKxG3Zc8fwGJB6T4KdVDdfLSC8Z"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Each swap fixture must produce identical events via the registry and via a
# direct parser call.
PARSE_PATHS = [
    pytest.param(lambda tx: OrcaWhirlpoolParser().parse(tx), id="direct"),
    pytest.param(parse_transaction, id="registry"),
]


def _b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = _B58_ALPHABET[digit] + encoded
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + encoded


def _anchor_data(name: str, arg_bytes: int) -> str:
    """base58 instruction data: anchor discriminator plus zeroed args."""
    return _b58encode(hashlib.sha256(f"global:{name}".encode()).digest()[:8] + b"\x00" * arg_bytes)


def _single_event(events: list[SwapEvent]) -> SwapEvent:
    assert len(events) == 1
    return events[0]


def _assert_common(event: SwapEvent, tx: dict) -> None:
    assert event.signature == tx["transaction"]["signatures"][0]
    assert event.slot == tx["slot"]
    assert event.block_time == datetime.fromtimestamp(tx["blockTime"], tz=UTC)
    assert event.wallet == WALLET
    assert event.dex is Dex.ORCA_WHIRLPOOL
    assert event.dex.value == "orca_whirlpool"
    assert event.program_id == ORCA_WHIRLPOOL
    assert event.event_index == 0
    assert event.aggregator is None


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_whirlpool_buy(load_tx, parse) -> None:
    # WSOL -> token through the v1 `swap` instruction (whirlpool at index 2).
    tx = load_tx("orca_whirlpool_buy")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.side is Side.BUY
    assert event.token_mint == TOKEN_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("50000")
    assert event.quote_amount == Decimal("2.5")
    assert event.price_quote_per_token == Decimal("0.00005")
    assert event.pool_address == POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_whirlpool_sell(load_tx, parse) -> None:
    # token -> WSOL through `swap_v2` (memo + mint accounts, whirlpool at 4).
    tx = load_tx("orca_whirlpool_sell")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.side is Side.SELL
    assert event.token_mint == TOKEN_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("20000")
    assert event.quote_amount == Decimal("0.9")
    assert event.price_quote_per_token == Decimal("0.000045")
    assert event.pool_address == POOL


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_usdc_quoted_buy(load_tx, parse) -> None:
    # token <-> USDC with no SOL leg at all: quote must be the USDC mint.
    tx = load_tx("orca_usdc_buy")
    event = _single_event(parse(tx))
    _assert_common(event, tx)
    assert event.side is Side.BUY
    assert event.token_mint == TOKEN2_MINT
    assert event.quote_mint == USDC_MINT
    assert event.token_amount == Decimal("120000")
    assert event.quote_amount == Decimal("300")
    assert event.price_quote_per_token == Decimal("0.0025")
    assert event.pool_address == POOL_USDC


@pytest.mark.parametrize("parse", PARSE_PATHS)
def test_lp_deposit_is_not_a_swap(load_tx, parse) -> None:
    # increase_liquidity: token and WSOL both leave the wallet (same-sign
    # deltas, no counter-leg) -> no swap events.
    tx = load_tx("orca_lp_deposit")
    assert parse(tx) == []


def test_matches_all_orca_fixtures(load_tx) -> None:
    parser = OrcaWhirlpoolParser()
    for name in (
        "orca_whirlpool_buy",
        "orca_whirlpool_sell",
        "orca_usdc_buy",
        "orca_lp_deposit",
    ):
        assert parser.matches(load_tx(name)), name


def test_does_not_match_foreign_transaction() -> None:
    tx = {
        "slot": 1,
        "blockTime": 1_750_000_000,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": ["4abc"],
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
    parser = OrcaWhirlpoolParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []


def test_failed_transaction_yields_nothing(load_tx) -> None:
    tx = load_tx("orca_whirlpool_buy")
    tx["meta"]["err"] = {"InstructionError": [0, {"Custom": 6021}]}
    parser = OrcaWhirlpoolParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []
    assert parse_transaction(tx) == []


def test_undecodable_swap_data_gives_no_pool_but_still_parses(load_tx) -> None:
    # Corrupt the instruction data: venue attribution must survive (program
    # id is still present) but the pool must be dropped rather than guessed.
    tx = load_tx("orca_whirlpool_buy")
    tx["transaction"]["message"]["instructions"][0]["data"] = "0OIl"  # invalid base58
    event = _single_event(OrcaWhirlpoolParser().parse(tx))
    assert event.dex is Dex.ORCA_WHIRLPOOL
    assert event.program_id == ORCA_WHIRLPOOL
    assert event.pool_address is None
    assert event.token_amount == Decimal("50000")


def test_two_hop_swap_gives_no_pool_but_still_parses(load_tx) -> None:
    # two_hop_swap crosses two whirlpools in one instruction: attributing a
    # single pool would be a guess, so pool_address must be None.
    tx = load_tx("orca_whirlpool_buy")
    tx["transaction"]["message"]["instructions"][0]["data"] = _anchor_data("two_hop_swap", 34)
    event = _single_event(OrcaWhirlpoolParser().parse(tx))
    assert event.pool_address is None
    assert event.side is Side.BUY
    assert event.quote_amount == Decimal("2.5")


def test_disagreeing_swap_instructions_give_no_pool(load_tx) -> None:
    # Two decoded swaps naming different whirlpool accounts: ambiguous.
    tx = load_tx("orca_whirlpool_buy")
    instructions = tx["transaction"]["message"]["instructions"]
    second = copy.deepcopy(instructions[0])
    second["accounts"][2] = second["accounts"][4]  # some other account
    instructions.append(second)
    event = _single_event(OrcaWhirlpoolParser().parse(tx))
    assert event.pool_address is None
    assert event.token_amount == Decimal("50000")


def test_pool_candidate_without_owned_vaults_is_dropped(load_tx) -> None:
    # A pool account that never appears as a token-account owner cannot be a
    # real whirlpool (its vaults would show up in the balances) -> None.
    tx = load_tx("orca_whirlpool_buy")
    accounts = tx["transaction"]["message"]["instructions"][0]["accounts"]
    accounts[2] = accounts[7]  # tick array: valid pubkey, owns no vaults
    event = _single_event(OrcaWhirlpoolParser().parse(tx))
    assert event.pool_address is None
    assert event.quote_amount == Decimal("2.5")
