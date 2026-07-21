"""Learning loop that closes rug-risk predictions against observed reality.

Two responsibilities, both invoked by the analytics runner:

- :func:`resolve_outcomes` labels aged :class:`TokenRiskAssessment` rows as
  ``rug`` / ``profit`` / ``loss`` / ``unknown`` by comparing the market
  snapshot nearest before the assessment with the latest snapshot, so
  predictions become supervised examples.
- :func:`tune_weights` rescales the *soft* component weights in
  ``BASE_WEIGHTS`` toward components that discriminate rugs from non-rugs,
  bounded by ``WEIGHT_MIN_FACTOR``/``WEIGHT_MAX_FACTOR`` and renormalized.

HARD-FILTER INVARIANT: this module never reads or writes any ``rug_block_*``
setting and has no code path that can alter hard-filter behavior. Learning
only rescales soft component weights within contract bounds; structural red
flags (active mint/freeze authority, extreme holder concentration) always
block regardless of what history says.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import fmean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RiskWeightSnapshot, TokenRiskAssessment, TokenSnapshot
from app.decision.risk_contracts import (
    BASE_WEIGHTS,
    RISK_WEIGHTS_KEY,
    WEIGHT_MAX_FACTOR,
    WEIGHT_MIN_FACTOR,
)
from app.logging_config import get_logger

logger = get_logger(__name__)

# Beyond this age with no usable snapshot evidence, an assessment can never be
# labeled meaningfully — mark it "unknown" so it stops being re-scanned.
_UNKNOWN_AFTER = timedelta(hours=72)

# Relative drop from baseline that counts as a rug (liquidity or price).
_RUG_DROP = Decimal("0.90")

_BATCH_SIZE = 500

# Minimum rug-labeled examples before means are trustworthy at all.
_MIN_RUGS = 10

# Weight sets closer than this (max abs per-component delta) to the active
# set are not persisted — avoids version churn from statistical noise.
_CHURN_THRESHOLD = 0.01


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """Treat naive timestamps (SQLite round-trips) as UTC for arithmetic."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _dropped(baseline: Decimal | None, latest: Decimal | None) -> bool:
    """True when ``latest`` fell at least 90% below a positive baseline."""
    if baseline is None or latest is None or baseline <= 0:
        return False
    return (baseline - latest) / baseline >= _RUG_DROP


