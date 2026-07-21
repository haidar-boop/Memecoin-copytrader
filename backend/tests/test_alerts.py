"""Boundary tests for the pure alert rule engine."""

from __future__ import annotations

from app.services.alerts import (
    DAILY_LOSS_FRACTION,
    INGESTION_STALL_MINUTES,
    MODEL_AUC_FLOOR,
    REGIME_STALE_MINUTES,
    evaluate_alerts,
)


def _names(context: dict) -> set[str]:
    return {a.name for a in evaluate_alerts(context)}


def test_empty_context_fires_nothing() -> None:
    assert evaluate_alerts({}) == []


def test_ingestion_stalled_boundary() -> None:
    assert "ingestion_stalled" not in _names({"minutes_since_last_trade": INGESTION_STALL_MINUTES})
    assert "ingestion_stalled" in _names({"minutes_since_last_trade": INGESTION_STALL_MINUTES + 1})


def test_failed_tx_ratio_boundary() -> None:
    # exactly 0.5 does NOT fire (strictly greater than)
    assert "high_failed_tx_ratio" not in _names({"failed_tx_1h": 5, "trades_1h": 10})
    assert "high_failed_tx_ratio" in _names({"failed_tx_1h": 6, "trades_1h": 10})


def test_failed_tx_ratio_zero_trades_no_divide_by_zero() -> None:
    # max(trades,1) -> 1 in the denominator
    assert "high_failed_tx_ratio" in _names({"failed_tx_1h": 2, "trades_1h": 0})
    assert "high_failed_tx_ratio" not in _names({"failed_tx_1h": 0, "trades_1h": 0})


def test_emergency_stop_toggle() -> None:
    assert "copy_emergency_stop_active" in _names({"emergency_stop": "1"})
    assert "copy_emergency_stop_active" in _names({"emergency_stop": True})
    assert "copy_emergency_stop_active" not in _names({"emergency_stop": ""})
    assert "copy_emergency_stop_active" not in _names({"emergency_stop": False})


def test_daily_loss_near_limit_boundary() -> None:
    limit = 1.0
    at = -DAILY_LOSS_FRACTION * limit  # -0.8
    # exactly at the threshold fires (<=)
    assert "daily_loss_near_limit" in _names(
        {"daily_pnl_sol": at, "daily_loss_limit_sol": limit}
    )
    assert "daily_loss_near_limit" not in _names(
        {"daily_pnl_sol": at + 0.01, "daily_loss_limit_sol": limit}
    )
    # a profit never fires
    assert "daily_loss_near_limit" not in _names(
        {"daily_pnl_sol": 0.5, "daily_loss_limit_sol": limit}
    )


def test_daily_loss_no_limit_configured() -> None:
    assert "daily_loss_near_limit" not in _names(
        {"daily_pnl_sol": -100.0, "daily_loss_limit_sol": 0.0}
    )


def test_model_accuracy_boundary() -> None:
    assert "model_accuracy_degraded" not in _names({"latest_model_auc": MODEL_AUC_FLOOR})
    assert "model_accuracy_degraded" in _names({"latest_model_auc": MODEL_AUC_FLOOR - 0.01})
    # None (no model yet) never fires
    assert "model_accuracy_degraded" not in _names({"latest_model_auc": None})


def test_no_recent_regime_boundary() -> None:
    assert "no_recent_regime" not in _names(
        {"minutes_since_last_regime": REGIME_STALE_MINUTES}
    )
    assert "no_recent_regime" in _names(
        {"minutes_since_last_regime": REGIME_STALE_MINUTES + 1}
    )


def test_multiple_alerts_fire_together() -> None:
    fired = _names(
        {
            "minutes_since_last_trade": 60,
            "failed_tx_1h": 9,
            "trades_1h": 10,
            "emergency_stop": True,
            "daily_pnl_sol": -1.0,
            "daily_loss_limit_sol": 1.0,
            "latest_model_auc": 0.4,
            "minutes_since_last_regime": 300,
        }
    )
    assert fired == {
        "ingestion_stalled",
        "high_failed_tx_ratio",
        "copy_emergency_stop_active",
        "daily_loss_near_limit",
        "model_accuracy_degraded",
        "no_recent_regime",
    }


def test_alert_fields_populated() -> None:
    (alert,) = [
        a
        for a in evaluate_alerts({"minutes_since_last_trade": 30})
        if a.name == "ingestion_stalled"
    ]
    assert alert.severity == "critical"
    assert alert.value == 30.0
    assert alert.threshold == INGESTION_STALL_MINUTES
