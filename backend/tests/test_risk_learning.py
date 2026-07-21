"""Tests for the risk-learning loop (outcome labeling + weight tuning)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.risk_learning import resolve_outcomes, tune_weights
from app.db.models import RiskWeightSnapshot, Token, TokenRiskAssessment, TokenSnapshot
from app.decision.risk_contracts import (
    BASE_WEIGHTS,
    RISK_WEIGHTS_KEY,
    WEIGHT_MAX_FACTOR,
    WEIGHT_MIN_FACTOR,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def make_settings(**overrides) -> SimpleNamespace:
    defaults = dict(
        rug_outcome_min_age_hours=12,
        rug_learning_enabled=True,
        rug_learning_min_samples=50,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def add_token(session: AsyncSession, mint: str) -> Token:
    token = Token(mint=mint, first_seen_at=NOW - timedelta(days=7))
    session.add(token)
    await session.flush()
    return token


def make_assessment(
    token: Token,
    ts: datetime,
    components: dict | None = None,
    outcome: str | None = None,
) -> TokenRiskAssessment:
    return TokenRiskAssessment(
        token_id=token.id,
        mint=token.mint,
        ts=ts,
        score=Decimal("50"),
        hard_blocked=False,
        components=components,
        engine_version="1",
        weights_version=0,
        outcome=outcome,
    )


async def add_snapshot(
    session: AsyncSession, token: Token, ts: datetime, price: str | None, liq: str | None
) -> None:
    session.add(
        TokenSnapshot(
            token_id=token.id,
            ts=ts,
            price_sol=Decimal(price) if price is not None else None,
            liquidity_sol=Decimal(liq) if liq is not None else None,
        )
    )
    # Flush per row: SQLite's insertmanyvalues sentinel matching chokes on
    # batched composite-PK datetime keys.
    await session.flush()


async def test_liquidity_collapse_labels_rug(db_session: AsyncSession) -> None:
    token = await add_token(db_session, "MintRug")
    ts = NOW - timedelta(hours=24)
    assessment = make_assessment(token, ts)
    db_session.add(assessment)
    await add_snapshot(db_session, token, ts - timedelta(minutes=5), "0.001", "100")
    await add_snapshot(db_session, token, NOW - timedelta(hours=1), "0.0009", "5")
    await db_session.flush()

    assert await resolve_outcomes(db_session, make_settings(), now=NOW) == 1
    assert assessment.outcome == "rug"
    assert assessment.outcome_resolved_at == NOW


async def test_profit_and_loss_roi_math(db_session: AsyncSession) -> None:
    winner = await add_token(db_session, "MintWin")
    loser = await add_token(db_session, "MintLose")
    ts = NOW - timedelta(hours=24)
    a_win = make_assessment(winner, ts)
    a_lose = make_assessment(loser, ts)
    db_session.add_all([a_win, a_lose])
    await add_snapshot(db_session, winner, ts - timedelta(minutes=5), "0.002", "100")
    await add_snapshot(db_session, winner, NOW - timedelta(hours=1), "0.003", "120")
    await add_snapshot(db_session, loser, ts - timedelta(minutes=5), "0.002", "100")
    await add_snapshot(db_session, loser, NOW - timedelta(hours=1), "0.0015", "90")
    await db_session.flush()

    assert await resolve_outcomes(db_session, make_settings(), now=NOW) == 2
    assert a_win.outcome == "profit"
    assert a_win.outcome_roi == Decimal("0.5")
    assert a_lose.outcome == "loss"
    assert a_lose.outcome_roi == Decimal("-0.25")


async def test_no_snapshots_unknown_only_after_72h(db_session: AsyncSession) -> None:
    token = await add_token(db_session, "MintBare")
    young = make_assessment(token, NOW - timedelta(hours=24))
    old = make_assessment(token, NOW - timedelta(hours=80))
    db_session.add_all([young, old])
    await db_session.flush()

    assert await resolve_outcomes(db_session, make_settings(), now=NOW) == 1
    assert old.outcome == "unknown"
    assert young.outcome is None  # left for a later cycle


async def test_fresh_assessments_not_touched(db_session: AsyncSession) -> None:
    token = await add_token(db_session, "MintFresh")
    fresh = make_assessment(token, NOW - timedelta(hours=2))
    db_session.add(fresh)
    await db_session.flush()

    assert await resolve_outcomes(db_session, make_settings(), now=NOW) == 0
    assert fresh.outcome is None


def components_for(score_map: dict[str, float]) -> dict:
    return {name: {"score": score, "w": 0.1} for name, score in score_map.items()}


async def seed_labeled(
    session: AsyncSession,
    rug_scores: dict[str, float],
    good_scores: dict[str, float],
    n_rugs: int = 25,
    n_good: int = 35,
) -> None:
    token = await add_token(session, "MintSeed")
    ts = NOW - timedelta(days=2)
    for _ in range(n_rugs):
        session.add(make_assessment(token, ts, components_for(rug_scores), outcome="rug"))
    for i in range(n_good):
        outcome = "profit" if i % 2 else "loss"
        session.add(make_assessment(token, ts, components_for(good_scores), outcome=outcome))
    await session.flush()


FLAT = {name: 50.0 for name in BASE_WEIGHTS}


async def test_tune_disabled_returns_none(db_session: AsyncSession, stub_redis) -> None:
    settings = make_settings(rug_learning_enabled=False)
    assert await tune_weights(db_session, stub_redis, settings, now=NOW) is None


async def test_tune_min_sample_guard(db_session: AsyncSession, stub_redis) -> None:
    await seed_labeled(db_session, FLAT, FLAT, n_rugs=5, n_good=10)
    assert await tune_weights(db_session, stub_redis, make_settings(), now=NOW) is None
    assert stub_redis.data == {}


async def test_weight_direction_and_renormalization(db_session: AsyncSession, stub_redis) -> None:
    rug = dict(FLAT, authority=90.0, lp_security=30.0)
    good = dict(FLAT, authority=40.0, lp_security=60.0)
    await seed_labeled(db_session, rug, good)

    weights = await tune_weights(db_session, stub_redis, make_settings(), now=NOW)
    assert weights is not None
    assert weights["authority"] > BASE_WEIGHTS["authority"]
    assert weights["lp_security"] < BASE_WEIGHTS["lp_security"]
    assert sum(weights.values()) == pytest.approx(1.0)

    payload = json.loads(stub_redis.data[RISK_WEIGHTS_KEY])
    assert payload["version"] == 1
    assert payload["weights"] == pytest.approx(weights)

    snap = (await db_session.execute(select(RiskWeightSnapshot))).scalar_one()
    assert snap.version == 1
    assert snap.sample_count == 60
    assert snap.notes["rug_count"] == 25


async def test_factor_clamped_at_bounds(db_session: AsyncSession, stub_redis) -> None:
    rug = dict(FLAT, authority=100.0, lp_security=0.0)
    good = dict(FLAT, authority=0.0, lp_security=100.0)
    await seed_labeled(db_session, rug, good)

    weights = await tune_weights(db_session, stub_redis, make_settings(), now=NOW)
    assert weights is not None
    snap = (await db_session.execute(select(RiskWeightSnapshot))).scalar_one()
    means = snap.notes["component_means"]
    assert means["authority"]["factor"] == WEIGHT_MAX_FACTOR
    assert means["lp_security"]["factor"] == WEIGHT_MIN_FACTOR


async def test_no_churn_below_threshold(db_session: AsyncSession, stub_redis) -> None:
    # Identical means for every component -> factors all 1.0 -> weights equal
    # BASE_WEIGHTS -> below the 0.01 churn threshold vs the active set.
    await seed_labeled(db_session, FLAT, FLAT)
    assert await tune_weights(db_session, stub_redis, make_settings(), now=NOW) is None
    assert stub_redis.data == {}
    assert (await db_session.execute(select(RiskWeightSnapshot))).scalars().all() == []


async def test_version_increments(db_session: AsyncSession, stub_redis) -> None:
    db_session.add(
        RiskWeightSnapshot(
            ts=NOW - timedelta(days=1),
            version=3,
            weights=dict(BASE_WEIGHTS),
            sample_count=50,
        )
    )
    rug = dict(FLAT, authority=90.0)
    good = dict(FLAT, authority=40.0)
    await seed_labeled(db_session, rug, good)

    weights = await tune_weights(db_session, stub_redis, make_settings(), now=NOW)
    assert weights is not None
    assert json.loads(stub_redis.data[RISK_WEIGHTS_KEY])["version"] == 4
