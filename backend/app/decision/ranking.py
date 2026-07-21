"""Adaptive wallet ranking from realized copy outcomes.

Exponentially-weighted adjustment per leader wallet, multiplied into the
static confidence score at evaluation time: leaders whose copied trades lose
money get demoted quickly and recover gradually as fresh wins arrive.
Small-sample guard: no adjustment until MIN_OUTCOMES results exist.

State lives in Redis (survives worker restarts; cheap to update per close);
every applied adjustment is also recorded in the decision's factor list, so
history remains auditable through trade_decisions.
"""

from __future__ import annotations

import json
from typing import Protocol

from app.logging_config import get_logger

log = get_logger(__name__)

RANKING_KEY_PREFIX = "copy:ranking:"  # + leader wallet id

EW_ALPHA = 0.25  # weight of the newest outcome
MIN_OUTCOMES = 5  # small-sample guard
ADJUST_MIN = 0.5  # hard floor: never erase a wallet entirely on a bad streak
ADJUST_MAX = 1.2  # modest boost cap: evidence accrues slowly upward


class _Redis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ex: int | None = None) -> object: ...


async def record_outcome(redis: _Redis, leader_wallet_id: int, roi: float) -> None:
    """Fold one closed copy position's ROI into the leader's EW score."""
    key = RANKING_KEY_PREFIX + str(leader_wallet_id)
    raw = await redis.get(key)
    state = json.loads(raw) if raw else {"ew_roi": 0.0, "n": 0}
    state["ew_roi"] = (1 - EW_ALPHA) * float(state["ew_roi"]) + EW_ALPHA * roi
    state["n"] = int(state["n"]) + 1
    await redis.set(key, json.dumps(state))
    log.info(
        "ranking_outcome_recorded",
        leader_wallet_id=leader_wallet_id,
        roi=round(roi, 4),
        ew_roi=round(state["ew_roi"], 4),
        n=state["n"],
    )


async def adjustment_factor(redis: _Redis, leader_wallet_id: int) -> tuple[float, str]:
    """Multiplier applied to the leader's confidence, with an explanation."""
    raw = await redis.get(RANKING_KEY_PREFIX + str(leader_wallet_id))
    if not raw:
        return 1.0, "no copy outcomes yet"
    state = json.loads(raw)
    n, ew_roi = int(state.get("n", 0)), float(state.get("ew_roi", 0.0))
    if n < MIN_OUTCOMES:
        return 1.0, f"only {n} outcomes (< {MIN_OUTCOMES}), no adjustment"
    # ew_roi -0.5 -> ~0.5x, 0 -> 1.0x, +0.5 -> ~1.2x (asymmetric by design:
    # losing streaks demote faster than winning streaks promote).
    if ew_roi < 0:
        factor = max(ADJUST_MIN, 1.0 + ew_roi)
    else:
        factor = min(ADJUST_MAX, 1.0 + ew_roi * 0.4)
    return factor, f"EW ROI {ew_roi:+.3f} over {n} copied outcomes -> x{factor:.2f}"
