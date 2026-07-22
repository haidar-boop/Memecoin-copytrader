"""Tests for the market-regime detector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select

from app.db.models import MarketRegime, MarketSnapshot, TokenSnapshot, Trade
from app.db.util import bulk_append
from app.evaluation.regime import (
    BEAR_PCT,
    BULL_PCT,
    detect_regime,
)
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
WINDOW_MINUTES = 60
LOOKBACK = 24


def _trade_row(
    event_index: int, wallet_id: int, side: str, quote: str, *, minutes_ago: int
) -> dict:
    return {
        "signature": f"sig-{side}-{wallet_id}-{event_index}",
        "event_index": event_index,
        "block_time": NOW - timedelta(minutes=minutes_ago),
        "slot": 1,
        "wallet_id": wallet_id,
        "token_id": 1,
        "dex": "pumpfun",
        "side": side,
        "token_amount": Decimal("1000"),
        "quote_amount": Decimal(quote),
        "quote_mint": WSOL_MINT,
    }


async def _seed_price_series(
    session, prices: list[float], **extra
) -> None:
    """Seed one snapshot per hour, oldest first. prices[0] is oldest."""
    n = len(prices)
    rows = []
    for i, price in enumerate(prices):
        hours_ago = n - 1 - i
        rows.append(
            {
                "ts": NOW - timedelta(hours=hours_ago),
                "sol_price_usd": Decimal(str(price)),
                "volume_sol_1h": Decimal(str(extra.get("volume", 100.0))),
                "trades_1h": extra.get("trades", 1000),
                "tokens_launched_1h": extra.get("launched", 3),
                "active_wallets_1h": 50,
                "failed_tx_1h": 5,
            }
        )
    await bulk_append(session, MarketSnapshot, rows)
    await session.commit()


async def _run(session):
    return await detect_regime(
        session,
        window_minutes=WINDOW_MINUTES,
        lookback_windows=LOOKBACK,
        now=NOW,
    )


async def test_rising_price_is_bull(db_session):
    prices = [100.0 + i for i in range(25)]  # 100 -> 124
    await _seed_price_series(db_session, prices)
    regime = await _run(db_session)
    assert regime.regime == "bull"
    assert regime.features["sol_price_pct_change"] > BULL_PCT
    assert not regime.high_volatility
    # Row persisted.
    stored = (await db_session.execute(select(MarketRegime))).scalars().all()
    assert len(stored) == 1


async def test_falling_price_is_bear(db_session):
    prices = [130.0 - i for i in range(25)]  # 130 -> 106
    await _seed_price_series(db_session, prices)
    regime = await _run(db_session)
    assert regime.regime == "bear"
    assert regime.features["sol_price_pct_change"] < BEAR_PCT


async def test_flat_price_is_sideways(db_session):
    prices = [100.0 + (0.01 if i % 2 else -0.01) for i in range(25)]
    await _seed_price_series(db_session, prices)
    regime = await _run(db_session)
    assert regime.regime == "sideways"
    assert abs(regime.features["sol_price_pct_change"]) < BULL_PCT


async def test_empty_data_defaults_sideways(db_session):
    regime = await _run(db_session)
    assert regime.regime == "sideways"
    assert "note" in regime.features
    assert regime.features["sol_price_pct_change"] is None


async def test_volatile_series_sets_high_volatility(db_session):
    prices = [120.0 if i % 2 == 0 else 80.0 for i in range(25)]
    await _seed_price_series(db_session, prices)
    regime = await _run(db_session)
    assert regime.high_volatility
    assert regime.features["return_stdev_pct"] > regime.features[
        "high_vol_stdev_threshold_pct"
    ]


async def test_launch_spike_sets_launch_wave(db_session):
    prices = [100.0] * 25
    rows = []
    for i, price in enumerate(prices):
        hours_ago = 24 - i
        launched = 40 if i == len(prices) - 1 else 2
        rows.append(
            {
                "ts": NOW - timedelta(hours=hours_ago),
                "sol_price_usd": Decimal(str(price)),
                "volume_sol_1h": Decimal("100"),
                "trades_1h": 1000,
                "tokens_launched_1h": launched,
            }
        )
    await bulk_append(db_session, MarketSnapshot, rows)
    await db_session.commit()
    regime = await _run(db_session)
    assert regime.launch_wave
    assert regime.features["latest_tokens_launched_1h"] == 40
    assert regime.features["median_tokens_launched_1h"] == 2


async def test_sell_dominated_falling_window_sets_panic_selling(db_session):
    prices = [130.0 - i for i in range(25)]  # bear + last window falling
    await _seed_price_series(db_session, prices)
    # Seed sell-dominated trades inside the last window.
    trades = []
    ei = 0
    for k in range(24):
        trades.append(
            _trade_row(ei, 1000 + k, Side.SELL.value, "2.0", minutes_ago=30)
        )
        ei += 1
    for k in range(4):
        trades.append(
            _trade_row(ei, 2000 + k, Side.BUY.value, "2.0", minutes_ago=30)
        )
        ei += 1
    await bulk_append(db_session, Trade, trades)
    await db_session.commit()
    regime = await _run(db_session)
    assert regime.panic_selling
    assert regime.features["last_window_sell_trades"] == 24
    assert regime.features["last_window_buy_trades"] == 4
    assert regime.regime == "bear"


async def test_whale_net_inflow_sets_whale_accumulation(db_session):
    prices = [100.0] * 25
    await _seed_price_series(db_session, prices)
    trades = []
    ei = 0
    # 12 small wallets each buy 0.5 SOL.
    for k in range(12):
        trades.append(
            _trade_row(ei, 3000 + k, Side.BUY.value, "0.5", minutes_ago=20)
        )
        ei += 1
    # 2 whales each net-buy 20 SOL.
    for k in range(2):
        trades.append(
            _trade_row(ei, 9000 + k, Side.BUY.value, "20.0", minutes_ago=15)
        )
        ei += 1
    await bulk_append(db_session, Trade, trades)
    await db_session.commit()
    regime = await _run(db_session)
    assert regime.whale_accumulation
    assert regime.features["whale_net_flow_sol"] > regime.features[
        "whale_net_flow_threshold_sol"
    ]


async def test_low_liquidity_from_thin_token_books(db_session):
    prices = [100.0] * 25
    await _seed_price_series(db_session, prices)
    snaps = []
    for token_id in range(5):
        # Latest snapshot per token has thin liquidity.
        snaps.append(
            {
                "token_id": token_id,
                "ts": NOW - timedelta(minutes=5),
                "liquidity_sol": Decimal("5.0"),
                "volume_sol_1h": Decimal("1"),
            }
        )
        snaps.append(
            {
                "token_id": token_id,
                "ts": NOW - timedelta(hours=10),
                "liquidity_sol": Decimal("500.0"),
                "volume_sol_1h": Decimal("1"),
            }
        )
    await bulk_append(db_session, TokenSnapshot, snaps)
    await db_session.commit()
    regime = await _run(db_session)
    assert regime.low_liquidity
    assert regime.features["median_token_liquidity_sol"] == 5.0


async def test_features_carry_all_numbers(db_session):
    np.random.seed(0)
    prices = [100.0 + i * 0.5 for i in range(25)]
    await _seed_price_series(db_session, prices)
    regime = await _run(db_session)
    f = regime.features
    for key in (
        "sol_price_first",
        "sol_price_last",
        "sol_price_pct_change",
        "sol_price_slope_per_hour",
        "return_stdev_pct",
        "median_token_liquidity_sol",
        "latest_tokens_launched_1h",
        "window_minutes",
        "lookback_windows",
    ):
        assert key in f
    assert regime.description


async def test_hysteresis_holds_bull_inside_band(db_session):
    """A prior bull label sticks while the trend hovers inside the band,
    and releases once it decays past the hysteresis margin."""
    from app.evaluation.regime import HYSTERESIS_PCT, _trend_label

    # Pure-function edges first (the DB path below covers integration).
    assert _trend_label(BULL_PCT - 0.5, "bull") == "bull"       # inside band
    assert _trend_label(BULL_PCT - HYSTERESIS_PCT, "bull") == "sideways"
    assert _trend_label(BEAR_PCT + 0.5, "bear") == "bear"
    assert _trend_label(BEAR_PCT + HYSTERESIS_PCT, "bear") == "sideways"
    # Entering still requires a full threshold cross.
    assert _trend_label(BULL_PCT - 0.5, "sideways") == "sideways"
    assert _trend_label(None, "bull") == "sideways"

    # Integration: previous bull row + a +2.5% series stays bull.
    db_session.add(
        MarketRegime(
            ts=NOW - timedelta(minutes=WINDOW_MINUTES),
            window_minutes=WINDOW_MINUTES,
            regime="bull",
        )
    )
    await db_session.commit()
    await _seed_price_series(db_session, list(np.linspace(100.0, 102.5, 24)))
    regime = await detect_regime(
        db_session,
        window_minutes=WINDOW_MINUTES,
        lookback_windows=LOOKBACK,
        now=NOW,
    )
    assert regime.regime == "bull"
    assert regime.features["previous_regime"] == "bull"
