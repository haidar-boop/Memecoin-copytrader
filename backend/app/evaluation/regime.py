"""Market-regime detector. Append-only ``market_regimes`` rows.

Each pass reads the recent :class:`MarketSnapshot` series (the venue-wide
minute cadence) over the trailing ``lookback_windows * window_minutes`` and
labels the market with a PRIMARY regime derived from the SOL price trend
(bull / bear / sideways) plus a set of independent MODIFIER FLAGS, each backed
by a documented rule whose numbers are all written into ``features``. Trades in
the most recent window feed the flow-based flags (whale accumulation, panic
selling); the latest :class:`TokenSnapshot` per token feeds the liquidity flag.

Every number behind every decision lands in ``features`` and the one-sentence
``description`` names which rules fired. Sparse/empty data never crashes: the
detector falls back to a ``sideways`` label with an explanatory note. One
``MarketRegime`` row is inserted per pass and returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketRegime, MarketSnapshot, TokenSnapshot, Trade
from app.db.util import aware as _aware
from app.db.util import sql_cutoff, to_float
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

# --- primary trend thresholds -------------------------------------------------
# SOL price percent change across the whole lookback window.
BULL_PCT = 3.0
BEAR_PCT = -3.0

# --- modifier-flag thresholds -------------------------------------------------
# Stdev of per-window percent returns above this = choppy tape.
HIGH_VOL_STDEV_PCT = 5.0
# Median latest per-token liquidity below this (SOL) = thin books; OR a venue
# volume collapse to below this fraction of the lookback median.
LOW_LIQUIDITY_SOL = 25.0
VOLUME_COLLAPSE_FRACTION = 0.5
# Whale accumulation: a real population, a positive net WSOL inflow from the
# top-decile-by-size wallets over the last window, at least this large (SOL).
WHALE_DECILE_PCT = 90.0
WHALE_MIN_WALLETS = 10
WHALE_NET_FLOW_SOL = 5.0
# Panic selling: sells dominate buys by this ratio over a minimum sample, while
# price is falling in the last window.
PANIC_SELL_RATIO = 1.5
PANIC_MIN_TRADES = 10
# Launch wave: latest tokens_launched_1h this many times the lookback median and
# at least this many launches in absolute terms.
LAUNCH_WAVE_MULT = 2.0
LAUNCH_WAVE_MIN = 5
# Trend exhaustion: price and volume/trade momentum diverge in sign over the
# last few windows, each of at least this magnitude (percent).
EXHAUSTION_WINDOWS = 3
EXHAUSTION_MIN_PCT = 1.0


def _pct_change(first: float, last: float) -> float | None:
    return None if first == 0 else (last - first) / abs(first) * 100.0


def _window_index(ts: datetime, start: datetime, window_minutes: int) -> int:
    return int((ts - start).total_seconds() // (window_minutes * 60))


async def _market_series(
    session: AsyncSession, start: datetime, window_minutes: int
) -> list[dict[str, Any]]:
    """Per-window representative snapshots (the last row in each window)."""
    rows = await session.execute(
        select(
            MarketSnapshot.ts,
            MarketSnapshot.sol_price_usd,
            MarketSnapshot.volume_sol_1h,
            MarketSnapshot.trades_1h,
            MarketSnapshot.tokens_launched_1h,
        )
        .where(MarketSnapshot.ts >= sql_cutoff(session, start))
        .order_by(MarketSnapshot.ts)
    )
    by_window: dict[int, dict[str, Any]] = {}
    for ts, price, volume, trades, launched in rows:
        ts = _aware(ts)
        if ts < start:
            continue
        idx = _window_index(ts, start, window_minutes)
        by_window[idx] = {
            "idx": idx,
            "ts": ts,
            "price": to_float(price),
            "volume_sol_1h": to_float(volume),
            "trades_1h": to_float(trades),
            "tokens_launched_1h": to_float(launched),
        }
    return [by_window[k] for k in sorted(by_window)]


def _detect_high_volatility(
    prices: list[float], features: dict[str, Any]
) -> bool:
    if len(prices) < 3:
        features["return_stdev_pct"] = None
        return False
    returns = [
        r
        for i in range(len(prices) - 1)
        if (r := _pct_change(prices[i], prices[i + 1])) is not None
    ]
    stdev = float(np.std(returns)) if returns else 0.0
    features["return_stdev_pct"] = stdev
    features["high_vol_stdev_threshold_pct"] = HIGH_VOL_STDEV_PCT
    return stdev > HIGH_VOL_STDEV_PCT


async def _detect_low_liquidity(
    session: AsyncSession,
    start: datetime,
    series: list[dict[str, Any]],
    features: dict[str, Any],
) -> bool:
    # Latest liquidity per token inside the lookback window.
    sub = (
        select(
            TokenSnapshot.token_id,
            func.max(TokenSnapshot.ts).label("ts"),
        )
        .where(TokenSnapshot.ts >= sql_cutoff(session, start))
        .group_by(TokenSnapshot.token_id)
        .subquery()
    )
    rows = await session.execute(
        select(TokenSnapshot.liquidity_sol).join(
            sub,
            (TokenSnapshot.token_id == sub.c.token_id)
            & (TokenSnapshot.ts == sub.c.ts),
        )
    )
    liqs = [v for (raw,) in rows if (v := to_float(raw)) is not None]
    median_liq = float(np.median(liqs)) if liqs else None
    features["median_token_liquidity_sol"] = median_liq
    features["low_liquidity_threshold_sol"] = LOW_LIQUIDITY_SOL
    features["token_liquidity_sample"] = len(liqs)

    volumes = [s["volume_sol_1h"] for s in series if s["volume_sol_1h"] is not None]
    latest_vol = volumes[-1] if volumes else None
    median_vol = float(np.median(volumes)) if volumes else None
    features["latest_volume_sol_1h"] = latest_vol
    features["median_volume_sol_1h"] = median_vol
    features["volume_collapse_fraction"] = VOLUME_COLLAPSE_FRACTION

    thin_books = median_liq is not None and median_liq < LOW_LIQUIDITY_SOL
    volume_collapse = (
        latest_vol is not None
        and median_vol is not None
        and median_vol > 0
        and latest_vol < VOLUME_COLLAPSE_FRACTION * median_vol
    )
    features["low_liquidity_thin_books"] = thin_books
    features["low_liquidity_volume_collapse"] = volume_collapse
    return thin_books or volume_collapse


async def _last_window_trades(
    session: AsyncSession, window_start: datetime
) -> list[tuple[int, str, float]]:
    rows = await session.execute(
        select(Trade.wallet_id, Trade.side, Trade.quote_amount, Trade.block_time).where(
            Trade.quote_mint == WSOL_MINT,
            Trade.block_time >= sql_cutoff(session, window_start),
        )
    )
    out: list[tuple[int, str, float]] = []
    for wallet_id, side, quote, block_time in rows:
        if _aware(block_time) < window_start:
            continue
        amount = to_float(quote)
        if amount is None:
            continue
        out.append((wallet_id, side, amount))
    return out


def _detect_whale_accumulation(
    trades: list[tuple[int, str, float]], features: dict[str, Any]
) -> bool:
    # Per-wallet size = total WSOL notional traded in the window.
    volume: dict[int, float] = {}
    net: dict[int, float] = {}
    for wallet_id, side, amount in trades:
        volume[wallet_id] = volume.get(wallet_id, 0.0) + amount
        sign = 1.0 if side == Side.BUY.value else -1.0
        net[wallet_id] = net.get(wallet_id, 0.0) + sign * amount
    features["last_window_wallet_count"] = len(volume)
    if len(volume) < WHALE_MIN_WALLETS:
        features["whale_net_flow_sol"] = None
        features["whale_wallet_count"] = 0
        return False
    threshold = float(np.percentile(np.array(list(volume.values())), WHALE_DECILE_PCT))
    whales = [w for w, vol in volume.items() if vol >= threshold]
    net_flow = sum(net[w] for w in whales)
    features["whale_size_threshold_sol"] = threshold
    features["whale_wallet_count"] = len(whales)
    features["whale_net_flow_sol"] = net_flow
    features["whale_net_flow_threshold_sol"] = WHALE_NET_FLOW_SOL
    return net_flow > WHALE_NET_FLOW_SOL


def _detect_panic_selling(
    trades: list[tuple[int, str, float]],
    last_window_price_pct: float | None,
    features: dict[str, Any],
) -> bool:
    buys = sum(1 for _, side, _ in trades if side == Side.BUY.value)
    sells = sum(1 for _, side, _ in trades if side == Side.SELL.value)
    features["last_window_buy_trades"] = buys
    features["last_window_sell_trades"] = sells
    features["panic_sell_ratio_threshold"] = PANIC_SELL_RATIO
    price_falling = last_window_price_pct is not None and last_window_price_pct < 0
    sell_dominated = (
        buys + sells >= PANIC_MIN_TRADES and sells > PANIC_SELL_RATIO * max(buys, 1)
    )
    return sell_dominated and price_falling


def _detect_launch_wave(
    series: list[dict[str, Any]], features: dict[str, Any]
) -> bool:
    launched = [
        s["tokens_launched_1h"] for s in series if s["tokens_launched_1h"] is not None
    ]
    if not launched:
        features["latest_tokens_launched_1h"] = None
        features["median_tokens_launched_1h"] = None
        return False
    latest = launched[-1]
    median = float(np.median(launched))
    features["latest_tokens_launched_1h"] = latest
    features["median_tokens_launched_1h"] = median
    features["launch_wave_mult_threshold"] = LAUNCH_WAVE_MULT
    return latest >= LAUNCH_WAVE_MIN and latest >= LAUNCH_WAVE_MULT * max(median, 1e-9)


def _detect_trend_exhaustion(
    series: list[dict[str, Any]], features: dict[str, Any]
) -> bool:
    tail = series[-EXHAUSTION_WINDOWS:]
    prices = [s["price"] for s in tail if s["price"] is not None]
    volumes = [s["volume_sol_1h"] for s in tail if s["volume_sol_1h"] is not None]
    trades = [s["trades_1h"] for s in tail if s["trades_1h"] is not None]
    if len(prices) < 2 or (len(volumes) < 2 and len(trades) < 2):
        features["exhaustion_price_pct"] = None
        features["exhaustion_momentum_pct"] = None
        return False
    price_pct = _pct_change(prices[0], prices[-1])
    # Momentum proxy: prefer volume, fall back to trade count.
    if len(volumes) >= 2:
        momentum_pct = _pct_change(volumes[0], volumes[-1])
        features["exhaustion_momentum_source"] = "volume_sol_1h"
    else:
        momentum_pct = _pct_change(trades[0], trades[-1])
        features["exhaustion_momentum_source"] = "trades_1h"
    features["exhaustion_price_pct"] = price_pct
    features["exhaustion_momentum_pct"] = momentum_pct
    features["exhaustion_min_pct"] = EXHAUSTION_MIN_PCT
    if price_pct is None or momentum_pct is None:
        return False
    # Price extends while participation contracts (or vice versa).
    return (
        abs(price_pct) >= EXHAUSTION_MIN_PCT
        and abs(momentum_pct) >= EXHAUSTION_MIN_PCT
        and (price_pct > 0) != (momentum_pct > 0)
    )


async def detect_regime(
    session: AsyncSession,
    *,
    window_minutes: int,
    lookback_windows: int,
    now: datetime | None = None,
) -> MarketRegime:
    """Detect and persist one market regime over the trailing lookback.

    Reads the recent market/token/trade state, derives a bull/bear/sideways
    label from the SOL price trend, evaluates six modifier flags, and inserts a
    single :class:`MarketRegime` row (also returned). Never raises on empty or
    sparse data; falls back to ``sideways`` with a note.
    """
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    total_minutes = window_minutes * lookback_windows
    start = now - timedelta(minutes=total_minutes)
    last_window_start = now - timedelta(minutes=window_minutes)

    series = await _market_series(session, start, window_minutes)
    prices = [s["price"] for s in series if s["price"] is not None]

    features: dict[str, Any] = {
        "window_minutes": window_minutes,
        "lookback_windows": lookback_windows,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "snapshot_windows": len(series),
        "price_points": len(prices),
        "bull_pct_threshold": BULL_PCT,
        "bear_pct_threshold": BEAR_PCT,
    }

    if len(prices) < 2:
        features["sol_price_pct_change"] = None
        features["note"] = "insufficient price history; defaulting to sideways"
        regime = MarketRegime(
            ts=now,
            window_minutes=window_minutes,
            regime="sideways",
            high_volatility=False,
            low_liquidity=False,
            whale_accumulation=False,
            panic_selling=False,
            launch_wave=False,
            trend_exhaustion=False,
            features=features,
            description=(
                "Sideways (fallback): only "
                f"{len(prices)} SOL price point(s) in the last "
                f"{total_minutes} minutes; no regime signal."
            ),
        )
        session.add(regime)
        await session.commit()
        log.info("regime_cycle", regime="sideways", reason="sparse", price_points=len(prices))
        return regime

    first_price, last_price = prices[0], prices[-1]
    pct_change = _pct_change(first_price, last_price)
    # Linear slope of price vs elapsed minutes (SOL price change per hour).
    xs = np.array(
        [(s["ts"] - start).total_seconds() / 3600.0 for s in series if s["price"] is not None]
    )
    ys = np.array([p for p in prices])
    slope_per_hour = float(np.polyfit(xs, ys, 1)[0]) if len(xs) >= 2 else None

    if pct_change is not None and pct_change > BULL_PCT:
        label = "bull"
    elif pct_change is not None and pct_change < BEAR_PCT:
        label = "bear"
    else:
        label = "sideways"

    features["sol_price_first"] = first_price
    features["sol_price_last"] = last_price
    features["sol_price_pct_change"] = pct_change
    features["sol_price_slope_per_hour"] = slope_per_hour

    # Price move within the most recent window (for the falling-price gates).
    last_window_prices = [
        s["price"] for s in series if s["price"] is not None and s["ts"] >= last_window_start
    ]
    if last_window_prices and len(prices) >= 2:
        # Compare the price entering the last window with the latest print.
        prior = last_window_prices[0] if len(last_window_prices) >= 2 else prices[-2]
        last_window_price_pct = _pct_change(prior, last_price)
    else:
        last_window_price_pct = None
    features["last_window_price_pct"] = last_window_price_pct

    trades = await _last_window_trades(session, last_window_start)
    features["last_window_trade_count"] = len(trades)
    features["last_window_start"] = last_window_start.isoformat()

    high_volatility = _detect_high_volatility(prices, features)
    low_liquidity = await _detect_low_liquidity(session, start, series, features)
    whale_accumulation = _detect_whale_accumulation(trades, features)
    panic_selling = _detect_panic_selling(trades, last_window_price_pct, features)
    launch_wave = _detect_launch_wave(series, features)
    trend_exhaustion = _detect_trend_exhaustion(series, features)

    fired = [
        name
        for name, on in [
            ("high_volatility", high_volatility),
            ("low_liquidity", low_liquidity),
            ("whale_accumulation", whale_accumulation),
            ("panic_selling", panic_selling),
            ("launch_wave", launch_wave),
            ("trend_exhaustion", trend_exhaustion),
        ]
        if on
    ]
    flag_txt = f" Flags fired: {', '.join(fired)}." if fired else " No modifier flags fired."
    # pct_change is None when the first price is 0 (div-by-zero guard); never
    # feed None to a numeric format spec.
    move_txt = f"{pct_change:+.2f}%" if pct_change is not None else "an undefined amount"
    description = (
        f"{label.capitalize()} regime: SOL price moved {move_txt} over the last "
        f"{total_minutes} minutes ({len(prices)} price points)."
        f"{flag_txt}"
    )

    regime = MarketRegime(
        ts=now,
        window_minutes=window_minutes,
        regime=label,
        high_volatility=high_volatility,
        low_liquidity=low_liquidity,
        whale_accumulation=whale_accumulation,
        panic_selling=panic_selling,
        launch_wave=launch_wave,
        trend_exhaustion=trend_exhaustion,
        features=features,
        description=description,
    )
    session.add(regime)
    await session.commit()
    log.info(
        "regime_cycle",
        regime=label,
        pct_change=pct_change,
        flags=fired,
        price_points=len(prices),
    )
    return regime
