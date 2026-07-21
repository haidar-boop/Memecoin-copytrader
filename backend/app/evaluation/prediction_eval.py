"""Prediction evaluation engine: predicted vs actual, error metrics, calibration.

Two append-only steps run periodically:

* :func:`resolve_outcomes` joins each unresolved trade :class:`Prediction` to the
  realized :class:`Position` it referenced (the earliest close after the
  prediction, within the label horizon) and records one
  :class:`PredictionOutcome` per resolved prediction. Idempotent: the unique
  ``prediction_id`` plus ``bulk_append(ignore_conflicts=True)`` makes reruns
  no-ops. Predictions whose position never closed inside the horizon are left
  unresolved (skipped), so a later close can still resolve them.

* :func:`compute_model_performance` aggregates resolved outcomes per model over
  a trailing window into one :class:`ModelPerformance` row (AUC, Brier,
  accuracy, base rate, mean ROI error, calibration curve). Models with fewer
  than ``min_resolved`` resolved outcomes are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from sklearn.metrics import roc_auc_score
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MlModel,
    ModelPerformance,
    Position,
    Prediction,
    PredictionOutcome,
)
from app.db.util import aware, bulk_append, sql_cutoff, to_decimal, to_float
from app.logging_config import get_logger

log = get_logger(__name__)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


async def resolve_outcomes(
    session: AsyncSession,
    *,
    label_horizon_hours: int,
    batch: int,
    now: datetime | None = None,
) -> int:
    """Resolve unresolved trade predictions against their closed positions.

    Returns the number of :class:`PredictionOutcome` rows inserted this run.
    """
    resolved_at = _now(now)
    horizon = timedelta(hours=label_horizon_hours)

    # Anti-join on the indexed prediction_id (NOT EXISTS), not NOT IN over the
    # whole outcomes table — the latter re-scans every resolved row each cycle
    # and grows unbounded as outcomes accumulate.
    already_resolved = (
        select(PredictionOutcome.id)
        .where(PredictionOutcome.prediction_id == Prediction.id)
        .exists()
    )
    predictions = (
        (
            await session.execute(
                select(Prediction)
                .where(
                    Prediction.subject_type == "trade",
                    ~already_resolved,
                )
                .order_by(Prediction.created_at)
                .limit(batch)
            )
        )
        .scalars()
        .all()
    )
    if not predictions:
        return 0

    # Load candidate closed positions for the referenced pairs in one query.
    pairs = {
        (p.wallet_id, p.token_id)
        for p in predictions
        if p.wallet_id is not None and p.token_id is not None
    }
    positions_by_pair: dict[tuple[int, int], list[Position]] = {}
    if pairs:
        wallet_ids = {w for w, _ in pairs}
        token_ids = {t for _, t in pairs}
        candidates = (
            (
                await session.execute(
                    select(Position).where(
                        Position.status == "closed",
                        Position.wallet_id.in_(wallet_ids),
                        Position.token_id.in_(token_ids),
                        Position.closed_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for position in candidates:
            key = (position.wallet_id, position.token_id)
            if key in pairs:
                positions_by_pair.setdefault(key, []).append(position)
        for episodes in positions_by_pair.values():
            episodes.sort(key=lambda pos: aware(pos.closed_at))

    rows: list[dict] = []
    for prediction in predictions:
        if prediction.wallet_id is None or prediction.token_id is None:
            continue
        created = aware(prediction.created_at)
        deadline = created + horizon
        episodes = positions_by_pair.get(
            (prediction.wallet_id, prediction.token_id), []
        )
        # A prediction is ABOUT the episode active at the buy that triggered
        # it — the one whose open time is nearest the prediction. Picking the
        # earliest close after the prediction instead would attribute a
        # re-entering wallet's PRIOR episode to a new prediction. Resolve only
        # when THAT episode closed within the horizon; otherwise leave the
        # prediction unresolved (a later close can still resolve it).
        match = min(
            episodes,
            key=lambda pos: abs((aware(pos.opened_at) - created).total_seconds()),
            default=None,
        )
        if match is None or not (created < aware(match.closed_at) <= deadline):
            continue

        predicted = prediction.predicted or {}
        prob = to_float(predicted.get("p_profit"))
        predicted_roi = to_decimal(predicted.get("roi"))
        realized_pnl = to_decimal(match.realized_pnl_sol)
        actual_label = 1 if (realized_pnl is not None and realized_pnl > 0) else 0
        actual_roi = to_decimal(match.roi)

        brier = None if prob is None else Decimal(str((prob - actual_label) ** 2))
        roi_error = (
            actual_roi - predicted_roi
            if actual_roi is not None and predicted_roi is not None
            else None
        )

        rows.append(
            {
                "prediction_id": prediction.id,
                "model_id": prediction.model_id,
                "resolved_at": resolved_at,
                "subject_type": prediction.subject_type,
                "predicted_prob": None if prob is None else to_decimal(prob),
                "predicted_roi": predicted_roi,
                "predicted_hold_seconds": None,
                "actual_label": actual_label,
                "actual_roi": actual_roi,
                "actual_hold_seconds": match.hold_time_seconds,
                "roi_error": roi_error,
                "hold_error_seconds": None,
                "brier": brier,
            }
        )

    await bulk_append(session, PredictionOutcome, rows, ignore_conflicts=True)
    await session.commit()
    if rows:
        log.info("resolved_prediction_outcomes", count=len(rows))
    return len(rows)


def _calibration_curve(
    probs: np.ndarray, labels: np.ndarray, bins: int
) -> list[dict]:
    """Equal-width probability bins over [0, 1]; membership sums to len(probs)."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    # Right-closed bins so p == 1.0 lands in the top bin.
    idx = np.clip(np.searchsorted(edges, probs, side="left") - 1, 0, bins - 1)
    curve: list[dict] = []
    for b in range(bins):
        mask = idx == b
        n = int(mask.sum())
        curve.append(
            {
                "p_bin": round(float((edges[b] + edges[b + 1]) / 2.0), 6),
                "predicted": round(float(probs[mask].mean()), 6) if n else None,
                "observed": round(float(labels[mask].mean()), 6) if n else None,
                "n": n,
            }
        )
    return curve


