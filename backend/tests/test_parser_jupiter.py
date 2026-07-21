"""Tests for the Jupiter aggregator fallback parser.

The fixtures model what makes Jupiter special as a router:

- ``jupiter_unknown_route_buy``: the route goes through an unsupported venue
  (only the Jupiter program plus an unknown inner program appear), so no
  venue parser can claim it and the registry falls back to ``JupiterParser``;
- ``jupiter_multihop_buy``: SOL -> intermediate -> token in one route; the
  intermediate mint's pre/post deltas cancel for the trader, so a single
  event for the final token with the SOL quote must come out;
- ``jupiter_raydium_route_sell``: the route goes through Raydium AMM v4, so
  the venue parser wins, the fallback stays silent, and the registry emits
  exactly one venue-attributed event tagged ``aggregator="jupiter"``.

Fixture balances are internally consistent: lamport sums differ by exactly
the fee, and WSOL token-account lamports carry rent plus the wrapped amount.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingestion.events import Dex, Side, SwapEvent
from app.ingestion.parsers import parse_transaction
from app.ingestion.parsers.jupiter import JupiterParser
from app.ingestion.programs import JUPITER_V4, JUPITER_V6, WSOL_MINT

# Pubkeys baked into the fixtures (deterministically generated).
TRADER = "GrTkKtxxsy5UHeyC9tFgoPsR9mtrv1Db6Vrz2p1gqhLN"
MOON_MINT = "BJsTvEo1DsQdsBtyN3MMSNA1TVrbj6qLFfSctrttK18T"
MID_MINT = "ETGEb3r6nQtXGk4AgStxiycezTuE8J96Vqkq3ez7SeUE"
FINAL_MINT = "BtVxvCULv4P2A4NKf94QthiX4L2mPiFBNBDarbf1DdGb"
PONZ_MINT = "EobtA7u7VicoYPUBwbs7adfJLtb5GRxjrVKu3ohZfse7"


def _single_event(events: list[SwapEvent]) -> SwapEvent:
    assert len(events) == 1
    return events[0]


def test_unsupported_route_via_registry(load_tx) -> None:
    """Unknown inner venue: only the fallback can claim the swap."""
    tx = load_tx("jupiter_unknown_route_buy")
    event = _single_event(parse_transaction(tx))

    assert event.dex is Dex.JUPITER
    assert event.dex.value == "jupiter"
    assert event.aggregator == "jupiter"
    assert event.wallet == TRADER
    assert event.side is Side.BUY
    assert event.token_mint == MOON_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("150000")
    assert event.quote_amount == Decimal("0.75")
    assert event.price_quote_per_token == Decimal("0.000005")
    assert event.pool_address is None
    assert event.program_id == JUPITER_V6
    assert event.event_index == 0
    assert event.signature == tx["transaction"]["signatures"][0]
    assert event.slot == tx["slot"]
    assert event.block_time == datetime.fromtimestamp(tx["blockTime"], tz=UTC)


def test_unsupported_route_direct_parse(load_tx) -> None:
    """A direct ``JupiterParser().parse`` call emits the same jupiter event."""
    tx = load_tx("jupiter_unknown_route_buy")
    event = _single_event(JupiterParser().parse(tx))

    assert event.dex is Dex.JUPITER
    assert event.wallet == TRADER
    assert event.side is Side.BUY
    assert event.token_mint == MOON_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("150000")
    assert event.quote_amount == Decimal("0.75")
    assert event.pool_address is None
    assert event.program_id == JUPITER_V6
    # The aggregator tag is the registry's job, not the parser's.
    assert event.aggregator is None


@pytest.mark.parametrize(
    ("parse", "expected_aggregator"),
    [
        pytest.param(lambda tx: JupiterParser().parse(tx), None, id="direct"),
        pytest.param(parse_transaction, "jupiter", id="registry"),
    ],
)
def test_multihop_collapses_to_final_leg(load_tx, parse, expected_aggregator) -> None:
    """SOL -> MID -> FINAL: the netted-out intermediate leg must not surface."""
    tx = load_tx("jupiter_multihop_buy")

    # Guard the fixture's intent: the trader's intermediate-mint balance
    # appears in both snapshots and its deltas cancel exactly.
    mid_entries = {
        phase: [
            entry
            for entry in tx["meta"][phase]
            if entry["owner"] == TRADER and entry["mint"] == MID_MINT
        ]
        for phase in ("preTokenBalances", "postTokenBalances")
    }
    assert mid_entries["preTokenBalances"] and mid_entries["postTokenBalances"]
    assert sum(int(e["uiTokenAmount"]["amount"]) for e in mid_entries["preTokenBalances"]) == sum(
        int(e["uiTokenAmount"]["amount"]) for e in mid_entries["postTokenBalances"]
    )

    event = _single_event(parse(tx))
    assert event.dex is Dex.JUPITER
    assert event.aggregator == expected_aggregator
    assert event.wallet == TRADER
    assert event.side is Side.BUY
    assert event.token_mint == FINAL_MINT
    assert event.token_mint != MID_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("60000")
    assert event.quote_amount == Decimal("1.2")
    assert event.price_quote_per_token == Decimal("0.00002")
    assert event.pool_address is None


def test_raydium_route_dedups_to_single_venue_event(load_tx) -> None:
    """Route through a supported venue: venue attribution wins, one event."""
    raydium = pytest.importorskip("app.ingestion.parsers.raydium")
    tx = load_tx("jupiter_raydium_route_sell")

    # Both adapters match the transaction; the registry must still emit
    # exactly one event, attributed to the venue, tagged with the aggregator.
    assert raydium.RaydiumParser().matches(tx)
    assert JupiterParser().matches(tx)

    event = _single_event(parse_transaction(tx))
    assert event.dex is Dex.RAYDIUM_AMM
    assert event.dex.value == "raydium_amm"
    assert event.aggregator == "jupiter"
    assert event.wallet == TRADER
    assert event.side is Side.SELL
    assert event.token_mint == PONZ_MINT
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("25000")
    assert event.quote_amount == Decimal("0.9")
    assert event.event_index == 0


def test_parser_contract_attributes() -> None:
    parser = JupiterParser()
    assert parser.fallback_only is True
    assert parser.dex is Dex.JUPITER
    assert parser.program_ids == frozenset({JUPITER_V6, JUPITER_V4})


def test_matches_all_jupiter_fixtures(load_tx) -> None:
    parser = JupiterParser()
    for name in (
        "jupiter_unknown_route_buy",
        "jupiter_multihop_buy",
        "jupiter_raydium_route_sell",
    ):
        assert parser.matches(load_tx(name)), name


def test_does_not_match_or_parse_foreign_transaction() -> None:
    tx = {
        "slot": 1,
        "blockTime": 1_750_100_000,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": ["2abc"],
            "message": {
                "accountKeys": [
                    {"pubkey": TRADER, "signer": True, "writable": True, "source": "transaction"}
                ],
                "instructions": [
                    {"programId": "11111111111111111111111111111111", "accounts": [], "data": ""}
                ],
            },
        },
    }
    parser = JupiterParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []


def test_failed_transaction_yields_nothing(load_tx) -> None:
    tx = load_tx("jupiter_unknown_route_buy")
    tx["meta"]["err"] = {"InstructionError": [1, {"Custom": 6001}]}
    parser = JupiterParser()
    assert not parser.matches(tx)
    assert parser.parse(tx) == []
    assert parse_transaction(tx) == []


def test_jupiter_v4_program_is_recognized(load_tx) -> None:
    """The older v4 router id must map to the same jupiter attribution."""
    tx = load_tx("jupiter_unknown_route_buy")
    for ix in tx["transaction"]["message"]["instructions"]:
        if ix["programId"] == JUPITER_V6:
            ix["programId"] = JUPITER_V4
    for entry in tx["transaction"]["message"]["accountKeys"]:
        if entry["pubkey"] == JUPITER_V6:
            entry["pubkey"] = JUPITER_V4

    event = _single_event(JupiterParser().parse(tx))
    assert event.dex is Dex.JUPITER
    assert event.program_id == JUPITER_V4
    assert event.quote_amount == Decimal("0.75")

    registry_event = _single_event(parse_transaction(tx))
    assert registry_event.aggregator == "jupiter"
    assert registry_event.dex is Dex.JUPITER
