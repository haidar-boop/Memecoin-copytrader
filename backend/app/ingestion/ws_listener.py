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
from app.services.redis import get_followed_wallets, try_dedup

log = get_logger(__name__)

MAX_BACKOFF_SECONDS = 60.0

# Subscription target kinds (values of the request/sub id maps).
_KIND_PROGRAM = "program"
_KIND_WALLET = "wallet"


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

    async def _followed_wallets(self) -> list[str]:
        if not self._settings.follow_lane_enabled:
            return []
        wallets = await get_followed_wallets(self._redis)
        return wallets[: self._settings.follow_lane_max]

    async def _watch_follow_set(self, ws, subscribed: list[str]) -> None:
        """Force a reconnect (which re-subscribes everything) when the
        followed set changes — simpler and more robust than in-place
        unsubscribe bookkeeping, at the cost of a ~1s socket bounce."""
        while True:
            await asyncio.sleep(self._settings.follow_lane_refresh_seconds)
            current = await self._followed_wallets()
            if sorted(current) != sorted(subscribed):
                log.info(
                    "follow_lane_changed_reconnecting",
                    before=len(subscribed),
                    after=len(current),
                )
                await ws.close()
                return

    async def _run_connection(self) -> None:
        followed = await self._followed_wallets()
        async with websockets.connect(
            self._settings.solana_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=20 * 1024 * 1024,
        ) as ws:
            # request/sub id -> (kind, id): programs feed the sampled main
            # stream, wallets feed the priority stream.
            sub_id_to_target: dict[int, tuple[str, str]] = {}
            request_id_to_target: dict[int, tuple[str, str]] = {}
            targets = [(_KIND_PROGRAM, pid) for pid in self._programs] + [
                (_KIND_WALLET, address) for address in followed
            ]
            for request_id, (kind, target_id) in enumerate(targets, start=1):
                request_id_to_target[request_id] = (kind, target_id)
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [target_id]},
                                {"commitment": "confirmed"},
                            ],
                        }
                    )
                )
            log.info(
                "ws_connected",
                programs=list(self._programs.values()),
                followed_wallets=len(followed),
            )

            watcher = asyncio.create_task(self._watch_follow_set(ws, followed))
            try:
                while not self._stopped.is_set():
                    message = json.loads(await ws.recv())
                    if "id" in message and "result" in message:
                        target = request_id_to_target.get(message["id"])
                        if target is not None:
                            sub_id_to_target[message["result"]] = target
                        continue
                    if message.get("method") != "logsNotification":
                        continue
                    params = message.get("params", {})
                    subscription = params.get("subscription")
                    kind, target_id = sub_id_to_target.get(
                        subscription, (_KIND_PROGRAM, "unknown")
                    )
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
                        program_id=target_id if kind == _KIND_PROGRAM else "",
                        priority=kind == _KIND_WALLET,
                    )
            finally:
                watcher.cancel()

    async def _enqueue(
        self,
        signature: str,
        slot: int,
        failed: bool,
        program_id: str,
        priority: bool = False,
    ) -> None:
        program = self._programs.get(program_id, program_id) or "followed_wallet"
        metrics.WS_MESSAGES.labels(program=program).inc()
        metrics.LAST_SLOT.set(slot)
        dedup_key = f"ingest:dedup:{signature}"
        if priority:
            # A followed wallet's tx also fires the program subscription; if
            # the program copy claimed dedup first the signature sits in the
            # SAMPLED stream where it will likely be trimmed. The priority
            # copy therefore always enqueues (persistence is idempotent) and
            # claims dedup so the program copy is suppressed when possible.
            await try_dedup(
                self._redis, dedup_key, self._settings.ingest_dedup_ttl_seconds
            )
            await self._redis.xadd(
                self._settings.ingest_priority_stream_key,
                {
                    "signature": signature,
                    "slot": str(slot),
                    "failed": "1" if failed else "0",
                    "program": program,
                    "attempts": "0",
                },
                maxlen=self._settings.ingest_priority_maxlen,
                approximate=True,
            )
            metrics.SIGNATURES_ENQUEUED.inc()
            await self._redis.hset(
                "ingest:checkpoint:listener",
                mapping={"slot": str(slot), "signature": signature},
            )
            return
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
