"""Minimal Jupiter swap-API client (quote + swap-transaction build).

HTTP goes through an injectable async ``fetch_json`` callable so tests never
touch the network; the default is a small httpx wrapper.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx

from app.ingestion.parsers.util import LAMPORTS_PER_SOL
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger

log = get_logger(__name__)

FetchJson = Callable[[str, str, dict | None], Awaitable[Any]]


async def _http_fetch_json(method: str, url: str, body: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=15.0) as client:
        if method == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url, json=body)
        response.raise_for_status()
        return response.json()


class JupiterClient:
    def __init__(self, base_url: str, fetch_json: FetchJson | None = None):
        self._base = base_url.rstrip("/")
        self._fetch = fetch_json or _http_fetch_json

    async def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int,
    ) -> dict | None:
        url = (
            f"{self._base}/quote?inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={amount_lamports}&slippageBps={slippage_bps}&swapMode=ExactIn"
        )
        try:
            payload = await self._fetch("GET", url, None)
        except Exception as exc:
            log.warning("jupiter_quote_failed", error=str(exc))
            return None
        if not isinstance(payload, dict) or "outAmount" not in payload:
            log.warning("jupiter_quote_malformed")
            return None
        return payload

    async def swap_transaction(self, *, quote: dict, user_public_key: str) -> str | None:
        """Base64 unsigned transaction for the given quote, or None."""
        try:
            payload = await self._fetch(
                "POST",
                f"{self._base}/swap",
                {
                    "quoteResponse": quote,
                    "userPublicKey": user_public_key,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                },
            )
        except Exception as exc:
            log.warning("jupiter_swap_build_failed", error=str(exc))
            return None
        tx = payload.get("swapTransaction") if isinstance(payload, dict) else None
        return tx if isinstance(tx, str) and tx else None


def buy_fill_from_quote(quote: dict, token_decimals: int) -> tuple[Decimal, Decimal] | None:
    """(token_amount, price_sol_per_token) implied by an ExactIn SOL->token quote."""
    try:
        out_amount = Decimal(str(quote["outAmount"])) / (Decimal(10) ** token_decimals)
        in_sol = Decimal(str(quote["inAmount"])) / LAMPORTS_PER_SOL
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None
    if out_amount <= 0:
        return None
    return out_amount, in_sol / out_amount


def sol_proceeds_from_quote(quote: dict) -> Decimal | None:
    """SOL received for an ExactIn token->SOL quote."""
    try:
        if quote.get("outputMint") != WSOL_MINT:
            return None
        return Decimal(str(quote["outAmount"])) / LAMPORTS_PER_SOL
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None
