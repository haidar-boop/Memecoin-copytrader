"""Online scoring with the active trade_profit model.

Every prediction is also INSERTed into ``predictions`` (append-only) with its
full feature context, so Phase 4 can compare predicted probabilities against
realized outcomes model-version by model-version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.ml import features, registry
from app.config import Settings
from app.db.models import Prediction
from app.logging_config import get_logger

log = get_logger(__name__)


async def predict_trade(
    session: AsyncSession, settings: Settings, trade_features: dict[str, Any]
) -> dict[str, Any] | None:
    """Score one candidate buy with the active trade_profit model.

    ``trade_features`` keys: ``wallet_stats`` (WalletStats column dict or
    None), ``token_age_seconds``, ``buy_size_sol``, ``hour_of_day``, ``dex``,
    plus optional ``signature`` / ``wallet_id`` / ``token_id`` identifiers.
    Returns None when no active model exists.

    Transaction contract: the Prediction row is flushed but NOT committed —
    the caller owns the unit of work (a decision path must be able to roll
    everything back atomically).
    """
    loaded = await registry.load_active(session, "trade_profit")
    if loaded is None:
        log.info("predict_trade_no_active_model")
        return None
    model_row, estimator, feature_names = loaded

    names, vector = features.trade_features(
        wallet_stats=trade_features.get("wallet_stats"),
        token_age_seconds=float(trade_features.get("token_age_seconds") or 0.0),
        buy_size_sol=float(trade_features.get("buy_size_sol") or 0.0),
        hour_of_day=float(trade_features.get("hour_of_day") or 0.0),
        dex=str(trade_features.get("dex") or "unknown"),
    )
    if feature_names and feature_names != names:
        log.warning(
            "predict_trade_feature_mismatch",
            model_id=model_row.id,
            expected=feature_names,
            got=names,
        )
        return None

    probability = float(
        estimator.predict_proba(np.asarray([vector], dtype=np.float64))[0, 1]
    )
    probability = min(max(probability, 0.0), 1.0)

    now = datetime.now(tz=UTC)
    predicted = {"p_profit": probability}
    context = {name: value for name, value in zip(names, vector, strict=True)}
    session.add(
        Prediction(
            model_id=model_row.id,
            created_at=now,
            subject_type="trade",
            signature=trade_features.get("signature"),
            wallet_id=trade_features.get("wallet_id"),
            token_id=trade_features.get("token_id"),
            predicted=predicted,
            context=context,
        )
    )
    await session.flush()

    return {
        "model_id": model_row.id,
        "model_name": model_row.name,
        "model_version": model_row.version,
        "p_profit": probability,
        "created_at": now.isoformat(),
    }
