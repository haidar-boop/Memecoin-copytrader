"""Tests for the ML training/prediction pipeline (app.analytics.ml)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.ml import predict_trade, retrain_once
from app.analytics.ml.registry import load_active
from app.config import Settings
from app.db.models import (
    MlModel,
    Position,
    Prediction,
    Token,
    Trade,
    Wallet,
    WalletStatsSnapshot,
)
from app.ingestion.programs import WSOL_MINT

BASE = datetime(2026, 1, 1, tzinfo=UTC)
N_WALLETS = 40
TRADES_PER_WALLET = 20  # 800 rows total


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        ml_model_dir=str(tmp_path / "models"),
        ml_min_training_rows=overrides.pop("ml_min_training_rows", 500),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


async def _seed_trades(session: AsyncSession) -> None:
    """~800 WSOL buys where high-win-rate wallets win 80%, low ones 20%.

    The wallet signal is planted in ``wallet_stats_snapshots`` PREDATING the
    trades: the dataset builder is point-in-time and deliberately ignores the
    live ``wallet_stats`` table (look-ahead leakage), so seeding the current
    table would leave the features signal-free.
    """
    rng = random.Random(42)
    np.random.seed(42)

    snapshot_rows: list[dict] = []
    token_counter = 0
    for w in range(N_WALLETS):
        good = w % 2 == 0
        wallet = Wallet(address=f"wallet{w}", first_seen_at=BASE, last_seen_at=BASE)
        session.add(wallet)
        await session.flush()
        snapshot_rows.append(
            {
                "wallet_id": wallet.id,
                "ts": BASE - timedelta(hours=1),
                "closed_position_count": 30,
                "win_count": 24 if good else 6,
                "win_rate": Decimal("0.8") if good else Decimal("0.2"),
                "profit_factor": Decimal("3.0") if good else Decimal("0.4"),
                "avg_hold_seconds": 1800,
                "roi_std": Decimal("0.3") if good else Decimal("1.2"),
                "confidence_score": Decimal("75") if good else Decimal("20"),
            }
        )
        for t in range(TRADES_PER_WALLET):
            token_counter += 1
            block_time = BASE + timedelta(minutes=10 * (w * TRADES_PER_WALLET + t))
            token = Token(mint=f"mint{token_counter}", first_seen_at=block_time - timedelta(hours=1))
            session.add(token)
            await session.flush()
            profitable = rng.random() < (0.8 if good else 0.2)
            session.add(
                Trade(
                    signature=f"sig{token_counter}",
                    event_index=0,
                    block_time=block_time,
                    slot=token_counter,
                    wallet_id=wallet.id,
                    token_id=token.id,
                    dex="pumpfun",
                    side="buy",
                    token_amount=Decimal(1000),
                    quote_amount=Decimal("0.5"),
                    quote_mint=WSOL_MINT,
                )
            )
            session.add(
                Position(
                    wallet_id=wallet.id,
                    token_id=token.id,
                    status="closed",
                    opened_at=block_time,
                    closed_at=block_time + timedelta(hours=2),
                    realized_pnl_sol=Decimal("0.3") if profitable else Decimal("-0.2"),
                )
            )
    await session.execute(insert(WalletStatsSnapshot), snapshot_rows)
    await session.commit()


async def test_retrain_trains_and_registers(db_session: AsyncSession, tmp_path: Path) -> None:
    await _seed_trades(db_session)
    settings = _settings(tmp_path)

    report = await retrain_once(db_session, settings, now=BASE + timedelta(days=30))

    trade_report = report["trade_profit"]
    assert "skipped" not in trade_report
    assert trade_report["metrics"]["roc_auc"] > 0.65
    assert 0.0 <= trade_report["metrics"]["brier"] <= 1.0
    assert 0.0 < trade_report["metrics"]["base_rate"] < 1.0
    assert trade_report["training_rows"] == N_WALLETS * TRADES_PER_WALLET
    assert trade_report["is_active"] is True

    row = (
        await db_session.execute(
            select(MlModel).where(MlModel.name == "trade_profit", MlModel.is_active.is_(True))
        )
    ).scalar_one()
    assert row.version == 1
    assert Path(row.artifact_path).exists()
    assert Path(row.artifact_path).parent == Path(settings.ml_model_dir)

    # wallet_persistence has no snapshot pairs -> graceful skip.
    assert "skipped" in report["wallet_persistence"]


async def test_second_retrain_bumps_version(db_session: AsyncSession, tmp_path: Path) -> None:
    await _seed_trades(db_session)
    settings = _settings(tmp_path)
    await retrain_once(db_session, settings, now=BASE + timedelta(days=30))
    report2 = await retrain_once(db_session, settings, now=BASE + timedelta(days=31))

    assert report2["trade_profit"]["version"] == 2
    rows = (
        (
            await db_session.execute(
                select(MlModel).where(MlModel.name == "trade_profit").order_by(MlModel.version)
            )
        )
        .scalars()
        .all()
    )
    assert [r.version for r in rows] == [1, 2]
    # Same data, same AUC: v2 does not beat v1, so v1 stays active.
    assert [r.is_active for r in rows] == [True, False]
    assert Path(rows[1].artifact_path).exists()


async def test_predict_trade_scores_and_records(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await _seed_trades(db_session)
    settings = _settings(tmp_path)
    await retrain_once(db_session, settings, now=BASE + timedelta(days=30))

    payload = await predict_trade(
        db_session,
        settings,
        {
            "wallet_stats": {
                "win_rate": 0.8,
                "profit_factor": 3.0,
                "closed_position_count": 30,
                "avg_hold_seconds": 1800,
                "roi_std": 0.3,
                "confidence_score": 75,
            },
            "token_age_seconds": 3600.0,
            "buy_size_sol": 0.5,
            "hour_of_day": 14.0,
            "dex": "pumpfun",
            "signature": "predsig",
            "wallet_id": 1,
            "token_id": 1,
        },
    )
    assert payload is not None
    assert 0.0 <= payload["p_profit"] <= 1.0
    assert payload["model_name"] == "trade_profit"

    prediction = (
        await db_session.execute(select(Prediction).where(Prediction.signature == "predsig"))
    ).scalar_one()
    assert prediction.subject_type == "trade"
    assert prediction.predicted["p_profit"] == payload["p_profit"]
    assert prediction.context["wallet_win_rate"] == 0.8

    # A good wallet should score higher than a bad one.
    bad = await predict_trade(
        db_session,
        settings,
        {
            "wallet_stats": {
                "win_rate": 0.2,
                "profit_factor": 0.4,
                "closed_position_count": 30,
                "avg_hold_seconds": 1800,
                "roi_std": 1.2,
                "confidence_score": 20,
            },
            "token_age_seconds": 3600.0,
            "buy_size_sol": 0.5,
            "hour_of_day": 14.0,
            "dex": "pumpfun",
        },
    )
    assert bad is not None
    assert payload["p_profit"] > bad["p_profit"]


async def test_predict_trade_none_without_model(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    result = await predict_trade(
        db_session,
        settings,
        {"wallet_stats": None, "token_age_seconds": 0, "buy_size_sol": 0.1, "hour_of_day": 0, "dex": "pumpfun"},
    )
    assert result is None
    count = (await db_session.execute(select(Prediction))).scalars().all()
    assert count == []


async def test_retrain_skips_below_min_rows(db_session: AsyncSession, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = await retrain_once(db_session, settings)
    assert "skipped" in report["trade_profit"]
    assert "skipped" in report["wallet_persistence"]
    assert (
        await db_session.execute(select(MlModel))
    ).scalars().all() == []


async def test_wallet_persistence_trains_on_snapshot_pairs(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    np.random.seed(7)
    rng = random.Random(7)
    rows: list[dict] = []
    for w in range(300):
        good = w % 2 == 0
        early_ts = BASE + timedelta(hours=w)
        later_positive = rng.random() < (0.85 if good else 0.15)
        rows.append(
            {
                "wallet_id": w + 1,
                "ts": early_ts,
                "closed_position_count": 25,
                "win_rate": Decimal("0.75") if good else Decimal("0.25"),
                "profit_factor": Decimal("2.5") if good else Decimal("0.5"),
                "pnl_30d_sol": Decimal("5") if good else Decimal("-3"),
                "confidence_score": Decimal("70") if good else Decimal("25"),
            }
        )
        rows.append(
            {
                "wallet_id": w + 1,
                "ts": early_ts + timedelta(days=26),
                "closed_position_count": 30,
                "pnl_30d_sol": Decimal("4") if later_positive else Decimal("-2"),
            }
        )
    await db_session.execute(insert(WalletStatsSnapshot), rows)
    await db_session.commit()

    settings = _settings(tmp_path, ml_min_training_rows=200)
    report = await retrain_once(db_session, settings, now=BASE + timedelta(days=60))

    wp = report["wallet_persistence"]
    assert "skipped" not in wp
    assert wp["metrics"]["roc_auc"] > 0.65
    loaded = await load_active(db_session, "wallet_persistence")
    assert loaded is not None
    row, estimator, names = loaded
    assert row.version == 1
    assert len(names) == 11
    assert hasattr(estimator, "predict_proba")
