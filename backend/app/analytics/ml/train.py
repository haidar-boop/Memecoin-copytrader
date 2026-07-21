"""Walk-forward training for both models, wired for the analytics job loop.

``retrain_once`` trains "trade_profit" and "wallet_persistence" in one pass.
Each model uses a time-ordered THREE-way split (70/15/15): fit a
gradient-boosted classifier on the earliest 70% of rows, fit the isotonic
calibration (FrozenEstimator + CalibratedClassifierCV, sklearn 1.9) on the
middle 15%, and report ROC-AUC / Brier / base rate on the final 15% — data
neither the model nor the calibrator has ever seen, so registry promotion
decisions rest on honest numbers. Artifacts and MlModel rows go through
:mod:`app.analytics.ml.registry`, which only promotes a version that beats
the reigning AUC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import brier_score_loss, roc_auc_score
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.ml import dataset, registry
from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)

ALGO = "hist_gradient_boosting+isotonic_calibration"
MODEL_PARAMS: dict[str, Any] = {"max_iter": 150, "random_state": 42}
TRAIN_FRACTION = 0.70
CALIBRATION_FRACTION = 0.15  # remainder is the untouched evaluation slice


def _train_and_evaluate(
    matrix: list[list[float]], labels: list[int]
) -> tuple[Any, dict[str, float], int] | str:
    """Fit + calibrate + evaluate on disjoint time slices.

    The evaluation slice is seen by neither the base model nor the isotonic
    calibrator — scoring the slice the calibrator was fit on lets isotonic
    regression memorize the labels and report fantasy AUC/Brier.
    """
    X = np.asarray(matrix, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int64)
    train_end = int(len(y) * TRAIN_FRACTION)
    cal_end = int(len(y) * (TRAIN_FRACTION + CALIBRATION_FRACTION))
    X_train, y_train = X[:train_end], y[:train_end]
    X_cal, y_cal = X[train_end:cal_end], y[train_end:cal_end]
    X_eval, y_eval = X[cal_end:], y[cal_end:]
    if (
        len(set(y_train.tolist())) < 2
        or len(set(y_cal.tolist())) < 2
        or len(set(y_eval.tolist())) < 2
    ):
        return "single_class_split"

    base = HistGradientBoostingClassifier(**MODEL_PARAMS)
    base.fit(X_train, y_train)
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    calibrated.fit(X_cal, y_cal)

    probabilities = calibrated.predict_proba(X_eval)[:, 1]
    metrics = {
        "roc_auc": float(roc_auc_score(y_eval, probabilities)),
        "brier": float(brier_score_loss(y_eval, probabilities)),
        "base_rate": float(y_eval.mean()),
        "train_rows": int(train_end),
        "calibration_rows": int(cal_end - train_end),
        "eval_rows": int(len(y_eval)),
    }
    return calibrated, metrics, len(y)


async def _retrain_model(
    session: AsyncSession,
    settings: Settings,
    *,
    name: str,
    feature_names: list[str],
    matrix: list[list[float]],
    labels: list[int],
    now: datetime,
) -> dict[str, Any]:
    if len(matrix) < settings.ml_min_training_rows:
        reason = f"{len(matrix)} rows < ml_min_training_rows={settings.ml_min_training_rows}"
        log.info("ml_retrain_skipped", model=name, reason=reason)
        return {"skipped": reason}

    outcome = _train_and_evaluate(matrix, labels)
    if isinstance(outcome, str):
        log.info("ml_retrain_skipped", model=name, reason=outcome)
        return {"skipped": outcome}
    estimator, metrics, rows = outcome

    row = await registry.save_model(
        session,
        name=name,
        estimator=estimator,
        algo=ALGO,
        model_dir=settings.ml_model_dir,
        training_rows=rows,
        params=MODEL_PARAMS,
        metrics=metrics,
        feature_names=feature_names,
        trained_at=now,
    )
    return {
        "model_id": row.id,
        "version": row.version,
        "is_active": row.is_active,
        "training_rows": rows,
        "metrics": metrics,
    }


async def retrain_once(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    """One retraining pass over both models; returns a per-model report."""
    now = now or datetime.now(tz=UTC)

    trade_names, trade_X, trade_y, _ = await dataset.build_trade_profit_dataset(
        session, label_horizon_hours=settings.ml_label_horizon_hours, now=now
    )
    trade_report = await _retrain_model(
        session,
        settings,
        name="trade_profit",
        feature_names=trade_names,
        matrix=trade_X,
        labels=trade_y,
        now=now,
    )

    wallet_names, wallet_X, wallet_y, _ = await dataset.build_wallet_persistence_dataset(
        session, now=now
    )
    wallet_report = await _retrain_model(
        session,
        settings,
        name="wallet_persistence",
        feature_names=wallet_names,
        matrix=wallet_X,
        labels=wallet_y,
        now=now,
    )

    return {"trade_profit": trade_report, "wallet_persistence": wallet_report}