async def compute_model_performance(
    session: AsyncSession,
    *,
    window_days: int,
    min_resolved: int,
    bins: int,
    now: datetime | None = None,
) -> list[ModelPerformance]:
    """Aggregate resolved outcomes per model over the trailing window.

    Inserts and returns one :class:`ModelPerformance` row per qualifying model.
    """
    ts = _now(now)
    cutoff = sql_cutoff(session, ts - timedelta(days=window_days))

    outcomes = (
        (
            await session.execute(
                select(PredictionOutcome).where(
                    PredictionOutcome.resolved_at >= cutoff,
                    PredictionOutcome.predicted_prob.is_not(None),
                    PredictionOutcome.actual_label.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    by_model: dict[int, list[PredictionOutcome]] = {}
    for outcome in outcomes:
        by_model.setdefault(outcome.model_id, []).append(outcome)

    model_names = dict(
        (
            await session.execute(select(MlModel.id, MlModel.name))
        ).all()
    )

    performances: list[ModelPerformance] = []
    rows: list[dict] = []
    for model_id, model_outcomes in sorted(by_model.items()):
        resolved_count = len(model_outcomes)
        if resolved_count < min_resolved:
            continue

        probs = np.array(
            [to_float(o.predicted_prob) for o in model_outcomes], dtype=float
        )
        labels = np.array(
            [int(o.actual_label) for o in model_outcomes], dtype=int
        )
        briers = [to_float(o.brier) for o in model_outcomes]
        brier_vals = [b for b in briers if b is not None]
        roi_errors = [to_decimal(o.roi_error) for o in model_outcomes]
        roi_err_vals = [r for r in roi_errors if r is not None]

        auc = None
        if len(np.unique(labels)) > 1:
            auc = float(roc_auc_score(labels, probs))

        brier = float(np.mean(brier_vals)) if brier_vals else None
        # Threshold at 0.5 (>=), not np.round: numpy's banker's rounding sends
        # an exactly-0.5 prediction to class 0, silently biasing the tie case.
        accuracy = float(np.mean((probs >= 0.5).astype(int) == labels))
        base_rate = float(labels.mean())
        mean_roi_error = (
            float(sum(roi_err_vals) / len(roi_err_vals)) if roi_err_vals else None
        )
        calibration = _calibration_curve(probs, labels, bins)

        row = {
            "ts": ts,
            "model_id": model_id,
            "model_name": model_names.get(model_id, str(model_id)),
            "window_days": window_days,
            "resolved_count": resolved_count,
            "auc": None if auc is None else to_decimal(auc),
            "brier": None if brier is None else to_decimal(brier),
            "accuracy": to_decimal(accuracy),
            "base_rate": to_decimal(base_rate),
            "mean_roi_error": None
            if mean_roi_error is None
            else to_decimal(mean_roi_error),
            "calibration": calibration,
        }
        rows.append(row)
        performances.append(ModelPerformance(**row))

    await bulk_append(session, ModelPerformance, rows)
    await session.commit()
    if rows:
        log.info("computed_model_performance", models=len(rows))
    return performances
