"""Tests for the token rug-risk engine (hard filters + weighted scoring)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.models import Token, TokenRiskAssessment, TokenSnapshot, Trade
from app.decision.risk_contracts import (
    BASE_WEIGHTS,
    ENGINE_VERSION,
    RISK_WEIGHTS_KEY,
    TokenSecuritySignals,
)
from app.decision.token_risk import TokenRiskEngine

NOW = datetime.now(UTC)


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def safe_signals(**overrides) -> TokenSecuritySignals:
    base = dict(
        mint="MintAAA",
        mint_authority="",
        freeze_authority="",
        top10_holder_pct=0.30,
        lp_exists=True,
        lp_burned_pct=0.95,
        lp_top_holder_pct=0.02,
    )
    base.update(overrides)
    return TokenSecuritySignals(**base)


class CountingProbe:
    def __init__(self, signals: TokenSecuritySignals) -> None:
        self.signals = signals
        self.calls = 0

    async def __call__(self) -> TokenSecuritySignals:
        self.calls += 1
        return self.signals


async def make_token(db_session, mint: str = "MintAAA", **kw) -> Token:
    token = Token(mint=mint, first_seen_at=kw.pop("first_seen_at", NOW), **kw)
    db_session.add(token)
    await db_session.flush()
    return token


def engine(stub_redis, **overrides) -> TokenRiskEngine:
    return TokenRiskEngine(make_settings(**overrides), stub_redis)


def component(verdict, name: str):
    return next(c for c in verdict.components if c.name == name)


# -- disabled short-circuit -------------------------------------------------


async def test_disabled_skips_probe_and_persistence(db_session, stub_redis):
    token = await make_token(db_session)
    probe = CountingProbe(safe_signals())
    verdict = await engine(stub_redis, rug_check_enabled=False).assess(
        db_session, token, probe
    )
    assert probe.calls == 0
    assert verdict.score == 0.0
    assert not verdict.hard_blocked
    assert verdict.components[0].note == "disabled"
    rows = (await db_session.execute(TokenRiskAssessment.__table__.select())).all()
    assert rows == []


# -- hard filters -----------------------------------------------------------


@pytest.mark.parametrize(
    ("signal_overrides", "reason"),
    [
        ({"mint_authority": "SomePubkey"}, "mint_authority_active"),
        ({"freeze_authority": "SomePubkey"}, "freeze_authority_active"),
        ({"top10_holder_pct": 0.75}, "holder_concentration_extreme"),
        ({"mint_authority": None}, "authority_unknown_fail_closed"),
        ({"freeze_authority": None}, "authority_unknown_fail_closed"),
    ],
)
async def test_hard_block_paths(db_session, stub_redis, signal_overrides, reason):
    token = await make_token(db_session)
    probe = CountingProbe(safe_signals(**signal_overrides))
    verdict = await engine(stub_redis).assess(db_session, token, probe)
    assert verdict.hard_blocked
    assert reason in verdict.blocked_reasons
    # Hard block never inflates the weighted score to 100 artificially.
    assert verdict.score < 100.0


async def test_bonding_curve_mint_authority_not_hard_blocked(db_session, stub_redis):
    """A pump.fun bonding-curve token's mint authority is the curve PDA
    itself (held until migration so the program can mint the graduation
    chunk) — not an EOA that can print supply at will. Hard-blocking it
    would reject essentially every pre-migration pump.fun token."""
    token = await make_token(db_session)
    probe = CountingProbe(
        safe_signals(mint_authority="CurvePda111", is_bonding_curve=True)
    )
    verdict = await engine(stub_redis).assess(db_session, token, probe)
    assert "mint_authority_active" not in verdict.blocked_reasons
    # Still penalized in the soft score, just not a structural veto.
    assert component(verdict, "authority").score == 100.0


async def test_fail_open_unknown_authority_not_blocked(db_session, stub_redis):
    token = await make_token(db_session)
    probe = CountingProbe(safe_signals(mint_authority=None, freeze_authority=None))
    verdict = await engine(stub_redis, rug_fail_closed=False).assess(
        db_session, token, probe
    )
    assert not verdict.hard_blocked
    assert component(verdict, "authority").score == 60.0


async def test_authority_blocks_respect_config_switches(db_session, stub_redis):
    token = await make_token(db_session)
    probe = CountingProbe(
        safe_signals(mint_authority="Pk1", freeze_authority="Pk2")
    )
    verdict = await engine(
        stub_redis,
        rug_block_mint_authority=False,
        rug_block_freeze_authority=False,
    ).assess(db_session, token, probe)
    assert not verdict.hard_blocked


# -- TTL reuse --------------------------------------------------------------


async def test_ttl_reuse_skips_probe(db_session, stub_redis):
    token = await make_token(db_session)
    probe = CountingProbe(safe_signals())
    eng = engine(stub_redis)
    first = await eng.assess(db_session, token, probe)
    second = await eng.assess(db_session, token, probe)
    assert probe.calls == 1
    assert second.assessment_id == first.assessment_id
    assert second.score == pytest.approx(first.score)
    assert [c.name for c in second.components] == [c.name for c in first.components]


async def test_stale_or_wrong_version_assessment_reprobes(db_session, stub_redis):
    token = await make_token(db_session)
    stale = TokenRiskAssessment(
        token_id=token.id,
        mint=token.mint,
        ts=NOW - timedelta(hours=2),
        score=Decimal(5),
        hard_blocked=False,
        engine_version=ENGINE_VERSION,
        weights_version=0,
    )
    wrong_version = TokenRiskAssessment(
        token_id=token.id,
        mint=token.mint,
        ts=NOW,
        score=Decimal(5),
        hard_blocked=False,
        engine_version="0-legacy",
        weights_version=0,
    )
    db_session.add_all([stale, wrong_version])
    await db_session.flush()
    probe = CountingProbe(safe_signals())
    await engine(stub_redis).assess(db_session, token, probe)
    assert probe.calls == 1


# -- weights ----------------------------------------------------------------


async def test_malformed_weights_fall_back_to_base(db_session, stub_redis):
    stub_redis.data[RISK_WEIGHTS_KEY] = "not json {"
    token = await make_token(db_session)
    verdict = await engine(stub_redis).assess(
        db_session, token, CountingProbe(safe_signals())
    )
    assert verdict.weights_version == 0
    for c in verdict.components:
        assert c.weight == pytest.approx(BASE_WEIGHTS[c.name])


async def test_weights_missing_key_uses_base(db_session, stub_redis):
    stub_redis.data[RISK_WEIGHTS_KEY] = json.dumps({"version": 3})
    token = await make_token(db_session)
    verdict = await engine(stub_redis).assess(
        db_session, token, CountingProbe(safe_signals())
    )
    assert verdict.weights_version == 0


async def test_out_of_bounds_weights_clamped_and_renormalized(db_session, stub_redis):
    learned = dict(BASE_WEIGHTS)
    learned["authority"] = 5.0  # way above 2x base 0.20 -> clamps to 0.40
    learned["lp_security"] = 0.0  # below 0.5x base 0.15 -> clamps to 0.075
    stub_redis.data[RISK_WEIGHTS_KEY] = json.dumps({"version": 7, "weights": learned})
    token = await make_token(db_session)
    verdict = await engine(stub_redis).assess(
        db_session, token, CountingProbe(safe_signals())
    )
    assert verdict.weights_version == 7
    weights = {c.name: c.weight for c in verdict.components}
    assert sum(weights.values()) == pytest.approx(1.0)
    total = 0.40 + 0.20 + 0.075 + 0.15 + 0.15 + 0.15
    assert weights["authority"] == pytest.approx(0.40 / total)
    assert weights["lp_security"] == pytest.approx(0.075 / total)


# -- component pinned values ------------------------------------------------


@pytest.mark.parametrize(
    ("mint_auth", "freeze_auth", "expected"),
    [
        ("", "", 0.0),
        ("Pk", "", 100.0),
        ("Pk", "Pk2", 100.0),
        ("", "Pk", 80.0),
        (None, None, 60.0),
        ("", None, 60.0),
    ],
)
def test_authority_component(stub_redis, mint_auth, freeze_auth, expected):
    c = engine(stub_redis)._score_authority(
        safe_signals(mint_authority=mint_auth, freeze_authority=freeze_auth)
    )
    assert c.score == expected


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(None, 50.0), (0.10, 0.0), (0.20, 0.0), (0.45, 50.0), (0.70, 100.0), (0.95, 100.0)],
)
def test_holder_concentration_component(stub_redis, pct, expected):
    c = engine(stub_redis)._score_holder_concentration(
        safe_signals(top10_holder_pct=pct)
    )
    assert c.score == pytest.approx(expected)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"is_bonding_curve": True, "lp_exists": False, "lp_burned_pct": None}, 40.0),
        ({"lp_burned_pct": 0.95}, 0.0),
        ({"lp_burned_pct": 0.90}, 0.0),
        ({"lp_burned_pct": 0.60, "lp_top_holder_pct": 0.10}, 40.0),
        ({"lp_burned_pct": 0.20, "lp_top_holder_pct": 0.60}, 90.0),
        ({"lp_burned_pct": 0.60, "lp_top_holder_pct": 0.60}, 40.0),
        ({"lp_exists": False, "lp_burned_pct": None}, 50.0),
        ({"lp_burned_pct": None}, 50.0),
    ],
)
def test_lp_security_component(stub_redis, overrides, expected):
    c = engine(stub_redis)._score_lp_security(safe_signals(**overrides))
    assert c.score == pytest.approx(expected)


async def test_deployer_history_component(db_session, stub_redis):
    eng = engine(stub_redis)
    anon = await make_token(db_session, mint="Anon", creator=None)
    c = await eng._score_deployer_history(db_session, anon)
    assert c.score == 30.0

    fresh = await make_token(db_session, mint="Fresh", creator="CreatorX")
    c = await eng._score_deployer_history(db_session, fresh)
    assert c.score == 30.0

    dead1 = await make_token(db_session, mint="Dead1", creator="CreatorX")
    db_session.add(
        TokenSnapshot(token_id=dead1.id, ts=NOW, liquidity_sol=Decimal("0.5"))
    )
    await db_session.flush()
    dead2 = await make_token(db_session, mint="Dead2", creator="CreatorX")
    db_session.add(
        TokenSnapshot(
            token_id=dead2.id,
            ts=NOW - timedelta(hours=1),
            price_sol=Decimal("1.0"),
            liquidity_sol=Decimal("50"),
        )
    )
    await db_session.flush()
    db_session.add(
        TokenSnapshot(
            token_id=dead2.id,
            ts=NOW,
            price_sol=Decimal("0.05"),
            liquidity_sol=Decimal("50"),
        )
    )
    alive = await make_token(db_session, mint="Alive", creator="CreatorX")
    db_session.add(
        TokenSnapshot(
            token_id=alive.id, ts=NOW, price_sol=Decimal("1"), liquidity_sol=Decimal("80")
        )
    )
    await db_session.flush()
    c = await eng._score_deployer_history(db_session, fresh)
    # dead1 (drained liquidity) + dead2 (-95% price) dead, alive fine: 2/3.
    assert c.score == pytest.approx(100.0 * 2 / 3)


async def add_buys(db_session, token_id: int, buys: list[int]) -> None:
    """One buy Trade per entry; the entry is the buying wallet_id.

    Flushed row-by-row: SQLite cannot match insertmanyvalues sentinels on
    the tz-aware composite PK of the trades hypertable schema.
    """
    for i, wallet_id in enumerate(buys):
        db_session.add(
            Trade(
                signature=f"sig{token_id}-{i}",
                event_index=0,
                block_time=NOW - timedelta(minutes=5),
                slot=1,
                wallet_id=wallet_id,
                token_id=token_id,
                dex="pumpfun",
                side="buy",
                token_amount=Decimal(1),
                quote_amount=Decimal(1),
                quote_mint="So1",
            )
        )
        await db_session.flush()


async def test_volume_authenticity_component(db_session, stub_redis):
    eng = engine(stub_redis)
    sparse = await make_token(db_session, mint="Sparse")
    await add_buys(db_session, sparse.id, [1, 2, 3])
    botted = await make_token(db_session, mint="Botted")
    await add_buys(
        db_session, botted.id, [wallet for wallet in range(5) for _ in range(10)]
    )
    organic = await make_token(db_session, mint="Organic")
    await add_buys(db_session, organic.id, list(range(100, 120)))

    assert (await eng._score_volume_authenticity(db_session, sparse, NOW)).score == 50.0
    # 5 wallets / 50 buys: concentration 0.9 * 140 clamps to 100.
    assert (await eng._score_volume_authenticity(db_session, botted, NOW)).score == 100.0
    assert (await eng._score_volume_authenticity(db_session, organic, NOW)).score == 0.0


async def test_liquidity_age_component(db_session, stub_redis):
    eng = engine(stub_redis)
    newborn = await make_token(db_session, mint="Newborn", first_seen_at=NOW)
    assert (
        await eng._score_liquidity_age(db_session, newborn, NOW)
    ).score == pytest.approx(100.0)

    mid = await make_token(
        db_session, mint="Mid", first_seen_at=NOW - timedelta(hours=12)
    )
    db_session.add(TokenSnapshot(token_id=mid.id, ts=NOW, liquidity_sol=Decimal(100)))
    await db_session.flush()
    assert (
        await eng._score_liquidity_age(db_session, mid, NOW)
    ).score == pytest.approx(25.0)

    deep = await make_token(db_session, mint="Deep", first_seen_at=NOW)
    db_session.add(TokenSnapshot(token_id=deep.id, ts=NOW, liquidity_sol=Decimal(500)))
    old = await make_token(
        db_session, mint="Old", first_seen_at=NOW - timedelta(days=3)
    )
    await db_session.flush()
    assert (await eng._score_liquidity_age(db_session, deep, NOW)).score == 0.0
    assert (await eng._score_liquidity_age(db_session, old, NOW)).score == 0.0


# -- persistence ------------------------------------------------------------


async def test_persists_assessment_row_shape(db_session, stub_redis):
    token = await make_token(db_session)
    verdict = await engine(stub_redis).assess(
        db_session, token, CountingProbe(safe_signals())
    )
    assert verdict.assessment_id is not None
    row = await db_session.get(TokenRiskAssessment, verdict.assessment_id)
    assert row.token_id == token.id
    assert row.mint == token.mint
    assert float(row.score) == pytest.approx(verdict.score)
    assert row.hard_blocked is False
    assert row.blocked_reasons == []
    assert set(row.components) == set(BASE_WEIGHTS)
    for payload in row.components.values():
        assert set(payload) == {"score", "weight", "note"}
    assert row.signals["mint"] == token.mint
    assert row.signals["lp_burned_pct"] == 0.95
    assert row.engine_version == ENGINE_VERSION
    assert row.weights_version == 0