async def resolve_outcomes(
    session: AsyncSession,
    settings: Any,
    now: datetime | None = None,
) -> int:
    """Label unresolved assessments old enough for their fate to be visible.

    Returns the number of rows labeled. Does not commit — the caller owns
    the transaction so a runner can batch this with other analytics writes.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(hours=settings.rug_outcome_min_age_hours)
    rows = (
        await session.execute(
            select(TokenRiskAssessment)
            .where(TokenRiskAssessment.outcome.is_(None))
            .where(TokenRiskAssessment.ts < cutoff)
            .order_by(TokenRiskAssessment.ts)
            .limit(_BATCH_SIZE)
        )
    ).scalars().all()

    labeled = 0
    for assessment in rows:
        outcome, roi = await _resolve_one(session, assessment, now)
        if outcome is None:
            continue
        assessment.outcome = outcome
        assessment.outcome_roi = roi
        assessment.outcome_resolved_at = now
        labeled += 1

    if labeled:
        logger.info("risk_outcomes_resolved", count=labeled, scanned=len(rows))
    return labeled


async def _resolve_one(
    session: AsyncSession,
    assessment: TokenRiskAssessment,
    now: datetime,
) -> tuple[str | None, Decimal | None]:
    """Decide one assessment's outcome, or (None, None) to retry next cycle."""
    expired = now - _aware(assessment.ts) >= _UNKNOWN_AFTER

    baseline = (
        await session.execute(
            select(TokenSnapshot)
            .where(TokenSnapshot.token_id == assessment.token_id)
            .where(TokenSnapshot.ts <= assessment.ts)
            .order_by(TokenSnapshot.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest = (
        await session.execute(
            select(TokenSnapshot)
            .where(TokenSnapshot.token_id == assessment.token_id)
            .order_by(TokenSnapshot.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if baseline is None or latest is None:
        return ("unknown", None) if expired else (None, None)

    if _dropped(baseline.liquidity_sol, latest.liquidity_sol) or _dropped(
        baseline.price_sol, latest.price_sol
    ):
        return "rug", None

    if baseline.price_sol is None or baseline.price_sol <= 0 or latest.price_sol is None:
        return ("unknown", None) if expired else (None, None)

    roi = (latest.price_sol - baseline.price_sol) / baseline.price_sol
    return ("profit" if roi > 0 else "loss"), roi


def _component_score(components: dict | None, name: str) -> float | None:
    entry = (components or {}).get(name)
    if isinstance(entry, dict) and isinstance(entry.get("score"), (int, float)):
        return float(entry["score"])
    return None


async def _active_weights(redis: Any, session: AsyncSession) -> dict[str, float]:
    """Current effective weights: Redis mirror, else newest snapshot, else base."""
    raw = await redis.get(RISK_WEIGHTS_KEY)
    if raw:
        try:
            payload = json.loads(raw)
            weights = payload.get("weights")
            if isinstance(weights, dict):
                return {k: float(v) for k, v in weights.items()}
        except (ValueError, TypeError):
            logger.warning("risk_weights_redis_unparseable")
    snap = (
        await session.execute(
            select(RiskWeightSnapshot).order_by(RiskWeightSnapshot.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if snap is not None:
        return {k: float(v) for k, v in snap.weights.items()}
    return dict(BASE_WEIGHTS)


async def tune_weights(
    session: AsyncSession,
    redis: Any,
    settings: Any,
    now: datetime | None = None,
) -> dict[str, float] | None:
    """Re-derive soft component weights from labeled outcomes.

    Returns the new weight mapping when a snapshot was written, else None
    (learning disabled, too few samples, or change below churn threshold).
    Does not commit — the caller owns the transaction.
    """
    if not settings.rug_learning_enabled:
        return None
    now = now or _utcnow()

    rows = (
        await session.execute(
            select(TokenRiskAssessment.outcome, TokenRiskAssessment.components).where(
                TokenRiskAssessment.outcome.in_(["rug", "profit", "loss"])
            )
        )
    ).all()
    rug_rows = [components for outcome, components in rows if outcome == "rug"]
    good_rows = [components for outcome, components in rows if outcome != "rug"]
    total = len(rows)
    if total < settings.rug_learning_min_samples or len(rug_rows) < _MIN_RUGS:
        return None

    means: dict[str, dict[str, float]] = {}
    scaled: dict[str, float] = {}
    for name, base in BASE_WEIGHTS.items():
        bad_scores = [s for c in rug_rows if (s := _component_score(c, name)) is not None]
        good_scores = [s for c in good_rows if (s := _component_score(c, name)) is not None]
        mean_bad = fmean(bad_scores) if bad_scores else 0.0
        mean_good = fmean(good_scores) if good_scores else 0.0
        # A component that scores rugs 50 points hotter than non-rugs earned
        # 1.5x weight; one that scores non-rugs hotter loses weight.
        factor = min(max(1.0 + (mean_bad - mean_good) / 100.0, WEIGHT_MIN_FACTOR), WEIGHT_MAX_FACTOR)
        means[name] = {"mean_rug": mean_bad, "mean_non_rug": mean_good, "factor": factor}
        scaled[name] = base * factor

    scale_total = sum(scaled.values())
    new_weights = {name: value / scale_total for name, value in scaled.items()}

    current = await _active_weights(redis, session)
    max_delta = max(
        abs(new_weights[name] - current.get(name, BASE_WEIGHTS[name])) for name in new_weights
    )
    if max_delta < _CHURN_THRESHOLD:
        return None

    latest_version = (
        await session.execute(select(func.max(RiskWeightSnapshot.version)))
    ).scalar_one()
    version = (latest_version or 0) + 1
    session.add(
        RiskWeightSnapshot(
            ts=now,
            version=version,
            weights=new_weights,
            sample_count=total,
            notes={
                "rug_count": len(rug_rows),
                "non_rug_count": len(good_rows),
                "component_means": means,
            },
        )
    )
    await redis.set(RISK_WEIGHTS_KEY, json.dumps({"version": version, "weights": new_weights}))
    logger.info(
        "risk_weights_tuned",
        version=version,
        sample_count=total,
        rug_count=len(rug_rows),
        max_delta=round(max_delta, 4),
    )
    return new_weights
