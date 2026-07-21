"""Tests for the on-chain token security probe."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import DexPool, Token
from app.enrichment.token_security import INCINERATOR, TokenSecurityProbe

MINT = "M1nt1111111111111111111111111111111111111111"
LP_MINT = "Lp11111111111111111111111111111111111111111"
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _mint_account(mint_auth: str | None, freeze_auth: str | None) -> dict:
    info: dict = {"decimals": 6, "supply": "1000"}
    if mint_auth is not None:
        info["mintAuthority"] = mint_auth
    if freeze_auth is not None:
        info["freezeAuthority"] = freeze_auth
    return {"data": {"parsed": {"info": info, "type": "mint"}, "program": "spl-token"}}


def _holders(entries: list[tuple[str, int]]) -> list[dict]:
    return [{"address": a, "amount": str(v), "uiAmount": float(v)} for a, v in entries]


class FakeRpc:
    """Canned-response RPC double; raise RuntimeError for keys in ``fail``."""

    def __init__(
        self,
        accounts: dict[str, dict] | None = None,
        largest: dict[str, list[dict]] | None = None,
        supplies: dict[str, dict] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.accounts = accounts or {}
        self.largest = largest or {}
        self.supplies = supplies or {}
        self.fail = fail or set()

    def _check(self, key: str) -> None:
        if key in self.fail:
            raise RuntimeError(f"rpc down: {key}")

    async def get_account_info(
        self, pubkey: str, encoding: str = "base64", budget_exempt: bool | None = None
    ) -> dict | None:
        self._check(f"account:{pubkey}")
        return self.accounts.get(pubkey)

    async def get_token_supply(self, mint: str, budget_exempt: bool | None = None) -> dict | None:
        self._check(f"supply:{mint}")
        return self.supplies.get(mint)

    async def get_token_largest_accounts(self, mint: str, budget_exempt: bool | None = None) -> list[dict]:
        self._check(f"largest:{mint}")
        return self.largest.get(mint, [])


async def _make_token(session: AsyncSession, primary_dex: str | None = "raydium") -> Token:
    token = Token(mint=MINT, primary_dex=primary_dex, first_seen_at=NOW)
    session.add(token)
    await session.flush()
    return token


async def _make_pool(
    session: AsyncSession,
    token: Token,
    lp_mint: str | None = LP_MINT,
    base_vault: str | None = "VaultBase111",
    quote_vault: str | None = "VaultQuote11",
) -> DexPool:
    pool = DexPool(
        address="Pool11111111",
        dex="raydium",
        token_id=token.id,
        base_mint=token.mint,
        quote_mint="So11111111111111111111111111111111111111112",
        lp_mint=lp_mint,
        base_vault=base_vault,
        quote_vault=quote_vault,
        first_seen_at=NOW,
    )
    session.add(pool)
    await session.flush()
    return pool


async def test_active_authorities_reported_and_persisted(db_session):
    token = await _make_token(db_session)
    rpc = FakeRpc(accounts={MINT: _mint_account("AuthA", "AuthF")})
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.mint_authority == "AuthA"
    assert signals.freeze_authority == "AuthF"
    assert token.mint_authority == "AuthA"
    assert token.freeze_authority == "AuthF"
    assert token.security_checked_at is not None


async def test_renounced_authorities_stored_as_empty_string(db_session):
    token = await _make_token(db_session)
    rpc = FakeRpc(accounts={MINT: _mint_account(None, None)})
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.mint_authority == ""
    assert signals.freeze_authority == ""
    assert token.mint_authority == ""
    assert token.freeze_authority == ""


async def test_top10_excludes_pool_vaults_and_clamps(db_session):
    token = await _make_token(db_session)
    await _make_pool(db_session, token, lp_mint=None)
    rpc = FakeRpc(
        accounts={MINT: _mint_account(None, None)},
        largest={
            MINT: _holders(
                [("VaultBase111", 900), ("VaultQuote11", 50), ("whale", 300), ("shrimp", 100)]
            )
        },
        supplies={MINT: {"amount": "1000", "decimals": 6}},
    )
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.top10_holder_pct == 0.4
    assert signals.holder_sample_count == 2
    assert signals.lp_exists is False


async def test_bonding_curve_detection(db_session):
    token = await _make_token(db_session, primary_dex="pumpfun")
    rpc = FakeRpc(
        accounts={MINT: _mint_account("AuthA", None)},
        largest={MINT: _holders([("whale", 500)])},
        supplies={MINT: {"amount": "1000", "decimals": 6}},
    )
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.is_bonding_curve is True
    assert signals.lp_exists is False


async def test_lp_burn_math_with_incinerator(db_session):
    token = await _make_token(db_session)
    await _make_pool(db_session, token)
    rpc = FakeRpc(
        accounts={MINT: _mint_account(None, None)},
        largest={
            MINT: _holders([("whale", 100)]),
            LP_MINT: _holders([(INCINERATOR, 300), ("lpwhale", 150), ("lpother", 50)]),
        },
        supplies={
            MINT: {"amount": "1000", "decimals": 6},
            LP_MINT: {"amount": "500", "decimals": 6},
        },
    )
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.lp_exists is True
    assert signals.lp_burned_pct == 300 / (500 + 300)
    assert signals.lp_top_holder_pct == 150 / 500


async def test_lp_zero_supply_treated_as_fully_burned(db_session):
    token = await _make_token(db_session)
    await _make_pool(db_session, token)
    rpc = FakeRpc(
        accounts={MINT: _mint_account(None, None)},
        largest={MINT: _holders([("whale", 100)]), LP_MINT: []},
        supplies={
            MINT: {"amount": "1000", "decimals": 6},
            LP_MINT: {"amount": "0", "decimals": 6},
        },
    )
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.lp_burned_pct == 1.0
    assert signals.lp_top_holder_pct is None


async def test_rpc_failures_yield_nones_and_probe_errors(db_session):
    token = await _make_token(db_session)
    await _make_pool(db_session, token)
    rpc = FakeRpc(
        fail={f"account:{MINT}", f"largest:{MINT}", f"largest:{LP_MINT}"}
    )
    signals = await TokenSecurityProbe(rpc, Settings()).probe(db_session, token)

    assert signals.mint_authority is None
    assert signals.freeze_authority is None
    assert signals.top10_holder_pct is None
    assert signals.lp_burned_pct is None
    assert signals.lp_top_holder_pct is None
    assert signals.lp_exists is True
    assert len(signals.probe_errors) == 3
    assert token.mint_authority is None
    assert token.security_checked_at is None
