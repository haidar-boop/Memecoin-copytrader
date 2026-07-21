"""Alert rules: pure evaluation of a monitoring context into Alert records.

``evaluate_alerts`` is a deterministic pure function of a plain ``context``
dict, so it is trivial to unit-test at boundary values and reuse from the
system API, background workers, or the Telegram notifier without touching a
database or Redis.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]

# --- module-constant thresholds -------------------------------------------
INGESTION_STALL_MINUTES = 15.0
FAILED_TX_RATIO = 0.5
DAILY_LOSS_FRACTION = 0.8
MODEL_AUC_FLOOR = 0.55
REGIME_STALE_MINUTES = 120.0


class Alert(BaseModel):
    name: str
    severity: Severity
    message: str
    value: float | None = None
    threshold: float | None = None


def _num(context: dict, key: str, default: float = 0.0) -> float:
    value = context.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_alerts(context: dict) -> list[Alert]:
    """Evaluate every alert rule against ``context`` and return those firing.

    Context keys (all optional; missing keys default to a non-firing value):
      - minutes_since_last_trade: float
      - failed_tx_1h: int, trades_1h: int
      - emergency_stop: truthy => copy trading halted
      - daily_pnl_sol: float (negative = loss), daily_loss_limit_sol: float
      - latest_model_auc: float | None
      - minutes_since_last_regime: float
    """
    alerts: list[Alert] = []

    # ingestion_stalled -----------------------------------------------------
    # None => no trades ever (fresh deploy), which is not a stall.
    stall = context.get("minutes_since_last_trade")
    if stall is not None and float(stall) > INGESTION_STALL_MINUTES:
        alerts.append(
            Alert(
                name="ingestion_stalled",
                severity="critical",
                message=f"No new trades for {stall:.1f} min (> {INGESTION_STALL_MINUTES:.0f}).",
                value=stall,
                threshold=INGESTION_STALL_MINUTES,
            )
        )

    # high_failed_tx_ratio --------------------------------------------------
    trades_1h = _num(context, "trades_1h", 0.0)
    failed_1h = _num(context, "failed_tx_1h", 0.0)
    ratio = failed_1h / max(trades_1h, 1.0)
    if ratio > FAILED_TX_RATIO:
        alerts.append(
            Alert(
                name="high_failed_tx_ratio",
                severity="warning",
                message=(
                    f"Failed-tx ratio {ratio:.2f} over last hour "
                    f"({failed_1h:.0f} failed / {trades_1h:.0f} trades)."
                ),
                value=ratio,
                threshold=FAILED_TX_RATIO,
            )
        )

    # copy_emergency_stop_active -------------------------------------------
    if context.get("emergency_stop"):
        alerts.append(
            Alert(
                name="copy_emergency_stop_active",
                severity="critical",
                message="Copy-trading emergency stop is active.",
                value=None,
                threshold=None,
            )
        )

    # daily_loss_near_limit -------------------------------------------------
    limit = _num(context, "daily_loss_limit_sol", 0.0)
    daily_pnl = _num(context, "daily_pnl_sol", 0.0)
    if limit > 0:
        loss_threshold = -DAILY_LOSS_FRACTION * limit
        if daily_pnl <= loss_threshold:
            alerts.append(
                Alert(
                    name="daily_loss_near_limit",
                    severity="warning",
                    message=(
                        f"Daily realized PnL {daily_pnl:.4f} SOL at/near loss limit "
                        f"({limit:.4f} SOL)."
                    ),
                    value=daily_pnl,
                    threshold=loss_threshold,
                )
            )

    # model_accuracy_degraded ----------------------------------------------
    auc = context.get("latest_model_auc")
    if auc is not None:
        auc_f = float(auc)
        if auc_f < MODEL_AUC_FLOOR:
            alerts.append(
                Alert(
                    name="model_accuracy_degraded",
                    severity="warning",
                    message=f"Latest model AUC {auc_f:.3f} below floor {MODEL_AUC_FLOOR}.",
                    value=auc_f,
                    threshold=MODEL_AUC_FLOOR,
                )
            )

    # no_recent_regime ------------------------------------------------------
    # None => the detector has never run yet (fresh deploy), not "stale".
    regime_age = context.get("minutes_since_last_regime")
    if regime_age is not None and float(regime_age) > REGIME_STALE_MINUTES:
        alerts.append(
            Alert(
                name="no_recent_regime",
                severity="info",
                message=f"No market regime detected for {float(regime_age):.0f} min.",
                value=float(regime_age),
                threshold=REGIME_STALE_MINUTES,
            )
        )

    return alerts
