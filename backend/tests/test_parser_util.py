"""Unit tests for the shared balance-delta inference helpers."""

from __future__ import annotations

from decimal import Decimal

from app.ingestion.events import Dex, Side
from app.ingestion.parsers import util
from app.ingestion.programs import USDC_MINT, WSOL_MINT

TRADER = "Trader1111111111111111111111111111111111111"
POOL_AUTH = "PoolAuth111111111111111111111111111111111111"
MEME = "Meme11111111111111111111111111111111111111111"
OTHER = "Other1111111111111111111111111111111111111111"


def make_tx(
    *,
    fee: int = 5000,
    pre_lamports: list[int] | None = None,
    post_lamports: list[int] | None = None,
    keys: list[str] | None = None,
    pre_tokens: list[dict] | None = None,
    post_tokens: list[dict] | None = None,
    err: object = None,
) -> dict:
    keys = keys or [TRADER, POOL_AUTH]
    return {
        "slot": 250_000_000,
        "blockTime": 1_752_000_000,
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": pre_lamports or [10_000_000_000, 0],
            "postBalances": post_lamports or [10_000_000_000 - fee, 0],
            "preTokenBalances": pre_tokens or [],
            "postTokenBalances": post_tokens or [],
            "logMessages": [],
            "innerInstructions": [],
        },
        "transaction": {
            "signatures": ["sig111"],
            "message": {
                "accountKeys": [
                    {"pubkey": key, "signer": i == 0, "writable": True, "source": "transaction"}
                    for i, key in enumerate(keys)
                ],
                "instructions": [],
            },
        },
    }


def token_balance(index: int, mint: str, owner: str, amount: str, decimals: int = 6) -> dict:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {
            "amount": amount,
            "decimals": decimals,
            "uiAmountString": str(Decimal(amount) / Decimal(10) ** decimals),
        },
    }


def test_buy_with_wsol_token_delta() -> None:
    tx = make_tx(
        pre_tokens=[
            token_balance(2, WSOL_MINT, TRADER, "5000000000", 9),
            token_balance(3, MEME, TRADER, "0"),
        ],
        post_tokens=[
            token_balance(2, WSOL_MINT, TRADER, "3000000000", 9),
            token_balance(3, MEME, TRADER, "1000000000"),
        ],
    )
    events = util.infer_swap_events(tx, TRADER, Dex.RAYDIUM_AMM, "prog")
    assert len(events) == 1
    event = events[0]
    assert event.side == Side.BUY
    assert event.token_mint == MEME
    assert event.quote_mint == WSOL_MINT
    assert event.token_amount == Decimal("1000")
    assert event.quote_amount == Decimal("2")
    assert event.price_quote_per_token == Decimal("0.002")


def test_sell_with_native_sol_fallback() -> None:
    fee = 5000
    # Trader receives 1.5 SOL native for selling 300 meme tokens.
    tx = make_tx(
        fee=fee,
        pre_lamports=[10_000_000_000, 0],
        post_lamports=[10_000_000_000 + 1_500_000_000 - fee, 0],
        pre_tokens=[token_balance(2, MEME, TRADER, "300000000")],
        post_tokens=[token_balance(2, MEME, TRADER, "0")],
    )
    events = util.infer_swap_events(tx, TRADER, Dex.PUMPFUN, "prog")
    assert len(events) == 1
    event = events[0]
    assert event.side == Side.SELL
    assert event.quote_mint == WSOL_MINT
    assert event.quote_amount == Decimal("1.5")
    assert event.token_amount == Decimal("300")


def test_stable_quoted_swap() -> None:
    tx = make_tx(
        pre_tokens=[
            token_balance(2, USDC_MINT, TRADER, "100000000"),
            token_balance(3, MEME, TRADER, "0"),
        ],
        post_tokens=[
            token_balance(2, USDC_MINT, TRADER, "40000000"),
            token_balance(3, MEME, TRADER, "600000000"),
        ],
    )
    events = util.infer_swap_events(tx, TRADER, Dex.ORCA_WHIRLPOOL, "prog")
    assert len(events) == 1
    assert events[0].quote_mint == USDC_MINT
    assert events[0].quote_amount == Decimal("60")
    assert events[0].side == Side.BUY


def test_stable_quoted_buy_not_preempted_by_ata_rent() -> None:
    # Regression: a 60-USDC buy whose tx also pays ~0.00204 SOL of ATA-creation
    # rent. The rent is opposite-signed to the +token delta and clears
    # MIN_SOL_FLOW, so the old code returned WSOL/0.00204 and discarded the real
    # USDC leg — collapsing price/PnL. The larger leg (USDC) must win.
    fee = 5000
    ata_rent = 2_040_000  # lamports
    tx = make_tx(
        fee=fee,
        pre_lamports=[10_000_000_000, 0],
        post_lamports=[10_000_000_000 - fee - ata_rent, 0],
        pre_tokens=[
            token_balance(2, USDC_MINT, TRADER, "100000000"),
            token_balance(3, MEME, TRADER, "0"),
        ],
        post_tokens=[
            token_balance(2, USDC_MINT, TRADER, "40000000"),
            token_balance(3, MEME, TRADER, "600000000"),
        ],
    )
    events = util.infer_swap_events(tx, TRADER, Dex.ORCA_WHIRLPOOL, "prog")
    assert len(events) == 1
    assert events[0].quote_mint == USDC_MINT
    assert events[0].quote_amount == Decimal("60")
    assert events[0].side == Side.BUY


def test_token_to_token_emits_two_events() -> None:
    tx = make_tx(
        pre_tokens=[
            token_balance(2, MEME, TRADER, "500000000"),
            token_balance(3, OTHER, TRADER, "0"),
        ],
        post_tokens=[
            token_balance(2, MEME, TRADER, "0"),
            token_balance(3, OTHER, TRADER, "250000000"),
        ],
    )
    events = util.infer_swap_events(tx, TRADER, Dex.JUPITER, "prog")
    assert len(events) == 2
    sides = {(e.token_mint, e.side) for e in events}
    assert (MEME, Side.SELL) in sides
    assert (OTHER, Side.BUY) in sides


def test_no_counter_leg_is_not_a_swap() -> None:
    tx = make_tx(
        pre_tokens=[token_balance(2, MEME, TRADER, "0")],
        post_tokens=[token_balance(2, MEME, TRADER, "100000000")],
    )
    assert util.infer_swap_events(tx, TRADER, Dex.RAYDIUM_AMM, "prog") == []


def test_failed_tx_yields_nothing() -> None:
    tx = make_tx(err={"InstructionError": [2, {"Custom": 6001}]})
    assert util.infer_swap_events(tx, TRADER, Dex.PUMPFUN, "prog") == []
    assert not util.is_success(tx)


def test_fee_payer_and_native_delta_fee_adjustment() -> None:
    fee = 5000
    tx = make_tx(fee=fee)
    assert util.fee_payer(tx) == TRADER
    # Only the fee moved, so the fee-adjusted delta is exactly zero.
    assert util.native_sol_delta(tx, TRADER) == Decimal(0)
