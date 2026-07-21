"""Token metadata backfill: supply/decimals plus Metaplex name/symbol/uri.

Targets token rows still missing ``decimals`` or ``symbol`` (newest first, so
freshly launched tokens get named before the long tail). Supply and decimals
come from ``getTokenSupply``; name/symbol/uri come from the Metaplex metadata
PDA, whose borsh layout is decoded manually — the fixed prefix is
``u8 key | [32] update_authority | [32] mint`` followed by three
u32-length-prefixed strings (name, symbol, uri), each NUL-padded on chain.
"""

from __future__ import annotations

import base64
import struct
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pydantic import BaseModel
from solders.pubkey import Pubkey
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Token
from app.ingestion.programs import METAPLEX_METADATA
from app.logging_config import get_logger

log = get_logger(__name__)

_METADATA_PROGRAM = Pubkey.from_string(METAPLEX_METADATA)
# u8 key + 32-byte update_authority + 32-byte mint precede the string block.
_HEADER_LEN = 1 + 32 + 32
_SYMBOL_MAX = 64  # tokens.symbol column width
_NAME_MAX = 256  # tokens.name column width


class _Rpc(Protocol):
    async def get_token_supply(self, mint: str) -> dict | None: ...

    async def get_account_info(self, pubkey: str, encoding: str = "base64") -> dict | None: ...


class MetaplexMetadata(BaseModel):
    """Decoded prefix of a Metaplex ``Metadata`` account."""

    key: int
    update_authority: str
    mint: str
    name: str
    symbol: str
    uri: str


def metadata_pda(mint: str) -> str:
    """Derive the Metaplex metadata PDA for ``mint``."""
    mint_key = Pubkey.from_string(mint)
    address, _bump = Pubkey.find_program_address(
        [b"metadata", bytes(_METADATA_PROGRAM), bytes(mint_key)], _METADATA_PROGRAM
    )
    return str(address)


def _read_borsh_string(raw: bytes, offset: int) -> tuple[str, int]:
    """Read one u32-length-prefixed string; strip on-chain NUL padding."""
    if offset + 4 > len(raw):
        raise ValueError("truncated string length prefix")
    (length,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    if length > len(raw) - offset:
        raise ValueError("string exceeds buffer")
    value = raw[offset : offset + length].decode("utf-8", errors="replace")
    return value.rstrip("\x00").strip(), offset + length


def decode_metadata(raw: bytes) -> MetaplexMetadata | None:
    """Decode the borsh Metadata layout. Pure (no I/O); None on malformed data."""
    if len(raw) < _HEADER_LEN + 4:
        return None
    try:
        name, offset = _read_borsh_string(raw, _HEADER_LEN)
        symbol, offset = _read_borsh_string(raw, offset)
        uri, offset = _read_borsh_string(raw, offset)
    except ValueError:
        return None
    return MetaplexMetadata(
        key=raw[0],
        update_authority=str(Pubkey.from_bytes(raw[1:33])),
        mint=str(Pubkey.from_bytes(raw[33:65])),
        name=name,
        symbol=symbol,
        uri=uri,
    )


def _account_data(value: dict | None) -> bytes | None:
    """Extract raw bytes from a base64-encoded getAccountInfo value."""
    if not value:
        return None
    data = value.get("data")
    if isinstance(data, (list, tuple)) and data:
        encoded = data[0]
    elif isinstance(data, str):
        encoded = data
    else:
        return None
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError):
        return None


async def _refresh_token(rpc: _Rpc, token: Token, now: datetime) -> bool:
    """Fill supply/decimals and Metaplex fields on one token row."""
    changed = False

    supply = await rpc.get_token_supply(token.mint)
    if supply:
        decimals = supply.get("decimals")
        if decimals is not None:
            token.decimals = int(decimals)
            changed = True
        ui_amount = supply.get("uiAmountString")
        if ui_amount is not None:
            try:
                token.supply = Decimal(str(ui_amount))
                changed = True
            except InvalidOperation:
                log.warning("token_supply_unparseable", mint=token.mint, value=str(ui_amount))

    account = await rpc.get_account_info(metadata_pda(token.mint), encoding="base64")
    raw = _account_data(account)
    meta = decode_metadata(raw) if raw is not None else None
    if meta is not None:
        if meta.symbol:
            token.symbol = meta.symbol[:_SYMBOL_MAX]
        if meta.name:
            token.name = meta.name[:_NAME_MAX]
        if meta.uri:
            token.metadata_uri = meta.uri
        token.metadata_updated_at = now
        changed = True
    return changed


async def run_once(
    session: AsyncSession,
    rpc: _Rpc,
    *,
    batch: int,
    now: datetime | None = None,
) -> int:
    """Single metadata pass; returns the number of token rows updated."""
    now = now or datetime.now(tz=UTC)
    tokens = (
        (
            await session.execute(
                select(Token)
                .where(or_(Token.decimals.is_(None), Token.symbol.is_(None)))
                .order_by(Token.first_seen_at.desc())
                .limit(batch)
            )
        )
        .scalars()
        .all()
    )

    updated = 0
    for token in tokens:
        try:
            if await _refresh_token(rpc, token, now):
                updated += 1
        except Exception as exc:
            log.warning("token_metadata_failed", mint=token.mint, error=str(exc))
    await session.commit()
    if tokens:
        log.info("token_metadata_cycle", scanned=len(tokens), updated=updated)
    return updated
