"""Pattern discovery over the learning database. Append-only ``patterns`` rows.

Each cycle runs a set of independent detectors over closed positions (and, for
the flow/volume detectors, trades and token snapshots) inside a trailing
window. Every finding that clears the evidence bar becomes one
:class:`DiscoveredPattern` row carrying its bucket identity (``key``), the
supporting numbers (``stats`` — always including overall baselines where they
apply), the evidence count, the window bounds, and a one-sentence description.
Rows are only ever inserted; history is training data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscoveredPattern, Position, Token, TokenSnapshot, Trade
from app.db.util import aware as _aware
from app.db.util import sql_cutoff
from app.ingestion.events import Side
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

# (label, upper bound in seconds) — ordered; None = open-ended top bucket.
ENTRY_DELAY_BUCKETS: list[tuple[str, float | None]] = [
    ("<1m", 60.0),
    ("1-10m", 600.0),
    ("10-60m", 3600.0),
    ("1-6h", 21600.0),
    ("6-24h", 86400.0),
    (">24h", None),
]

HOLD_TIME_BUCKETS: list[tuple[str, float | None]] = [
    ("<5m", 300.0),
    ("5-30m", 1800.0),
    ("30m-4h", 14400.0),
    ("4-24h", 86400.0),
    (">24h", None),
]

WHALE_DECILE_PCT = 90.0
WHALE_FLOW_HOURS = 24
WHALE_TOP_TOKENS = 10
# A "top decile" needs a population: below this many wallets, whale-flow
# detection would just crown someone whale of themselves.
WHALE_MIN_WALLETS = 10
VOLUME_TREND_SNAPSHOTS = 3


def _f(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def _bucket_label(seconds: float, buckets: list[tuple[str, float | None]]) -> str:
    for label, upper in buckets:
        if upper is None or seconds < upper:
            return label
    return buckets[-1][0]


def _win(position: Position) -> bool:
    if position.roi is not None:
        return Decimal(str(position.roi)) > 0
    return Decimal(str(position.realized_pnl_sol or 0)) > 0


def _group_stats(positions: list[Position]) -> dict[str, Any]:
    n = len(positions)
    wins = sum(1 for p in positions if _win(p))
    rois = [float(p.roi) for p in positions if p.roi is not None]
    return {
        "n": n,
        "win_rate": wins / n if n else None,
        "avg_roi": float(np.mean(rois)) if rois else None,
    }


async def _closed_positions(
    session: AsyncSession, window_start: datetime
) -> list[Position]:
    rows = (
        (
            await session.execute(
                select(Position).where(
                    Position.status == "closed",
                    Position.closed_at.is_not(None),
                    # Window bound in SQL so the scan stays O(window); the
                    # Python check below remains as an exactness belt.
                    Position.closed_at >= sql_cutoff(session, window_start),
                )
            )
        )
        .scalars()
        .all()
    )
    return [p for p in rows if _aware(p.closed_at) >= window_start]  # type: ignore[arg-type]


def _detect_hour_of_day(
    positions: list[Position], overall: dict[str, Any], min_evidence: int
) -> list[dict[str, Any]]:
    by_hour: dict[int, list[Position]] = {}
    for p in positions:
        by_hour.setdefault(_aware(p.opened_at).hour, []).append(p)
    findings: list[dict[str, Any]] = []
    for hour in sorted(by_hour):
        group = _group_stats(by_hour[hour])
        if group["n"] < min_evidence:
            continue
        delta = (
            group["win_rate"] - overall["win_rate"]
            if group["win_rate"] is not None and overall["win_rate"] is not None
            else None
        )
        stats = {**group, **{f"overall_{k}": v for k, v in overall.items()}}
        stats["delta_win_rate"] = delta
        findings.append(
            {
                "kind": "hour_of_day",
                "key": {"hour": hour},
                "stats": stats,
                "evidence_count": group["n"],
                "description": (
                    f"Positions opened at {hour:02d}:00 UTC won "
                    f"{group['win_rate']:.1%} of the time over {group['n']} positions, "
                    f"vs a {overall['win_rate']:.1%} overall baseline "
                    f"(delta {delta:+.1%})."
                ),
            }
        )
    return findings


def _bucketed_findings(
    kind: str,
    grouped: dict[str, list[Position]],
    order: list[str],
    overall: dict[str, Any],
    min_evidence: int,
    describe: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for label in order:
        members = grouped.get(label, [])
        group = _group_stats(members)
        if group["n"] < min_evidence:
            continue
        stats = {**group, **{f"overall_{k}": v for k, v in overall.items()}}
        win_txt = f"{group['win_rate']:.1%}" if group["win_rate"] is not None else "n/a"
        roi_txt = f"{group['avg_roi']:.2f}" if group["avg_roi"] is not None else "n/a"
        findings.append(
            {
                "kind": kind,
                "key": {"bucket": label},
                "stats": stats,
                "evidence_count": group["n"],
                "description": (
                    f"{describe} bucket {label}: win rate {win_txt}, avg ROI {roi_txt} "
                    f"over {group['n']} positions (overall win rate "
                    f"{overall['win_rate']:.1%})."
                ),
            }
        )
    return findings


async def _detect_entry_delay(
    session: AsyncSession,
    positions: list[Position],
    overall: dict[str, Any],
    min_evidence: int,
) -> list[dict[str, Any]]:
    token_ids = {p.token_id for p in positions}
    if not token_ids:
        return []
    first_seen: dict[int, datetime] = {
        token_id: _aware(seen)
        for token_id, seen in await session.execute(
            select(Token.id, Token.first_seen_at).where(Token.id.in_(token_ids))
        )
    }
    grouped: dict[str, list[Position]] = {}
    for p in positions:
        seen = first_seen.get(p.token_id)
        if seen is None:
            continue
        delay = max((_aware(p.opened_at) - seen).total_seconds(), 0.0)
        grouped.setdefault(_bucket_label(delay, ENTRY_DELAY_BUCKETS), []).append(p)
    return _bucketed_findings(
        "entry_delay",
        grouped,
        [label for label, _ in ENTRY_DELAY_BUCKETS],
        overall,
        min_evidence,
        "Entry delay after token first seen",
    )


def _detect_position_size(
    positions: list[Position], overall: dict[str, Any], min_evidence: int
) -> list[dict[str, Any]]:
    sized = [p for p in positions if p.bought_sol is not None]
    if len(sized) < 2:
        return []
    sizes = np.array([float(p.bought_sol) for p in sized], dtype=float)
    edges = [float(e) for e in np.percentile(sizes, [0, 20, 40, 60, 80, 100])]
    findings: list[dict[str, Any]] = []
    for i in range(5):
        lo, hi = edges[i], edges[i + 1]
        members = [
            p
            for p in sized
            if (lo <= float(p.bought_sol) < hi)
            or (i == 4 and float(p.bought_sol) == hi)
        ]
        group = _group_stats(members)
        if group["n"] < min_evidence:
            continue
        stats = {**group, **{f"overall_{k}": v for k, v in overall.items()}}
        stats["edges"] = edges
        stats["quintile_min"] = lo
        stats["quintile_max"] = hi
        win_txt = f"{group['win_rate']:.1%}" if group["win_rate"] is not None else "n/a"
        findings.append(
            {
                "kind": "position_size",
                "key": {"quintile": i + 1, "min_sol": lo, "max_sol": hi},
                "stats": stats,
                "evidence_count": group["n"],
                "description": (
                    f"Position-size quintile {i + 1} ({lo:.4f}-{hi:.4f} SOL): win rate "
                    f"{win_txt} over {group['n']} positions (overall "
                    f"{overall['win_rate']:.1%})."
                ),
            }
        )
    return findings


def _detect_hold_time(
    positions: list[Position], overall: dict[str, Any], min_evidence: int
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Position]] = {}
    for p in positions:
        if p.hold_time_seconds is None:
            continue
        grouped.setdefault(
            _bucket_label(float(p.hold_time_seconds), HOLD_TIME_BUCKETS), []
        ).append(p)
    return _bucketed_findings(
        "hold_time",
        grouped,
        [label for label, _ in HOLD_TIME_BUCKETS],
        overall,
        min_evidence,
        "Hold time",
    )


async def _detect_whale_flow(
    session: AsyncSession,
    positions: list[Position],
    min_evidence: int,
    now: datetime,
) -> list[dict[str, Any]]:
    per_wallet: dict[int, list[float]] = {}
    for p in positions:
        per_wallet.setdefault(p.wallet_id, []).append(float(p.bought_sol or 0))
    if len(per_wallet) < WHALE_MIN_WALLETS:
        return []
    avgs = {w: float(np.mean(sizes)) for w, sizes in per_wallet.items()}
    threshold = float(np.percentile(np.array(list(avgs.values())), WHALE_DECILE_PCT))
    whales = [w for w, avg in avgs.items() if avg >= threshold]
    if not whales:
        return []
    cutoff = now - timedelta(hours=WHALE_FLOW_HOURS)
    rows = await session.execute(
        select(Trade.token_id, Trade.side, Trade.quote_amount, Trade.block_time).where(
            Trade.wallet_id.in_(whales),
            Trade.quote_mint == WSOL_MINT,
            Trade.block_time >= sql_cutoff(session, cutoff),
        )
    )
    net: dict[int, Decimal] = {}
    counts: dict[int, int] = {}
    for token_id, side, quote, block_time in rows:
        if _aware(block_time) < cutoff:
            continue
        amount = Decimal(str(quote))
        sign = 1 if side == Side.BUY.value else -1
        net[token_id] = net.get(token_id, Decimal(0)) + sign * amount
        counts[token_id] = counts.get(token_id, 0) + 1
    ranked = sorted(net.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    findings: list[dict[str, Any]] = []
    for token_id, flow in ranked[:WHALE_TOP_TOKENS]:
        if counts[token_id] < min_evidence:
            continue
        direction = "into" if flow >= 0 else "out of"
        findings.append(
            {
                "kind": "whale_flow",
                "key": {"token_id": token_id},
                "stats": {
                    "net_flow_sol": float(flow),
                    "trade_count": counts[token_id],
                    "whale_wallet_count": len(whales),
                    "whale_avg_size_threshold_sol": threshold,
                    "window_hours": WHALE_FLOW_HOURS,
                },
                "evidence_count": counts[token_id],
                "description": (
                    f"Top-decile wallets moved a net {abs(float(flow)):.4f} SOL "
                    f"{direction} token {token_id} across {counts[token_id]} trades "
                    f"in the last {WHALE_FLOW_HOURS}h."
                ),
            }
        )
    return findings


async def _detect_volume_trend(
    session: AsyncSession, window_start: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(TokenSnapshot.token_id, TokenSnapshot.ts, TokenSnapshot.volume_sol_1h).where(
            TokenSnapshot.ts >= sql_cutoff(session, window_start)
        )
    )
    by_token: dict[int, list[tuple[datetime, Decimal | None]]] = {}
    for token_id, ts, volume in rows:
        ts = _aware(ts)
        if ts < window_start:
            continue
        by_token.setdefault(token_id, []).append(
            (ts, None if volume is None else Decimal(str(volume)))
        )
    findings: list[dict[str, Any]] = []
    for token_id in sorted(by_token):
        series = sorted(by_token[token_id], key=lambda pair: pair[0])
        last = series[-VOLUME_TREND_SNAPSHOTS:]
        if len(last) < VOLUME_TREND_SNAPSHOTS:
            continue
        volumes = [v for _, v in last]
        if any(v is None for v in volumes):
            continue
        if not all(volumes[i] < volumes[i + 1] for i in range(len(volumes) - 1)):
            continue
        path = [float(v) for v in volumes]  # type: ignore[arg-type]
        findings.append(
            {
                "kind": "volume_trend",
                "key": {"token_id": token_id},
                "stats": {"volume_sol_1h_path": path, "snapshots": len(path)},
                "evidence_count": len(path),
                "description": (
                    f"Token {token_id} 1h volume rose monotonically over its last "
                    f"{len(path)} snapshots: "
                    f"{' -> '.join(f'{v:.4f}' for v in path)} SOL."
                ),
            }
        )
    return findings


async def run_once(
    session: AsyncSession,
    *,
    window_days: int,
    min_evidence: int,
    now: datetime | None = None,
) -> int:
    """One pattern-discovery pass; returns the number of pattern rows inserted."""
    now = _aware(now) if now is not None else datetime.now(tz=UTC)
    window_start = now - timedelta(days=window_days)

    positions = await _closed_positions(session, window_start)
    overall = _group_stats(positions)

    findings: list[dict[str, Any]] = []
    if positions:
        findings.extend(_detect_hour_of_day(positions, overall, min_evidence))
        findings.extend(
            await _detect_entry_delay(session, positions, overall, min_evidence)
        )
        findings.extend(_detect_position_size(positions, overall, min_evidence))
        findings.extend(_detect_hold_time(positions, overall, min_evidence))
        findings.extend(await _detect_whale_flow(session, positions, min_evidence, now))
    findings.extend(await _detect_volume_trend(session, window_start))

    if not findings:
        log.info("patterns_cycle", inserted=0, closed_positions=len(positions))
        return 0

    rows = [
        {
            **finding,
            "window_start": window_start,
            "window_end": now,
            "computed_at": now,
        }
        for finding in findings
    ]
    await session.execute(insert(DiscoveredPattern), rows)
    await session.commit()
    log.info(
        "patterns_cycle",
        inserted=len(rows),
        closed_positions=len(positions),
        kinds=sorted({row["kind"] for row in rows}),
    )
    return len(rows)
