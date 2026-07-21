"""Solana WebSocket listener.

Subscribes to ``logsSubscribe`` (mentions filter) for every enabled DEX
program and pushes transaction signatures onto the Redis ingest stream.
Failed transactions are enqueued too — failure patterns are training data.

Fetching/parsing happens in the ingest-writer worker so a slow RPC never
backpressures the socket.
"""

from __future__ import annotations

import asyncio
import json
import random

import redis.asyncio as aioredis
import websockets

from app.config import Settings
from app.ingestion.programs import listen_program_ids
from app.logging_config import get_logger
from app.services import metrics
from app.services.redis import try_dedup

log = get_logger(__name__)

MAX_BACKOFF_SECONDS = 60.0


class LogsListener:
    def __init__(self, settings: Settings, redis: aioredis.Redis):
        self._settings = settings
        self._redis = redis
        self._programs = listen_program_ids(settings.enabled_dexes)
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        if not self._programs:
            log.error("listener_no_programs_enabled")
            return
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                await self._run_connection()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics.WS_RECONNECTS.inc()
                delay = min(backoff, MAX_BACKOFF_SECONDS) * (0.5 + random.random())
                log.warning(
                    "ws_disconnected", error=str(exc), retry_in_seconds=round(delay, 1)
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _run_connection(self) -> None:
        async with websockets.connect(
            self._settings.solana_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=20 * 1024 * 1024,
        ) as ws:
            sub_id_to_program: dict[int, str] = {}
            request_id_to_program: dict[int, str] = {}
            for request_id, program_id in enumerate(self._programs, start=1):
                request_id_to_program[request_id] = program_id
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [program_id]},
                                {"commitment": "confirmed"},
                            ],
                        }
                    )
                )
            log.info("ws_connected", programs=list(self._programs.values()))

            while not self._stopped.is_set():
                message = json.loads(await ws.recv())
                if "id" in message and "result" in message:
                    program_id = request_id_to_program.get(message["id"])
                    if program_id is not None:
                        sub_id_to_program[message["result"]] = program_id
                    continue
                if message.get("method") != "logsNotification":
                    continue
                params = message.get("params", {})
                subscription = params.get("subscription")
                program_id = sub_id_to_program.get(subscription, "unknown")
                result = params.get("result", {})
                slot = result.get("context", {}).get("slot", 0)
                value = result.get("value", {})
                signature = value.get("signature")
                if not signature:
                    continue
                await self._enqueue(
                    signature=signature,
                    slot=slot,
                    failed=value.get("err") is not None,
                    program_id=program_id,
                )

    async def _enqueue(self, signature: str, slot: int, failed: bool, program_id: str) -> None:
        program = self._programs.get(program_id, program_id)
        metrics.WS_MESSAGES.labels(program=program).inc()
        metrics.LAST_SLOT.set(slot)
        dedup_key = f"ingest:dedup:{signature}"
        if not await try_dedup(
            self._redis, dedup_key, self._settings.ingest_dedup_ttl_seconds
        ):
            metrics.SIGNATURES_DEDUPED.inc()
            return
        try:
            await self._redis.xadd(
                self._settings.ingest_stream_key,
                {
                    "signature": signature,
                    "slot": str(slot),
                    "failed": "1" if failed else "0",
                    "program": program,
                    "attempts": "0",
                },
                maxlen=self._settings.ingest_stream_maxlen,
                approximate=True,
            )
        except Exception:
            # Release the dedup claim: logsSubscribe fires once per tx, so a
            # claimed-but-never-enqueued signature would be lost for the whole
            # dedup TTL otherwise.
            try:
                await self._redis.delete(dedup_key)
            except Exception:
                log.warning("dedup_release_failed", signature=signature)
            raise
        metrics.SIGNATURES_ENQUEUED.inc()
        await self._redis.hset(
            "ingest:checkpoint:listener", mapping={"slot": str(slot), "signature": signature}
        )
