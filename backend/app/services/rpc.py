"""Minimal async Solana JSON-RPC client.

Uses raw JSON-RPC over httpx instead of heavyweight SDKs: we only need a
handful of read methods, want full control over retries/rate limiting, and
parse ``jsonParsed`` transaction JSON directly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.logging_config import get_logger
from app.services import metrics

log = get_logger(__name__)

RETRYABLE_HTTP = {429, 500, 502, 503, 504}
# Node-side transient errors (node behind, tx not yet available, etc.)
RETRYABLE_RPC_CODES = {-32004, -32005, -32014}


class RpcError(Exception):
    def __init__(self, code: int, message: str, method: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.code = code
        self.message = message
        self.method = method


class _RateLimiter:
    def __init__(self, requests_per_second: float):
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


class SolanaRpc:
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 5,
        requests_per_second: float = 8.0,
    ):
        self._url = url
        self._max_retries = max_retries
        self._limiter = _RateLimiter(requests_per_second)
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        backoff = 0.5
        for attempt in range(self._max_retries + 1):
            await self._limiter.wait()
            started = time.monotonic()
            try:
                resp = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                metrics.RPC_REQUESTS.labels(method=method, status="network_error").inc()
                if attempt >= self._max_retries:
                    raise
                log.warning("rpc_network_error", method=method, error=str(exc), attempt=attempt)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)
                continue
            finally:
                metrics.RPC_LATENCY.labels(method=method).observe(time.monotonic() - started)

            if resp.status_code in RETRYABLE_HTTP:
                metrics.RPC_REQUESTS.labels(method=method, status=str(resp.status_code)).inc()
                if attempt >= self._max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, 15)
                continue
            resp.raise_for_status()

            body = resp.json()
            if "error" in body:
                code = int(body["error"].get("code", 0))
                message = str(body["error"].get("message", ""))
                if code in RETRYABLE_RPC_CODES and attempt < self._max_retries:
                    metrics.RPC_REQUESTS.labels(method=method, status="rpc_retry").inc()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 15)
                    continue
                metrics.RPC_REQUESTS.labels(method=method, status="rpc_error").inc()
                raise RpcError(code, message, method)

            metrics.RPC_REQUESTS.labels(method=method, status="ok").inc()
            return body.get("result")
        raise RuntimeError(f"rpc retries exhausted for {method}")  # pragma: no cover

    # --- typed wrappers ----------------------------------------------------

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    async def get_balance(self, pubkey: str) -> int | None:
        result = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return None if result is None else int(result.get("value", 0))

    async def get_account_info(self, pubkey: str, encoding: str = "base64") -> dict | None:
        result = await self.call(
            "getAccountInfo", [pubkey, {"encoding": encoding, "commitment": "confirmed"}]
        )
        return None if result is None else result.get("value")

    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict | None]:
        result = await self.call(
            "getMultipleAccounts", [pubkeys, {"encoding": encoding, "commitment": "confirmed"}]
        )
        return [] if result is None else list(result.get("value", []))

    async def get_token_supply(self, mint: str) -> dict | None:
        result = await self.call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        return None if result is None else result.get("value")

    async def get_token_account_balance(self, account: str) -> dict | None:
        result = await self.call("getTokenAccountBalance", [account, {"commitment": "confirmed"}])
        return None if result is None else result.get("value")

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        result = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return [] if result is None else list(result.get("value", []))

    # --- transaction submission (Phase 3 live execution) -------------------

    async def simulate_transaction(self, tx_base64: str) -> dict | None:
        result = await self.call(
            "simulateTransaction",
            [tx_base64, {"encoding": "base64", "commitment": "confirmed",
                         "replaceRecentBlockhash": True}],
        )
        return None if result is None else result.get("value")

    async def send_transaction(self, tx_base64: str) -> str:
        """Submit a signed transaction; returns the signature. Raises on error."""
        return await self.call(
            "sendTransaction",
            [tx_base64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict | None]:
        result = await self.call(
            "getSignatureStatuses", [signatures, {"searchTransactionHistory": False}]
        )
        return [] if result is None else list(result.get("value", []))

    async def get_program_accounts_count(
        self, program_id: str, filters: list[dict] | None = None
    ) -> int:
        """Count matching program accounts without pulling account data."""
        params: list[Any] = [
            program_id,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "dataSlice": {"offset": 0, "length": 0},
                **({"filters": filters} if filters else {}),
            },
        ]
        result = await self.call("getProgramAccounts", params)
        return 0 if result is None else len(result)
