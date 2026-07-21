"""The trade evaluator: copy-or-skip with transparent scoring.

Every evaluation persists a TradeDecision row — including skips — with two
audit trails: ``factors`` (how the confidence/risk scores were composed) and
``reasons`` (every filter and safety gate with its outcome). The decision
policy itself is data for Phase 4.

Scoring:
- confidence 0-100: adaptive wallet confidence (static score x EW ranking
  adjustment) blended with the ML probability when a model is active and
  a token-quality component from recent snapshots.
- risk 0-100: liquidity depth, token age, recent volatility, wallet
  consistency — each mapped to [0, 100] and weighted.
- expected reward/drawdown come from the leader's historical avg ROI and
  max drawdown, scaled by the ranking adjustment.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.ml import predict
from app.analytics.ml.dataset import WALLET_FEATURE_COLUMNS
from app.config import Settings
from app.db.models import Token, TokenSnapshot, TradeDecision, Wallet, WalletStats
from app.db.util import aware, to_decimal, to_float
from app.decision import ranking, sizing
from app.decision.risk_contracts import RiskVerdict
from app.decision.safety import SafetyGuard
from app.logging_config import get_logger

log = get_logger(__name__)

# Confidence blend weights; ML weight redistributes to the wallet component
# when no model is active (weights always sum to 1).
W_WALLET = 0.45
W_ML = 0.25
W_TOKEN = 0.20
W_MARKET = 0.10


@dataclass
class LeaderBuy:
    """Normalized leader-buy event (from the events:trades channel)."""

    signature: str
    wallet_address: str
    token_mint: str
    quote_amount_sol: Decimal
    block_time: datetime


@dataclass
class Evaluation:
    decision: str  # copy | skip
    confidence: float
    risk: float
    size_sol: Decimal
    expected_reward: float | None
    expected_drawdown: float | None
    p_profit: float | None
    reasons: list[dict] = field(default_factory=list)
    factors: list[dict] = field(default_factory=list)
    decision_id: int | None = None
    token_id: int | None = None
    leader_wallet_id: int | None = None


def _scale(value: float, lo: float, hi: float) -> float:
    """Map value to [0, 1] over [lo, hi], clamped."""
    if hi <= lo:
        return 0.0
    return min(max((value - lo) / (hi - lo), 0.0), 1.0)


async def _latest_snapshots(
    session: AsyncSession, token_id: int, limit: int = 2
) -> list[TokenSnapshot]:
    return list(
        (
            await session.execute(
                select(TokenSnapshot)
                .where(TokenSnapshot.token_id == token_id)
                .order_by(TokenSnapshot.ts.desc())
                .limit(limit)
            )
        ).scalars()
    )


RiskAssessor = Callable[[AsyncSession, Token], Awaitable[RiskVerdict]]


class Evaluator:
    def __init__(
        self,
        settings: Settings,
        redis: Any,
        guard: SafetyGuard,
        risk_assessor: RiskAssessor | None = None,
    ):
        self._settings = settings
        self._redis = redis
        self._guard = guard
        # Pre-copy structural rug assessment (wired by the copytrader worker,
        # which owns the RPC client). None = evaluation-only context.
        self._risk_assessor = risk_assessor

    async def evaluate_buy(self, session: AsyncSession, event: LeaderBuy) -> Evaluation:
        settings = self._settings
        reasons: list[dict] = []
        factors: list[dict] = []

        wallet = (
            await session.execute(
                select(Wallet).where(Wallet.address == event.wallet_address)
            )
        ).scalar_one_or_none()
        token = (
            await session.execute(select(Token).where(Token.mint == event.token_mint))
        ).scalar_one_or_none()
        if wallet is None or token is None:
            return await self._persist(
                session,
                event,
                Evaluation(
                    decision="skip",
                    confidence=0.0,
                    risk=100.0,
                    size_sol=Decimal(0),
                    expected_reward=None,
                    expected_drawdown=None,
                    p_profit=None,
                    reasons=[{"gate": "known_entities", "passed": False,
                              "note": "wallet or token not in database yet"}],
                ),
                wallet,
                token,
            )

        stats = (
            await session.execute(
                select(WalletStats).where(WalletStats.wallet_id == wallet.id)
            )
        ).scalar_one_or_none()
        snapshots = await _latest_snapshots(session, token.id)
        latest = snapshots[0] if snapshots else None
        previous = snapshots[1] if len(snapshots) > 1 else None
        now = datetime.now(tz=UTC)
        token_age_seconds = (now - aware(token.first_seen_at)).total_seconds()

        # --- confidence composition ---------------------------------------
        base_conf = to_float(stats.confidence_score) if stats else None
        adjust, adjust_note = await ranking.adjustment_factor(self._redis, wallet.id)
        # A genuine 0.0 confidence must stay 0, not fall back to the prior
        # (that is what `is None` guards, unlike truthiness).
        prior_conf = base_conf if base_conf is not None else 30.0
        wallet_conf = min(prior_conf * adjust, 100.0)
        factors.append(
            {
                "factor": "wallet_confidence",
                "value": round(wallet_conf, 2),
                "note": f"static {base_conf if base_conf is not None else 'n/a (prior 30)'}"
                f", adaptive {adjust_note}",
            }
        )

        ml_payload = None
        if stats is not None:
            ml_payload = await predict.predict_trade(
                session,
                settings,
                {
                    "wallet_stats": {
                        column: getattr(stats, column)
                        for column in WALLET_FEATURE_COLUMNS
                    },
                    "token_age_seconds": token_age_seconds,
                    "buy_size_sol": float(event.quote_amount_sol),
                    "hour_of_day": event.block_time.hour + event.block_time.minute / 60.0,
                    "dex": token.primary_dex or "unknown",
                    "signature": event.signature,
                    "wallet_id": wallet.id,
                    "token_id": token.id,
                },
            )
        p_profit = ml_payload["p_profit"] if ml_payload else None
        factors.append(
            {
                "factor": "ml_p_profit",
                "value": p_profit,
                "note": "active trade_profit model" if ml_payload else
                        "no active model; weight folded into wallet confidence",
            }
        )

        liquidity = to_float(latest.liquidity_sol) if latest else None
        volume_1h = to_float(latest.volume_sol_1h) if latest else None
        holder_growth = None
        if (
            latest
            and previous
            and latest.holder_count is not None
            and previous.holder_count is not None
        ):
            holder_growth = latest.holder_count - previous.holder_count
        token_quality = (
            0.4 * _scale(liquidity or 0.0, 0.0, 500.0)
            + 0.3 * _scale(volume_1h or 0.0, 0.0, 1000.0)
            + 0.3 * _scale(float(holder_growth or 0), 0.0, 50.0)
        )
        factors.append(
            {
                "factor": "token_quality",
                "value": round(token_quality * 100, 2),
                "note": f"liquidity={liquidity} SOL, volume_1h={volume_1h} SOL, "
                f"holder_growth={holder_growth}",
            }
        )

        market_component = 0.5  # neutral until Phase 4 regimes land
        factors.append(
            {"factor": "market", "value": 50.0,
             "note": "neutral placeholder until Phase 4 market regimes"}
        )

        ml_weight = W_ML if p_profit is not None else 0.0
        wallet_weight = W_WALLET + (W_ML - ml_weight)
        confidence = (
            wallet_weight * wallet_conf
            + ml_weight * (p_profit or 0.0) * 100.0
            + W_TOKEN * token_quality * 100.0
            + W_MARKET * market_component * 100.0
        )

        # --- risk composition ---------------------------------------------
        age_seconds = (now - aware(token.first_seen_at)).total_seconds()
        volatility = None
        if latest and latest.vwap_sol_5m and latest.price_sol:
            vwap, price = to_float(latest.vwap_sol_5m), to_float(latest.price_sol)
            if vwap and price and vwap > 0:
                volatility = abs(price - vwap) / vwap
        risk_parts = {
            # thin liquidity is the dominant memecoin risk
            "liquidity": (1.0 - _scale(liquidity or 0.0, 0.0, 200.0)) * 40.0,
            "token_age": (1.0 - _scale(age_seconds, 0.0, 86_400.0)) * 25.0,
            "volatility": _scale(volatility if volatility is not None else 0.15, 0.0, 0.5) * 20.0,
            "wallet_consistency": _scale(
                to_float(stats.roi_std) if stats and stats.roi_std is not None else 1.0,
                0.0, 3.0,
            ) * 15.0,
        }
        risk = sum(risk_parts.values())
        factors.append(
            {"factor": "risk_components",
             "value": round(risk, 2),
             "note": {k: round(v, 2) for k, v in risk_parts.items()}}
        )

        expected_reward = None
        expected_drawdown = None
        if stats is not None:
            avg_roi = to_float(stats.avg_roi)
            # Only DISCOUNT expected reward by a demotion; never let a demotion
            # (adjust<1) make a losing leader's negative expected ROI look less
            # bad. Positive edge is scaled by the adjustment; negatives pass
            # through unscaled.
            if avg_roi is None:
                expected_reward = None
            elif avg_roi >= 0:
                expected_reward = avg_roi * adjust
            else:
                expected_reward = avg_roi
            expected_drawdown = to_float(stats.max_drawdown_pct)

        # --- gates ----------------------------------------------------------
        def gate(name: str, passed: bool, note: str) -> bool:
            reasons.append({"gate": name, "passed": passed, "note": note})
            return passed

        _, exposure = await self._guard.open_exposure(session)
        size, size_notes = sizing.copy_size_sol(
            settings,
            leader_size_sol=event.quote_amount_sol,
            current_exposure_sol=exposure,
        )

        followed = wallet.is_tracked or (
            settings.copy_auto_follow
            and wallet_conf >= settings.copy_min_wallet_confidence
        )
        safety_blocks = await self._guard.gate_reasons(session, event.token_mint)

        # --- pre-copy rug-risk assessment ---------------------------------
        rug_verdict: RiskVerdict | None = None
        if settings.rug_check_enabled and self._risk_assessor is not None:
            rug_verdict = await self._risk_assessor(session, token)
            factors.append(rug_verdict.as_factor())
        if not settings.rug_check_enabled:
            rug_note = "rug check disabled by config"
        elif rug_verdict is None:
            rug_note = "no assessor in this context; gate inactive"
        elif rug_verdict.hard_blocked:
            rug_note = "HARD BLOCK: " + "; ".join(rug_verdict.blocked_reasons)
        else:
            rug_note = f"score {rug_verdict.score:.1f} vs max {settings.rug_max_score}"

        checks = [
            gate("copy_enabled", settings.copy_enabled, "master switch"),
            gate(
                "followed_leader",
                followed,
                "manually tracked" if wallet.is_tracked else
                f"auto-follow at adjusted confidence {wallet_conf:.1f} "
                f"(bar {settings.copy_min_wallet_confidence})",
            ),
            gate(
                "wallet_not_blacklisted",
                event.wallet_address not in settings.copy_wallet_blacklist,
                "wallet blacklist",
            ),
            gate(
                "token_not_blacklisted",
                event.token_mint not in settings.copy_token_blacklist,
                "token blacklist",
            ),
            gate(
                "token_rug_risk",
                rug_verdict is None
                or (
                    not rug_verdict.hard_blocked
                    and rug_verdict.score <= settings.rug_max_score
                ),
                rug_note,
            ),
            gate(
                "confidence_threshold",
                confidence >= settings.copy_min_confidence,
                f"{confidence:.1f} vs min {settings.copy_min_confidence}",
            ),
            gate(
                "risk_threshold",
                risk <= settings.copy_max_risk,
                f"{risk:.1f} vs max {settings.copy_max_risk}",
            ),
            gate(
                "liquidity_floor",
                liquidity is not None and liquidity >= settings.copy_min_liquidity_sol,
                f"{liquidity} SOL vs min {settings.copy_min_liquidity_sol}",
            ),
            gate(
                "market_cap_band",
                latest is not None
                and latest.market_cap_usd is not None
                and settings.copy_min_market_cap_usd
                <= float(latest.market_cap_usd)
                <= settings.copy_max_market_cap_usd,
                f"mcap {to_float(latest.market_cap_usd) if latest else None} USD",
            ),
            gate("position_size", size > 0, "; ".join(size_notes)),
            gate("safety_rails", not safety_blocks, "; ".join(safety_blocks) or "clear"),
            gate(
                "no_duplicate_position",
                await self._no_open_position(session, token.id),
                "one open copy position per token",
            ),
        ]

        evaluation = Evaluation(
            decision="copy" if all(checks) else "skip",
            confidence=round(confidence, 2),
            risk=round(risk, 2),
            size_sol=size,
            expected_reward=expected_reward,
            expected_drawdown=expected_drawdown,
            p_profit=p_profit,
            reasons=reasons,
            factors=factors,
        )
        return await self._persist(session, event, evaluation, wallet, token)

    async def _no_open_position(self, session: AsyncSession, token_id: int) -> bool:
        from app.db.models import CopyPosition

        row = (
            await session.execute(
                select(CopyPosition.id).where(
                    CopyPosition.token_id == token_id, CopyPosition.status == "open"
                )
            )
        ).first()
        return row is None

    async def _persist(
        self,
        session: AsyncSession,
        event: LeaderBuy,
        evaluation: Evaluation,
        wallet: Wallet | None,
        token: Token | None,
    ) -> Evaluation:
        row = TradeDecision(
            created_at=datetime.now(tz=UTC),
            source_signature=event.signature,
            leader_wallet_id=wallet.id if wallet else None,
            token_id=token.id if token else None,
            side="buy",
            mode=self._settings.copy_mode,
            confidence_score=to_decimal(evaluation.confidence),
            risk_score=to_decimal(evaluation.risk),
            expected_reward=to_decimal(evaluation.expected_reward),
            expected_drawdown=to_decimal(evaluation.expected_drawdown),
            p_profit=to_decimal(evaluation.p_profit),
            decision=evaluation.decision,
            size_sol=evaluation.size_sol,
            reasons=evaluation.reasons,
            factors=evaluation.factors,
        )
        session.add(row)
        await session.flush()
        evaluation.decision_id = row.id
        evaluation.token_id = token.id if token else None
        evaluation.leader_wallet_id = wallet.id if wallet else None
        log.info(
            "trade_evaluated",
            decision=evaluation.decision,
            confidence=evaluation.confidence,
            risk=evaluation.risk,
            leader=event.wallet_address,
            token=event.token_mint,
        )
        return evaluation
