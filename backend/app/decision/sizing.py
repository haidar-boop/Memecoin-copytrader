"""Position sizing for copy trades. Pure functions."""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.db.util import quantize_sol

# A trade smaller than this is dust: fees dominate and the position is
# effectively unsellable. Below it the trade is not taken.
MIN_TRADE_SOL = Decimal("0.001")


def copy_size_sol(
    settings: Settings,
    *,
    leader_size_sol: Decimal,
    current_exposure_sol: Decimal,
) -> tuple[Decimal, list[str]]:
    """Size for one copy buy, clamped by every limit. Returns (size, notes).

    A zero size means the trade cannot be taken within limits.
    """
    notes: list[str] = []
    if settings.copy_size_mode == "percent":
        size = leader_size_sol * Decimal(str(settings.copy_percent_of_leader)) / 100
        notes.append(f"{settings.copy_percent_of_leader}% of leader {leader_size_sol} SOL")
    else:
        size = Decimal(str(settings.copy_fixed_sol))
        notes.append(f"fixed {size} SOL")

    max_position = Decimal(str(settings.copy_max_position_sol))
    if size > max_position:
        notes.append(f"clamped to max position {max_position} SOL")
        size = max_position

    remaining = Decimal(str(settings.copy_max_exposure_sol)) - current_exposure_sol
    if remaining <= 0:
        notes.append("no exposure headroom")
        return Decimal(0), notes
    if size > remaining:
        notes.append(f"clamped to exposure headroom {remaining} SOL")
        size = remaining

    # Quantize to lamport precision (percent mode can produce >9 dp) so the
    # stored size and the executed lamport amount agree exactly.
    size = quantize_sol(max(size, Decimal(0)))
    if 0 < size < MIN_TRADE_SOL:
        notes.append(f"below dust floor {MIN_TRADE_SOL} SOL")
        return Decimal(0), notes
    return size, notes
