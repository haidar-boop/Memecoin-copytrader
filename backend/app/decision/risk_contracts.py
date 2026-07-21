"""Shared contracts for the token rug-risk engine.

Three modules collaborate through these types:

- ``app.enrichment.token_security`` (the on-chain probe) produces
  :class:`TokenSecuritySignals` from RPC lookups.
- ``app.decision.token_risk`` (the scoring engine) combines those signals
  with DB-derived signals into a 0-100 risk score plus hard-filter verdicts.
- ``app.analytics.risk_learning`` (the learning loop) labels assessment
  outcomes and tunes the *soft* component weights within bounds.

Hard filters are structural red flags and are never subject to learned
weighting: an active mint authority can print supply at will regardless of
how past tokens performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bump when scoring semantics change so stored assessments stay comparable.
ENGINE_VERSION = "1"

# Redis key holding the current learned weight mapping (JSON) and its version.
RISK_WEIGHTS_KEY = "risk:weights"

# Soft component names -> base weights (must sum to 1.0). The learning loop
# may scale each within [WEIGHT_MIN_FACTOR, WEIGHT_MAX_FACTOR] of its base
# before renormalizing; it may never add or remove components.
BASE_WEIGHTS: dict[str, float] = {
    "authority": 0.20,        # mint/freeze authority still active (sub-block levels)
    "holder_concentration": 0.20,
    "lp_security": 0.15,      # LP burned/locked vs held by few wallets
    "deployer_history": 0.15, # creator's prior tokens' fates
    "volume_authenticity": 0.15,
    "liquidity_age": 0.15,    # thin liquidity x extreme youth
}
WEIGHT_MIN_FACTOR = 0.5
WEIGHT_MAX_FACTOR = 2.0


@dataclass
class TokenSecuritySignals:
    """On-chain facts about a token, fetched by the security probe.

    ``None`` always means "could not determine", never "safe". The engine
    treats unknowns pessimistically but distinguishes them from confirmed
    red flags in its explanations.
    """

    mint: str
    # Authorities: None = unknown; "" = confirmed renounced; else the pubkey.
    mint_authority: str | None = None
    freeze_authority: str | None = None
    # Fraction of circulating supply held by the top 10 non-pool accounts,
    # in [0, 1]. None = unknown.
    top10_holder_pct: float | None = None
    holder_sample_count: int | None = None
    # LP status for the token's primary pool, if any.
    lp_exists: bool = False
    lp_burned_pct: float | None = None      # fraction of LP supply burned
    lp_top_holder_pct: float | None = None  # largest single LP holder share
    # Bonding-curve tokens (pump.fun pre-migration) have no external LP.
    is_bonding_curve: bool = False
    probe_errors: list[str] = field(default_factory=list)


@dataclass
class RiskComponent:
    name: str
    score: float          # 0 (safe) - 100 (maximum risk)
    weight: float         # effective (possibly learned) weight applied
    note: str


@dataclass
class RiskVerdict:
    """The engine's output for one token at one moment."""

    mint: str
    score: float                     # 0-100 weighted composite
    hard_blocked: bool
    blocked_reasons: list[str] = field(default_factory=list)
    components: list[RiskComponent] = field(default_factory=list)
    weights_version: int = 0
    assessment_id: int | None = None

    def as_factor(self) -> dict:
        """Compact JSON representation for TradeDecision factors."""
        return {
            "factor": "token_rug_risk",
            "value": round(self.score, 2),
            "note": {
                "hard_blocked": self.hard_blocked,
                "blocked_reasons": self.blocked_reasons,
                "components": {
                    c.name: {"score": round(c.score, 1), "w": round(c.weight, 3)}
                    for c in self.components
                },
                "weights_version": self.weights_version,
            },
        }
