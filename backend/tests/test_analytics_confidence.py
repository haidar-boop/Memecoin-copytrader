"""Tests for the pure confidence scoring engine."""

from __future__ import annotations

from decimal import Decimal

from app.analytics.confidence import PRIOR_SCORE, score_wallet


def base_stats(**overrides) -> dict:
    stats = {
        "closed_position_count": 0,
        "win_count": 0,
        "profit_factor": None,
        "roi_std": None,
        "pnl_30d_sol": None,
    }
    stats.update(overrides)
    return stats


def test_no_evidence_lands_at_prior() -> None:
    score, components = score_wallet(base_stats())
    assert abs(float(score) - PRIOR_SCORE) < 1.0
    names = {entry["component"] for entry in components}
    assert {"win_rate", "profitability", "consistency", "recency", "experience"} <= names


def test_strong_wallet_scores_high() -> None:
    score, _ = score_wallet(
        base_stats(
            closed_position_count=80,
            win_count=52,
            profit_factor=Decimal("2.5"),
            roi_std=Decimal("0.4"),
            pnl_30d_sol=Decimal("25"),
        )
    )
    assert float(score) > 60


def test_weak_wallet_scores_low() -> None:
    score, _ = score_wallet(
        base_stats(
            closed_position_count=60,
            win_count=10,
            profit_factor=Decimal("0.3"),
            roi_std=Decimal("2.5"),
            pnl_30d_sol=Decimal("-15"),
        )
    )
    assert float(score) < PRIOR_SCORE


def test_small_sample_cannot_score_extreme() -> None:
    lucky_score, _ = score_wallet(
        base_stats(closed_position_count=3, win_count=3, profit_factor=None)
    )
    seasoned_score, _ = score_wallet(
        base_stats(
            closed_position_count=100,
            win_count=70,
            profit_factor=Decimal("3"),
            roi_std=Decimal("0.5"),
            pnl_30d_sol=Decimal("30"),
        )
    )
    # Three straight wins must score well below a proven long track record.
    assert float(lucky_score) < float(seasoned_score) - 15


def test_explanation_contributions_are_complete() -> None:
    score, components = score_wallet(
        base_stats(closed_position_count=40, win_count=20, profit_factor=Decimal("1.5"))
    )
    weighted = [c for c in components if c["weight"] is not None]
    assert all(c["contribution"] is not None for c in weighted)
    assert 0 <= float(score) <= 100
    shrink = next(c for c in components if c["component"] == "sample_shrinkage")
    assert 0 < shrink["value"] <= 1


def test_score_bounds_hold_for_garbage_inputs() -> None:
    score, _ = score_wallet(
        base_stats(
            closed_position_count=10**9,
            win_count=10**9,
            profit_factor=Decimal("1e30"),
            roi_std=Decimal("0"),
            pnl_30d_sol=Decimal("1e20"),
        )
    )
    assert 0 <= float(score) <= 100
