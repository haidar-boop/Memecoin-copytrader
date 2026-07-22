"""Token rug-risk engine: hard filters plus weighted 0-100 soft scoring.

The engine sits between the on-chain security probe and the copy-trade
evaluator. It combines probe facts (:class:`TokenSecuritySignals`) with
database-derived evidence (deployer track record, trade concentration,
liquidity/age) into a single 0-100 risk score plus hard-filter verdicts.

Two properties drive the design:

- Hard filters are structural: an active mint authority can print supply at
  will regardless of any weighted evidence, so they block independently of
  the learned weights and never inflate the weighted score — ``hard_blocked``
  is its own flag so the score stays an honest weighted composite.
- Every assessment is persisted append-only so the learning loop can later
  label outcomes and tune the *soft* weights within the contract bounds.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Token, TokenRiskAssessment, TokenSnapshot, Trade
from app.decision.risk_contracts import (
    BASE_WEIGHTS,
    ENGINE_VERSION,
    RISK_WEIGHTS_KEY,
    WEIGHT_MAX_FACTOR,
    WEIGHT_MIN_FACTOR,
    RiskComponent,
    RiskVerdict,
    TokenSecuritySignals,
)
from app.logging_config import get_logger

logger = get_logger(__name__)

# Component tuning constants, pinned by tests. Kept module-level (not config)
# because changing them changes scoring semantics and must bump ENGINE_VERSION.
_UNKNOWN_SCORE = 50.0
_AUTHORITY_UNKNOWN = 60.0
_AUTHORITY_FREEZE_ONLY = 80.0
_HOLDER_SCALE_LO = 0.20
_HOLDER_SCALE_HI = 0.70
_LP_BONDING_CURVE = 40.0
_LP_BURN_SAFE = 0.90
_LP_TOP_HOLDER_RISKY = 0.5
_DEPLOYER_NO_HISTORY = 30.0
_DEPLOYER_HISTORY_CAP = 20
_DEAD_LIQUIDITY_SOL = 1.0
_DEAD_PRICE_DROP = 0.90
_VOLUME_WINDOW = timedelta(hours=6)
_VOLUME_MIN_BUYS = 10
_VOLUME_CONCENTRATION_GAIN = 140.0
_LIQ_FULL_SOL = 200.0
_AGE_FULL_SECONDS = 86_400.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _scale(x: float, lo: float, hi: float) -> float:
    """Linear position of ``x`` in [lo, hi], clamped to [0, 1]."""
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _as_utc(ts: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; stored values are always UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class TokenRiskEngine:
    """Config-driven hard filters plus learnable weighted soft scoring."""

    def __init__(self, settings: Settings, redis: Any) -> None:
        self._settings = settings
        self._redis = redis

    async def assess(
        self,
        session: AsyncSession,
        token: Token,
        probe: Callable[[], Awaitable[TokenSecuritySignals]],
    ) -> RiskVerdict:
        """Assess ``token``, reusing a fresh stored assessment when possible.

        ``probe`` is a thunk so the (expensive, RPC-consuming) on-chain
        lookup only runs when no reusable assessment exists.
        """
        if not self._settings.rug_check_enabled:
            return RiskVerdict(
                mint=token.mint,
                score=0.0,
                hard_blocked=False,
                components=[RiskComponent("rug_check", 0.0, 0.0, "disabled")],
            )

        reused = await self._load_recent(session, token)
        if reused is not None:
            return reused

        signals = await probe()
        weights, weights_version = await self._load_weights()
        components = await self._score_components(session, token, signals)
        blocked_reasons = self._hard_filters(signals)

        score = _clamp(
            sum(c.score * weights[c.name] for c in components)
        )
        for c in components:
            c.weight = weights[c.name]

        row = TokenRiskAssessment(
            token_id=token.id,
            mint=token.mint,
            ts=datetime.now(UTC),
            # asyncpg rejects bare floats for NUMERIC binds; str() first so
            # the Decimal is exact-at-display rather than binary-noise.
            score=Decimal(str(round(score, 6))),
            hard_blocked=bool(blocked_reasons),
            blocked_reasons=blocked_reasons,
            components={
                c.name: {"score": c.score, "weight": c.weight, "note": c.note}
                for c in components
            },
            signals=asdict(signals),
            engine_version=ENGINE_VERSION,
            weights_version=weights_version,
        )
        session.add(row)
        await session.flush()

        logger.info(
            "token_risk_assessed",
            mint=token.mint,
            score=round(score, 2),
            hard_blocked=bool(blocked_reasons),
            blocked_reasons=blocked_reasons,
            weights_version=weights_version,
        )
        return RiskVerdict(
            mint=token.mint,
            score=score,
            hard_blocked=bool(blocked_reasons),
            blocked_reasons=blocked_reasons,
            components=components,
            weights_version=weights_version,
            assessment_id=row.id,
        )

    # -- TTL reuse ---------------------------------------------------------

    async def _load_recent(
        self, session: AsyncSession, token: Token
    ) -> RiskVerdict | None:
        """Rebuild a verdict from the latest stored assessment if still fresh.

        Freshness requires both the TTL and the same ENGINE_VERSION: a
        deployed scoring change must not be masked by stale rows.
        """
        row = (
            await session.execute(
                select(TokenRiskAssessment)
                .where(
                    TokenRiskAssessment.token_id == token.id,
                    TokenRiskAssessment.engine_version == ENGINE_VERSION,
                )
                .order_by(TokenRiskAssessment.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self._settings.rug_assessment_ttl_seconds
        )
        if _as_utc(row.ts) < cutoff:
            return None
        components = [
            RiskComponent(
                name=name,
                score=float(payload.get("score", 0.0)),
                weight=float(payload.get("weight", 0.0)),
                note=str(payload.get("note", "")),
            )
            for name, payload in (row.components or {}).items()
        ]
        return RiskVerdict(
            mint=row.mint,
            score=float(row.score),
            hard_blocked=row.hard_blocked,
            blocked_reasons=list(row.blocked_reasons or []),
            components=components,
            weights_version=row.weights_version,
            assessment_id=row.id,
        )

    # -- hard filters ------------------------------------------------------

    def _hard_filters(self, signals: TokenSecuritySignals) -> list[str]:
        """Structural red flags, config-driven and weight-independent."""
        s = self._settings
        reasons: list[str] = []
        # A pump.fun bonding-curve token's mint authority is the curve PDA
        # itself, held until migration purely so the program can mint the
        # last chunk on graduation to Raydium — it cannot be misused by the
        # deployer the way an EOA-held mint authority can. Hard-blocking it
        # would reject essentially every pre-migration pump.fun token, i.e.
        # the entire population leaders trade. It still counts against the
        # soft authority score below, just not as a structural veto.
        if (
            s.rug_block_mint_authority
            and signals.mint_authority
            and not signals.is_bonding_curve
        ):
            reasons.append("mint_authority_active")
        if s.rug_block_freeze_authority and signals.freeze_authority:
            reasons.append("freeze_authority_active")
        if (
            signals.top10_holder_pct is not None
            and signals.top10_holder_pct > s.rug_block_top10_pct
        ):
            reasons.append("holder_concentration_extreme")
        if s.rug_fail_closed and (
            signals.mint_authority is None or signals.freeze_authority is None
        ):
            reasons.append("authority_unknown_fail_closed")
        return reasons

    # -- weights -----------------------------------------------------------

    async def _load_weights(self) -> tuple[dict[str, float], int]:
        """Learned weights from Redis, defensively clamped and renormalized.

        The tuner already clamps to contract bounds, but Redis is writable
        by ops, so the engine re-clamps (belt and braces) — a fat-fingered
        weight can never dominate or vanish a component.
        """
        raw = None
        try:
            raw = await self._redis.get(RISK_WEIGHTS_KEY)
        except Exception:
            logger.warning("risk_weights_redis_error", exc_info=True)
        if raw is None:
            return dict(BASE_WEIGHTS), 0
        try:
            payload = json.loads(raw)
            version = int(payload["version"])
            learned = payload["weights"]
            clamped = {
                name: _clamp(
                    float(learned.get(name, base)),
                    base * WEIGHT_MIN_FACTOR,
                    base * WEIGHT_MAX_FACTOR,
                )
                for name, base in BASE_WEIGHTS.items()
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning("risk_weights_invalid_payload")
            return dict(BASE_WEIGHTS), 0
        total = sum(clamped.values())
        if total <= 0:
            return dict(BASE_WEIGHTS), 0
        return {name: w / total for name, w in clamped.items()}, version

    # -- soft components ---------------------------------------------------

    async def _score_components(
        self,
        session: AsyncSession,
        token: Token,
        signals: TokenSecuritySignals,
    ) -> list[RiskComponent]:
        now = datetime.now(UTC)
        return [
            self._score_authority(signals),
            self._score_holder_concentration(signals),
            self._score_lp_security(signals),
            await self._score_deployer_history(session, token),
            await self._score_volume_authenticity(session, token, now),
            await self._score_liquidity_age(session, token, now),
        ]

    def _score_authority(self, signals: TokenSecuritySignals) -> RiskComponent:
        """Renounced both = 0; active mint = 100; freeze only = 80; unknown = 60.

        Active mint dominates (supply inflation), unknown sits below any
        confirmed flag because it may resolve safe.
        """
        if signals.mint_authority:
            score, note = 100.0, "mint authority active"
        elif signals.freeze_authority:
            score, note = _AUTHORITY_FREEZE_ONLY, "freeze authority active"
        elif signals.mint_authority is None or signals.freeze_authority is None:
            score, note = _AUTHORITY_UNKNOWN, "authority unknown"
        else:
            score, note = 0.0, "both authorities renounced"
        return RiskComponent("authority", score, 0.0, note)

    def _score_holder_concentration(
        self, signals: TokenSecuritySignals
    ) -> RiskComponent:
        """Linear scale of top-10 supply share: 0.20 -> 0 up to 0.70 -> 100."""
        pct = signals.top10_holder_pct
        if pct is None:
            return RiskComponent(
                "holder_concentration", _UNKNOWN_SCORE, 0.0, "holders unknown"
            )
        score = 100.0 * _scale(pct, _HOLDER_SCALE_LO, _HOLDER_SCALE_HI)
        return RiskComponent(
            "holder_concentration", score, 0.0, f"top10 hold {pct:.0%}"
        )

    def _score_lp_security(self, signals: TokenSecuritySignals) -> RiskComponent:
        """LP exit-ability mapping, pinned by tests:

        - bonding curve -> 40 (normal for pump.fun, but the curve is
          inherently exit-able by the deployer pre-migration);
        - LP exists and burn fraction unknown -> 50 (unknown);
        - top LP holder > 0.5 while burn < 0.5 -> 90 (one wallet can pull);
        - burn >= 0.90 -> 0 (effectively unruggable);
        - otherwise linear in unburned share: 100 * (1 - burned);
        - no LP at all -> 50 (unknown/unassessable).
        """
        if signals.is_bonding_curve:
            return RiskComponent("lp_security", _LP_BONDING_CURVE, 0.0, "bonding curve")
        if not signals.lp_exists or signals.lp_burned_pct is None:
            return RiskComponent("lp_security", _UNKNOWN_SCORE, 0.0, "lp status unknown")
        burned = signals.lp_burned_pct
        if burned >= _LP_BURN_SAFE:
            return RiskComponent("lp_security", 0.0, 0.0, f"lp burned {burned:.0%}")
        top = signals.lp_top_holder_pct
        if top is not None and top > _LP_TOP_HOLDER_RISKY and burned < 0.5:
            return RiskComponent(
                "lp_security", 90.0, 0.0, f"top lp holder {top:.0%}, burn {burned:.0%}"
            )
        return RiskComponent(
            "lp_security", 100.0 * (1.0 - burned), 0.0, f"lp burned {burned:.0%}"
        )

    async def _score_deployer_history(
        self, session: AsyncSession, token: Token
    ) -> RiskComponent:
        """Fraction of the creator's prior tokens that died.

        Dead = latest snapshot liquidity <= 1 SOL, or price down >= 90% from
        that token's own max snapshot price. Unknown creator or no priors is
        a mild 30: serial ruggers usually leave a trail, a blank slate is
        only weak evidence of safety.
        """
        if token.creator is None:
            return RiskComponent(
                "deployer_history", _DEPLOYER_NO_HISTORY, 0.0, "creator unknown"
            )
        prior = (
            (
                await session.execute(
                    select(Token)
                    .where(Token.creator == token.creator, Token.id != token.id)
                    .order_by(Token.first_seen_at.desc())
                    .limit(_DEPLOYER_HISTORY_CAP)
                )
            )
            .scalars()
            .all()
        )
        if not prior:
            return RiskComponent(
                "deployer_history", _DEPLOYER_NO_HISTORY, 0.0, "no prior tokens"
            )
        dead = 0
        for other in prior:
            if await self._token_is_dead(session, other.id):
                dead += 1
        score = 100.0 * dead / len(prior)
        return RiskComponent(
            "deployer_history", score, 0.0, f"{dead}/{len(prior)} prior tokens dead"
        )

    async def _token_is_dead(self, session: AsyncSession, token_id: int) -> bool:
        latest = (
            await session.execute(
                select(TokenSnapshot)
                .where(TokenSnapshot.token_id == token_id)
                .order_by(TokenSnapshot.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            return False
        if latest.liquidity_sol is not None and float(latest.liquidity_sol) <= _DEAD_LIQUIDITY_SOL:
            return True
        if latest.price_sol is not None:
            max_price = (
                await session.execute(
                    select(func.max(TokenSnapshot.price_sol)).where(
                        TokenSnapshot.token_id == token_id
                    )
                )
            ).scalar_one()
            if max_price is not None and float(max_price) > 0:
                drop = 1.0 - float(latest.price_sol) / float(max_price)
                if drop >= _DEAD_PRICE_DROP:
                    return True
        return False

    async def _score_volume_authenticity(
        self, session: AsyncSession, token: Token, now: datetime
    ) -> RiskComponent:
        """Buy-side wallet concentration over the last 6h.

        With unique buyers ``u`` over ``n`` buys, concentration = 1 - u/n;
        score = clamp(concentration * 140, 0, 100). The 140 gain means e.g.
        5 wallets doing 50 buys (concentration 0.9) pins to 100, while
        organic flow (u close to n) stays near 0. Fewer than 10 buys is
        insufficient evidence -> 50.
        """
        since = now - _VOLUME_WINDOW
        n, u = (
            await session.execute(
                select(
                    func.count(), func.count(func.distinct(Trade.wallet_id))
                ).where(
                    Trade.token_id == token.id,
                    Trade.side == "buy",
                    Trade.block_time >= since,
                )
            )
        ).one()
        if n < _VOLUME_MIN_BUYS:
            return RiskComponent(
                "volume_authenticity", _UNKNOWN_SCORE, 0.0, f"only {n} buys in 6h"
            )
        concentration = 1.0 - u / n
        score = _clamp(concentration * _VOLUME_CONCENTRATION_GAIN)
        return RiskComponent(
            "volume_authenticity", score, 0.0, f"{u} wallets / {n} buys"
        )

    async def _score_liquidity_age(
        self, session: AsyncSession, token: Token, now: datetime
    ) -> RiskComponent:
        """Thin liquidity x extreme youth.

        score = 100 * (1 - scale(L, 0, 200 SOL)) * (1 - scale(A, 0, 24h)):
        high only when the pool is BOTH shallow and brand-new; either deep
        liquidity or an established age drives it toward 0. No snapshot
        means liquidity 0 (worst case).
        """
        latest = (
            await session.execute(
                select(TokenSnapshot)
                .where(TokenSnapshot.token_id == token.id)
                .order_by(TokenSnapshot.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        liquidity = (
            float(latest.liquidity_sol)
            if latest is not None and latest.liquidity_sol is not None
            else 0.0
        )
        age_seconds = max(0.0, (now - _as_utc(token.first_seen_at)).total_seconds())
        score = (
            100.0
            * (1.0 - _scale(liquidity, 0.0, _LIQ_FULL_SOL))
            * (1.0 - _scale(age_seconds, 0.0, _AGE_FULL_SECONDS))
        )
        return RiskComponent(
            "liquidity_age",
            score,
            0.0,
            f"{liquidity:.1f} SOL liquidity, {age_seconds / 3600:.1f}h old",
        )
