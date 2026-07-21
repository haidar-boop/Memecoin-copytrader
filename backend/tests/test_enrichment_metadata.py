"""Metaplex borsh decoding and token metadata job tests."""

from __future__ import annotations

import base64
import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from solders.pubkey import Pubkey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token
from app.enrichment import token_metadata
from app.ingestion.programs import METAPLEX_METADATA

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)

UPDATE_AUTHORITY = Pubkey.from_bytes(bytes([7] * 32))
MINT_A = Pubkey.from_bytes(bytes([1] * 32))
MINT_B = Pubkey.from_bytes(bytes([2] * 32))
MINT_C = Pubkey.from_bytes(bytes([3] * 32))


def borsh_string(value: str, padded_len: int | None = None) -> bytes:
    """u32-LE length prefix + bytes, NUL-padded to the on-chain fixed width."""
    raw = value.encode("utf-8")
    if padded_len is not None:
        raw = raw.ljust(padded_len, b"\x00")
    return struct.pack("<I", len(raw)) + raw


def build_metadata(mint: Pubkey, name: str, symbol: str, uri: str) -> bytes:
    """Realistic Metadata account bytes: fixed header, padded strings, tail."""
    return (
        bytes([4])  # key = MetadataV1
        + bytes(UPDATE_AUTHORITY)
        + bytes(mint)
        + borsh_string(name, 32)
        + borsh_string(symbol, 10)
        + borsh_string(uri, 200)
        + struct.pack("<H", 500)  # seller_fee_basis_points
        + b"\x00"  # creators: Option::None (decoder must ignore the tail)
    )


def wrap_account(data: bytes) -> dict:
    """Shape of a getAccountInfo value with base64 encoding."""
    return {
        "data": [base64.b64encode(data).decode(), "base64"],
        "owner": METAPLEX_METADATA,
        "lamports": 5616720,
        "executable": False,
    }


class StubRpc:
    def __init__(self) -> None:
        self.supply_by_mint: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.supply_calls: list[str] = []
        self.account_calls: list[str] = []

    async def get_token_supply(self, mint: str) -> dict | None:
        self.supply_calls.append(mint)
        return self.supply_by_mint.get(mint)

    async def get_account_info(self, pubkey: str, encoding: str = "base64") -> dict | None:
        self.account_calls.append(pubkey)
        return self.accounts.get(pubkey)


# --- pure borsh decoding ----------------------------------------------------


def test_decode_metadata_strips_nul_padding() -> None:
    raw = build_metadata(MINT_A, "Doge Wif Hat", "WIF", "https://arweave.net/abc")
    meta = token_metadata.decode_metadata(raw)
    assert meta is not None
    assert meta.key == 4
    assert meta.update_authority == str(UPDATE_AUTHORITY)
    assert meta.mint == str(MINT_A)
    assert meta.name == "Doge Wif Hat"
    assert meta.symbol == "WIF"
    assert meta.uri == "https://arweave.net/abc"


def test_decode_metadata_rejects_truncated_buffers() -> None:
    raw = build_metadata(MINT_A, "Doge", "DOGE", "u")
    assert token_metadata.decode_metadata(raw[:70]) is None  # cuts inside name
    assert token_metadata.decode_metadata(b"") is None
    assert token_metadata.decode_metadata(bytes(40)) is None


def test_decode_metadata_rejects_oversized_length_prefix() -> None:
    raw = bytes([4]) + bytes(UPDATE_AUTHORITY) + bytes(MINT_A) + struct.pack("<I", 2**31)
    assert token_metadata.decode_metadata(raw) is None


def test_metadata_pda_matches_solders_derivation() -> None:
    program = Pubkey.from_string(METAPLEX_METADATA)
    expected, _bump = Pubkey.find_program_address(
        [b"metadata", bytes(program), bytes(MINT_A)], program
    )
    assert token_metadata.metadata_pda(str(MINT_A)) == str(expected)


# --- run_once against the database ------------------------------------------


async def test_run_once_fills_supply_and_metadata(db_session: AsyncSession) -> None:
    incomplete = Token(mint=str(MINT_A), first_seen_at=T0)
    complete = Token(mint=str(MINT_B), symbol="DONE", decimals=9, first_seen_at=T0)
    db_session.add_all([incomplete, complete])
    await db_session.commit()

    rpc = StubRpc()
    rpc.supply_by_mint[str(MINT_A)] = {
        "amount": "1000000000000000",
        "decimals": 6,
        "uiAmount": 1_000_000_000.0,
        "uiAmountString": "1000000000",
    }
    pda = token_metadata.metadata_pda(str(MINT_A))
    rpc.accounts[pda] = wrap_account(
        build_metadata(MINT_A, "Doge Wif Hat", "WIF", "https://arweave.net/abc")
    )

    updated = await token_metadata.run_once(db_session, rpc, batch=10, now=T0)

    assert updated == 1
    refreshed = (
        await db_session.execute(select(Token).where(Token.mint == str(MINT_A)))
    ).scalar_one()
    assert refreshed.decimals == 6
    assert refreshed.supply == Decimal("1000000000")
    assert refreshed.symbol == "WIF"
    assert refreshed.name == "Doge Wif Hat"
    assert refreshed.metadata_uri == "https://arweave.net/abc"
    assert refreshed.metadata_updated_at is not None
    # The already-complete token was never queried.
    assert rpc.supply_calls == [str(MINT_A)]
    assert rpc.account_calls == [pda]


async def test_run_once_prefers_newest_and_honors_batch(db_session: AsyncSession) -> None:
    older = Token(mint=str(MINT_B), first_seen_at=T0 - timedelta(hours=1))
    newer = Token(mint=str(MINT_C), first_seen_at=T0)
    db_session.add_all([older, newer])
    await db_session.commit()

    rpc = StubRpc()
    rpc.supply_by_mint[str(MINT_C)] = {
        "amount": "42000000000",
        "decimals": 9,
        "uiAmountString": "42",
    }

    await token_metadata.run_once(db_session, rpc, batch=1, now=T0)

    assert rpc.supply_calls == [str(MINT_C)]
    assert newer.decimals == 9
    assert newer.supply == Decimal("42")
    assert older.decimals is None


async def test_run_once_without_metadata_account_sets_supply_only(
    db_session: AsyncSession,
) -> None:
    token = Token(mint=str(MINT_A), first_seen_at=T0)
    db_session.add(token)
    await db_session.commit()

    rpc = StubRpc()  # no metadata PDA account registered
    rpc.supply_by_mint[str(MINT_A)] = {
        "amount": "12350000",
        "decimals": 5,
        "uiAmountString": "123.5",
    }

    updated = await token_metadata.run_once(db_session, rpc, batch=10, now=T0)

    assert updated == 1
    assert token.decimals == 5
    assert token.supply == Decimal("123.5")
    assert token.symbol is None
    assert token.metadata_updated_at is None


async def test_run_once_survives_bad_mint(db_session: AsyncSession) -> None:
    bad = Token(mint="not-a-base58-key!!", first_seen_at=T0)  # newest: processed first
    good = Token(mint=str(MINT_A), first_seen_at=T0 - timedelta(minutes=1))
    db_session.add_all([bad, good])
    await db_session.commit()

    rpc = StubRpc()
    rpc.supply_by_mint[str(MINT_A)] = {
        "amount": "1000000",
        "decimals": 6,
        "uiAmountString": "1",
    }

    updated = await token_metadata.run_once(db_session, rpc, batch=10, now=T0)

    assert updated == 1  # the bad mint is skipped, the good one still lands
    assert good.decimals == 6
    assert bad.decimals is None
