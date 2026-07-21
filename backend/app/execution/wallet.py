"""Dedicated trading-wallet keypair handling.

The secret comes from configuration (env), accepts either a base58 string or
a JSON byte array (both Phantom-exportable), and is only ever loaded when
live mode is explicitly requested. The secret itself is never logged.
"""

from __future__ import annotations

import json

from solders.keypair import Keypair

from app.logging_config import get_logger

log = get_logger(__name__)


def load_keypair(secret: str | None) -> Keypair | None:
    if not secret:
        return None
    try:
        stripped = secret.strip()
        if stripped.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(stripped)))
        return Keypair.from_base58_string(stripped)
    except Exception:
        # Never include the secret (or parsing internals that may echo it).
        log.error("trading_wallet_secret_invalid")
        return None
