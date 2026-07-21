"""Evidence-gated wallet confidence scoring (0-100) with full explanations.

Design principles:
- Bayesian shrinkage everywhere: small samples pull hard toward pessimistic
  priors, so a wallet cannot look brilliant off three lucky trades.
- Every component reports its raw input, weight, and contribution — the API
  serves this payload verbatim so the UI can always answer "why this score".
- Pure function of already-computed metrics: no I/O, trivially testable, and
  reproducible from any historical wallet_stats_snapshot row.

Score anatomy (weights sum to 100):
- win_rate (35): Beta-shrunk win rate vs a pessimistic memecoin prior.
- profitability (30): profit factor squashed to [0, 1] via pf/(pf+1).
- consistency (15): low dispersion of per-position ROI scores higher.
- recency (10): direction and rough magnitude of the trailing 30d PNL.
- experience (10): saturating closed-position count.
Finally the whole score shrinks toward the neutral prior by n/(n+K) so
low-sample wallets land near PRIOR_SCORE regardless of raw components.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

# Pessimistic base rate: most memecoin positions lose.
WIN_RATE_PRIOR = 0.35
WIN_RATE_PRIOR_STRENGTH = 10.0  # pseudo-observations behind the prior

PRIOR_SCORE = 30.0  # where a no-evidence wallet lands
GLOBAL_SHRINK_K = 10.0  # closed positions needed to trust half the deviation

EXPERIENCE_HALF_LIFE = 20.0  # closed positions at which experience = 0.5

WEIGHTS = {
    "win_rate": 35.0,
    "profitability": 30.0,
    "consistency": 15.0,
    "recency": 10.0,
    "experience": 10.0,
}


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def score_wallet(stats: dict[str, Any]) -> tuple[Decimal, list[dict]]:
    """Score a wallet from its metric dict (WalletStats column names).

    Returns ``(score 0-100, components)`` where components is a JSON-ready
    list explaining every term, plus the final sample-size shrinkage step.
    """
    closed = max(int(_f(stats.get("closed_position_count")) or 0), 0)
    wins = min(max(int(_f(stats.get("win_count")) or 0), 0), closed) if closed else 0

    components: list[dict] = []

    # --- win rate, Beta-shrunk --------------------------------------------
    shrunk_win_rate = (wins + WIN_RATE_PRIOR * WIN_RATE_PRIOR_STRENGTH) / (
        closed + WIN_RATE_PRIOR_STRENGTH
    )
    # Normalize against the prior so "at prior" contributes half weight.
    win_component = min(max(shrunk_win_rate / (2 * WIN_RATE_PRIOR), 0.0), 1.0)
    components.append(
        {
            "component": "win_rate",
            "raw": round(shrunk_win_rate, 4),
            "weight": WEIGHTS["win_rate"],
            "value": round(win_component, 4),
            "note": f"{wins}/{closed} wins shrunk toward prior {WIN_RATE_PRIOR}",
        }
    )

    # --- profitability ----------------------------------------------------
    profit_factor = _f(stats.get("profit_factor"))
    if profit_factor is not None:
        profit_component = profit_factor / (profit_factor + 1.0)
        profit_note = f"profit factor {profit_factor:.3f}"
    elif wins > 0:
        # profit_factor is None exactly when gross loss is zero: any
        # non-winning closed positions were break-even, not losses.
        profit_component = 0.9  # undefeated so far; cap short of certainty
        profit_note = "no losing positions yet (capped)"
    else:
        profit_component = 0.5
        profit_note = "no realized profit/loss evidence"
    components.append(
        {
            "component": "profitability",
            "raw": profit_factor,
            "weight": WEIGHTS["profitability"],
            "value": round(profit_component, 4),
            "note": profit_note,
        }
    )

    # --- consistency ------------------------------------------------------
    roi_std = _f(stats.get("roi_std"))
    if roi_std is not None and closed >= 3:
        consistency_component = 1.0 / (1.0 + roi_std)
        consistency_note = f"per-position ROI std {roi_std:.3f}"
    else:
        consistency_component = 0.5
        consistency_note = "insufficient closed positions for dispersion"
    components.append(
        {
            "component": "consistency",
            "raw": roi_std,
            "weight": WEIGHTS["consistency"],
            "value": round(consistency_component, 4),
            "note": consistency_note,
        }
    )

    # --- recency ----------------------------------------------------------
    pnl_30d = _f(stats.get("pnl_30d_sol"))
    if pnl_30d is not None:
        recency_component = 0.5 * (1.0 + pnl_30d / (abs(pnl_30d) + 1.0))
        recency_note = f"30d realized pnl {pnl_30d:+.3f} SOL"
    else:
        recency_component = 0.5
        recency_note = "no 30d pnl data"
    components.append(
        {
            "component": "recency",
            "raw": pnl_30d,
            "weight": WEIGHTS["recency"],
            "value": round(recency_component, 4),
            "note": recency_note,
        }
    )

    # --- experience -------------------------------------------------------
    experience_component = closed / (closed + EXPERIENCE_HALF_LIFE)
    components.append(
        {
            "component": "experience",
            "raw": closed,
            "weight": WEIGHTS["experience"],
            "value": round(experience_component, 4),
            "note": f"{closed} closed positions",
        }
    )

    raw_score = sum(entry["weight"] * entry["value"] for entry in components)

    # --- global sample-size shrinkage toward the neutral prior ------------
    shrink = closed / (closed + GLOBAL_SHRINK_K)
    final = PRIOR_SCORE + (raw_score - PRIOR_SCORE) * shrink
    final = min(max(final, 0.0), 100.0)
    components.append(
        {
            "component": "sample_shrinkage",
            "raw": closed,
            "weight": None,
            "value": round(shrink, 4),
            "note": (
                f"raw {raw_score:.1f} shrunk toward prior {PRIOR_SCORE:.0f} "
                f"with n/(n+{GLOBAL_SHRINK_K:.0f})"
            ),
        }
    )
    for entry in components:
        contribution = (
            entry["weight"] * entry["value"] if entry["weight"] is not None else None
        )
        entry["contribution"] = round(contribution, 2) if contribution is not None else None

    return Decimal(f"{final:.2f}"), components
