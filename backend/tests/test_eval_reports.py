"""Tests for the AI report generator (app.evaluation.reports)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import select

from app.db.models import (
    MarketRegime,
    ModelPerformance,
    Prediction,
    PredictionOutcome,
    Report,
    StrategyStat,
    Wallet,
    WalletStats,
)
from app.evaluation.reports import generate_report

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

SECTION_KEYS = {
    "best_wallets",
    "worst_wallets",
    "best_strategies",
    "declining_strategies",
    "highest_risk_wallets",
    "most_consistent_wallets",
    "prediction_accuracy",
    "biggest_mistakes",
    "biggest_improvements",
    "market_summary",
}


async def _seed(session) -> None:
    np.random.seed(7)

    # Wallets with a spread of confidence, roi_std and closed counts.
    # (confidence, closed_positions, win_rate, roi_std, max_drawdown_pct, avg_roi)
    specs = [
        (95.0, 40, 0.80, 0.10, 0.15, 1.20),  # best, consistent
        (82.0, 30, 0.70, 0.25, 0.30, 0.90),
        (60.0, 25, 0.55, 0.90, 0.70, 0.20),  # risky
        (40.0, 20, 0.45, 1.50, 0.85, -0.10),  # riskiest, worst-ish
        (30.0, 12, 0.40, 0.60, 0.50, -0.20),  # worst confidence
        (70.0, 2, 0.90, 0.05, 0.05, 2.00),  # too few closed positions
    ]
    for i, (conf, closed, wr, rstd, mdd, avg_roi) in enumerate(specs, start=1):
        session.add(Wallet(
            id=i, address=f"addr{i}", first_seen_at=NOW, last_seen_at=NOW
        ))
        session.add(WalletStats(
            wallet_id=i,
            computed_at=NOW,
            closed_position_count=closed,
            win_rate=Decimal(str(wr)),
            roi_std=Decimal(str(rstd)),
            max_drawdown_pct=Decimal(str(mdd)),
            avg_roi=Decimal(str(avg_roi)),
            confidence_score=Decimal(str(conf)),
        ))

    # StrategyStat across two windows per style: sniper improving, swing declining.
    prior = NOW - timedelta(days=10)
    for style, (roi0, roi1, wr0, wr1) in {
        "sniper": (0.20, 0.60, 0.50, 0.65),
        "swing": (0.80, 0.30, 0.70, 0.55),
    }.items():
        session.add(StrategyStat(
            ts=prior, style=style, window_days=30, wallet_count=10,
            closed_positions=50, win_rate=Decimal(str(wr0)), avg_roi=Decimal(str(roi0)),
            total_pnl_sol=Decimal("5"), profit_factor=Decimal("1.5"),
        ))
        session.add(StrategyStat(
            ts=NOW, style=style, window_days=30, wallet_count=12,
            closed_positions=60, win_rate=Decimal(str(wr1)), avg_roi=Decimal(str(roi1)),
            total_pnl_sol=Decimal("6"), profit_factor=Decimal("1.6"),
        ))

    # ModelPerformance: two rows per model showing improvement.
    for model_id, name, (auc0, auc1) in [
        (1, "trade_profit", (0.62, 0.71)),
        (2, "wallet_quality", (0.55, 0.58)),
    ]:
        session.add(ModelPerformance(
            ts=prior, model_id=model_id, model_name=name, window_days=30,
            resolved_count=100, auc=Decimal(str(auc0)), brier=Decimal("0.22"),
            accuracy=Decimal("0.60"), base_rate=Decimal("0.50"),
        ))
        session.add(ModelPerformance(
            ts=NOW, model_id=model_id, model_name=name, window_days=30,
            resolved_count=120, auc=Decimal(str(auc1)), brier=Decimal("0.18"),
            accuracy=Decimal("0.66"), base_rate=Decimal("0.50"),
        ))

    # Predictions + resolved outcomes, including a high-prob wrong one.
    session.add(Prediction(
        id=1, model_id=1, created_at=NOW - timedelta(days=1), subject_type="trade",
        predicted={"p_profit": 0.95},
    ))
    session.add(Prediction(
        id=2, model_id=1, created_at=NOW - timedelta(days=1), subject_type="trade",
        predicted={"p_profit": 0.40},
    ))
    session.add(PredictionOutcome(
        prediction_id=1, model_id=1, resolved_at=NOW - timedelta(hours=2),
        subject_type="trade", predicted_prob=Decimal("0.950000"),
        actual_label=0, actual_roi=Decimal("-0.8"), brier=Decimal("0.90250000"),
    ))
    session.add(PredictionOutcome(
        prediction_id=2, model_id=1, resolved_at=NOW - timedelta(hours=2),
        subject_type="trade", predicted_prob=Decimal("0.400000"),
        actual_label=0, actual_roi=Decimal("-0.1"), brier=Decimal("0.16000000"),
    ))

    session.add(MarketRegime(
        ts=NOW - timedelta(minutes=5), window_minutes=60, regime="bull",
        high_volatility=True, whale_accumulation=True,
        description="Strong uptrend with whale accumulation.",
    ))
    # An older regime to make sure the latest one wins.
    session.add(MarketRegime(
        ts=NOW - timedelta(hours=6), window_minutes=60, regime="bear",
        panic_selling=True, description="Old regime.",
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_generate_report_full(db_session) -> None:
    await _seed(db_session)
    top_n = 3
    report = await generate_report(
        db_session, kind="weekly", window_days=7, top_n=top_n, now=NOW
    )

    # Report row inserted and returned with an id.
    assert report.id is not None
    stored = (
        await db_session.execute(select(Report).where(Report.id == report.id))
    ).scalar_one()
    assert stored.kind == "weekly"

    sections = report.sections
    assert set(sections) == SECTION_KEYS
    for section in sections.values():
        assert set(section) == {"conclusion", "evidence", "items"}
        assert section["conclusion"]

    # best_wallets: ordered by confidence desc, length <= top_n.
    best = sections["best_wallets"]["items"]
    assert len(best) <= top_n
    confs = [it["confidence_score"] for it in best]
    assert confs == sorted(confs, reverse=True)
    assert best[0]["wallet_id"] == 1  # 95.0
    assert best[0]["closed_position_count"] == 40
    assert best[0]["win_rate"] == pytest.approx(0.80)

    # worst_wallets excludes the wallet with too few closed positions (id 6).
    worst_ids = {it["wallet_id"] for it in sections["worst_wallets"]["items"]}
    assert 6 not in worst_ids
    assert 5 in worst_ids  # lowest confidence among eligible

    # strategies: sniper rising, swing declining.
    best_styles = [it["style"] for it in sections["best_strategies"]["items"]]
    decl_styles = [it["style"] for it in sections["declining_strategies"]["items"]]
    assert "sniper" in best_styles
    assert "swing" in decl_styles

    # highest risk is the riskiest wallet (id 4, roi_std 1.5).
    assert sections["highest_risk_wallets"]["items"][0]["wallet_id"] == 4
    # most consistent is the low-roi_std wallet among eligible (id 1, 0.10).
    assert sections["most_consistent_wallets"]["items"][0]["wallet_id"] == 1

    # prediction_accuracy present for both models.
    acc_models = {it["model_id"] for it in sections["prediction_accuracy"]["items"]}
    assert acc_models == {1, 2}

    # biggest_mistakes surfaces the high-prob wrong prediction first.
    mistakes = sections["biggest_mistakes"]["items"]
    assert mistakes[0]["prediction_id"] == 1
    assert mistakes[0]["predicted_prob"] == pytest.approx(0.95)
    assert mistakes[0]["actual_label"] == 0

    # biggest_improvements: both models improved AUC; leader is model 1 (+0.09).
    improvements = sections["biggest_improvements"]["items"]
    assert improvements[0]["model_id"] == 1
    assert improvements[0]["delta_auc"] == pytest.approx(0.09)

    # market_summary reflects the latest (bull) regime and its flags.
    market = sections["market_summary"]["items"][0]
    assert market["regime"] == "bull"
    assert "whale_accumulation" in market["active_flags"]
    assert "high_volatility" in market["active_flags"]

    # markdown non-empty and contains a number.
    assert report.markdown
    assert re.search(r"\d", report.markdown)
    assert "# Weekly Report" in report.markdown
    assert report.summary


@pytest.mark.asyncio
async def test_generate_report_empty_is_graceful(db_session) -> None:
    report = await generate_report(
        db_session, kind="daily", window_days=1, top_n=5, now=NOW
    )
    assert report.id is not None
    assert set(report.sections) == SECTION_KEYS
    for section in report.sections.values():
        # Every section present with an insufficient-data note and no items.
        assert section["items"] == []
        assert "insufficient data" in section["conclusion"]
    assert report.markdown
    assert report.summary


@pytest.mark.asyncio
async def test_generate_report_deterministic(db_session) -> None:
    await _seed(db_session)
    r1 = await generate_report(
        db_session, kind="weekly", window_days=7, top_n=3, now=NOW
    )
    r2 = await generate_report(
        db_session, kind="weekly", window_days=7, top_n=3, now=NOW
    )
    assert r1.sections == r2.sections
    assert r1.markdown == r2.markdown
