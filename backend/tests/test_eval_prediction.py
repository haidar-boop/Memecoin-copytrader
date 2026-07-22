"""Tests for the prediction evaluation engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import func, insert, select

from app.db.models import (
    MlModel,
    ModelPerformance,
    Position,
    Prediction,
    PredictionOutcome,
)
from app.evaluation.prediction_eval import (
    compute_model_performance,
    resolve_outcomes,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


async def _model(session, name: str, version: int) -> int:
    result = await session.execute(
        insert(MlModel).returning(MlModel.id),
        {
            "name": name,
            "version": version,
            "algo": "logreg",
            "trained_at": NOW - timedelta(days=1),
            "training_rows": 1000,
        },
    )
    return result.scalar_one()


async def _prediction(
    session, *, model_id: int, wallet_id: int, token_id: int, prob: float, created: datetime
) -> int:
    result = await session.execute(
        insert(Prediction).returning(Prediction.id),
        {
            "model_id": model_id,
            "created_at": created,
            "subject_type": "trade",
            "signature": f"sig-{wallet_id}-{token_id}",
            "wallet_id": wallet_id,
            "token_id": token_id,
            "predicted": {"p_profit": prob},
            "context": {},
        },
    )
    return result.scalar_one()


async def _closed_position(
    session,
    *,
    wallet_id: int,
    token_id: int,
    opened: datetime,
    closed: datetime,
    pnl: str,
    roi: str,
    hold: int,
) -> None:
    await session.execute(
        insert(Position),
        {
            "wallet_id": wallet_id,
            "token_id": token_id,
            "status": "closed",
            "opened_at": opened,
            "closed_at": closed,
            "realized_pnl_sol": Decimal(pnl),
            "roi": Decimal(roi),
            "hold_time_seconds": hold,
        },
    )


@pytest.mark.asyncio
async def test_resolve_outcomes_fields_and_idempotent(db_session):
    model_id = await _model(db_session, "trade_profit", 1)
    created = NOW - timedelta(hours=10)

    # Winner and loser predictions with matching closed positions.
    pid_win = await _prediction(
        db_session, model_id=model_id, wallet_id=1, token_id=10, prob=0.8, created=created
    )
    await _closed_position(
        db_session,
        wallet_id=1,
        token_id=10,
        opened=created - timedelta(hours=1),
        closed=created + timedelta(hours=2),
        pnl="1.5",
        roi="0.4",
        hold=7200,
    )
    pid_lose = await _prediction(
        db_session, model_id=model_id, wallet_id=2, token_id=20, prob=0.3, created=created
    )
    await _closed_position(
        db_session,
        wallet_id=2,
        token_id=20,
        opened=created - timedelta(hours=1),
        closed=created + timedelta(hours=2),
        pnl="-0.5",
        roi="-0.3",
        hold=3600,
    )
    # Prediction whose position never closed within the horizon -> skipped.
    await _prediction(
        db_session, model_id=model_id, wallet_id=3, token_id=30, prob=0.6, created=created
    )
    await _closed_position(
        db_session,
        wallet_id=3,
        token_id=30,
        opened=created - timedelta(hours=1),
        closed=created + timedelta(hours=48),  # beyond 24h horizon
        pnl="2.0",
        roi="0.5",
        hold=100,
    )
    await db_session.flush()

    count = await resolve_outcomes(
        db_session, label_horizon_hours=24, batch=100, now=NOW
    )
    assert count == 2

    win = (
        await db_session.execute(
            select(PredictionOutcome).where(
                PredictionOutcome.prediction_id == pid_win
            )
        )
    ).scalar_one()
    assert win.actual_label == 1
    assert float(win.actual_roi) == pytest.approx(0.4)
    assert win.actual_hold_seconds == 7200
    # brier = (0.8 - 1)^2 = 0.04
    assert float(win.brier) == pytest.approx(0.04)
    assert win.roi_error is None  # predicted_roi absent -> None

    lose = (
        await db_session.execute(
            select(PredictionOutcome).where(
                PredictionOutcome.prediction_id == pid_lose
            )
        )
    ).scalar_one()
    assert lose.actual_label == 0
    # brier = (0.3 - 0)^2 = 0.09
    assert float(lose.brier) == pytest.approx(0.09)

    # Rerun is idempotent: unique prediction_id, no duplicates.
    count2 = await resolve_outcomes(
        db_session, label_horizon_hours=24, batch=100, now=NOW
    )
    assert count2 == 0
    total = (
        await db_session.execute(select(func.count()).select_from(PredictionOutcome))
    ).scalar_one()
    assert total == 2


@pytest.mark.asyncio
async def test_compute_model_performance_signal_and_gate(db_session):
    np.random.seed(7)
    good_id = await _model(db_session, "good_model", 1)
    sparse_id = await _model(db_session, "sparse_model", 1)

    created = NOW - timedelta(hours=5)
    n = 60
    # Planted signal: probability correlates with label.
    for i in range(n):
        label = 1 if i % 2 == 0 else 0
        prob = 0.5 + 0.35 * (label - 0.5) * 2 + np.random.normal(0, 0.05)
        prob = float(min(0.99, max(0.01, prob)))
        wallet_id = 100 + i
        token_id = 1000 + i
        await _prediction(
            db_session,
            model_id=good_id,
            wallet_id=wallet_id,
            token_id=token_id,
            prob=prob,
            created=created,
        )
        pnl = "1.0" if label == 1 else "-1.0"
        await _closed_position(
            db_session,
            wallet_id=wallet_id,
            token_id=token_id,
            opened=created - timedelta(hours=1),
            closed=created + timedelta(hours=1),
            pnl=pnl,
            roi="0.5" if label == 1 else "-0.5",
            hold=3600,
        )

    # Sparse model: only a few resolved -> gated out.
    for i in range(3):
        wallet_id = 500 + i
        token_id = 5000 + i
        await _prediction(
            db_session,
            model_id=sparse_id,
            wallet_id=wallet_id,
            token_id=token_id,
            prob=0.5,
            created=created,
        )
        await _closed_position(
            db_session,
            wallet_id=wallet_id,
            token_id=token_id,
            opened=created - timedelta(hours=1),
            closed=created + timedelta(hours=1),
            pnl="1.0",
            roi="0.5",
            hold=3600,
        )
    await db_session.flush()

    resolved = await resolve_outcomes(
        db_session, label_horizon_hours=24, batch=1000, now=NOW
    )
    assert resolved == n + 3

    perfs = await compute_model_performance(
        db_session, window_days=30, min_resolved=20, bins=10, now=NOW
    )
    assert len(perfs) == 1
    perf = perfs[0]
    assert perf.model_id == good_id
    assert perf.resolved_count == n
    assert perf.auc is not None and float(perf.auc) > 0.5
    assert float(perf.base_rate) == pytest.approx(0.5)
    # Calibration bin membership sums to resolved_count.
    assert sum(b["n"] for b in perf.calibration) == n
    assert len(perf.calibration) == 10

    # Persisted exactly one ModelPerformance row (good model only).
    stored = (
        await db_session.execute(select(ModelPerformance))
    ).scalars().all()
    assert len(stored) == 1
    assert stored[0].model_name == "good_model"


@pytest.mark.asyncio
async def test_prediction_matches_own_episode_not_prior_reentry(db_session):
    """Re-entry matching mirrors the TRAINING labeling rule.

    dataset.build_trade_profit_dataset labels a buy with the wallet's
    earliest close after it within the horizon — including an overlapping
    prior episode that closes soon after. The model learned that label, so
    evaluation must attribute the same episode or AUC/Brier are computed
    against answers the model was never taught.
    """
    model_id = await _model(db_session, "trade_profit", 1)
    # Old episode: opened 10h before the prediction, closed 1h after it (a big
    # win). New episode: opened at the prediction time, closes 30h later
    # (beyond the 24h horizon) — a loss.
    prediction_at = NOW - timedelta(hours=5)
    await _closed_position(
        db_session, wallet_id=1, token_id=1,
        opened=prediction_at - timedelta(hours=10),
        closed=prediction_at + timedelta(hours=1),
        pnl="5", roi="2.0", hold=39600,
    )
    await _closed_position(
        db_session, wallet_id=1, token_id=1,
        opened=prediction_at,
        closed=prediction_at + timedelta(hours=30),
        pnl="-0.5", roi="-0.4", hold=108000,
    )
    await _prediction(
        db_session, model_id=model_id, wallet_id=1, token_id=1,
        prob=0.8, created=prediction_at,
    )
    await db_session.commit()

    resolved = await resolve_outcomes(
        db_session, label_horizon_hours=24, batch=1000, now=NOW
    )
    # Earliest close after the prediction within the horizon = the prior
    # episode's +5 win — exactly what the training label for this buy would
    # have been. One outcome, attributed to that episode.
    assert resolved == 1
    outcome = (
        await db_session.execute(select(PredictionOutcome))
    ).scalar_one()
    assert outcome.actual_label == 1  # the winning close, per training rule
