"""Ingest-writer worker: signatures stream -> getTransaction -> parse -> DB.

Consumes the Redis stream via a consumer group (horizontally scalable),
fetches full transactions with bounded concurrency, parses them through the
registry and persists everything idempotently. New trades are published on
the ``events:trades`` pub/sub channel for live consumers (Phase 3+ decision
engine, dashboards).

Delivery model: at-least-once. Retries re-add the entry to the stream BEFORE
acking the old one, and every DB write is idempotent, so a crash anywhere
duplicates work instead of losing it; after ``ingest_max_attempts`` a
signature is dropped with a metric. Crashed consumers' pending entries are
reclaimed via XAUTOCLAIM.

Ordering model: transaction fetches run concurrently (network-bound), but
persistence is sequential in (slot, signature) order within each batch — the
position ledger folds a wallet's buys/sells in on-chain order, so same-batch
trades of one wallet must not race each other. Cross-batch order follows the
stream, which the listener feeds in notification order.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.util import aware
from app.ingestion.parsers import parse_transaction
from app.ingestion.programs import WSOL_MINT
from app.logging_config import get_logger
from app.services import metrics
from app.services.redis import SOL_PRICE_KEY, TRADES_CHANNEL, ensure_group, publish_json
from app.services.rpc import SolanaRpc

log = get_logger(__name__)

REQUEUE_DELAY_SECONDS = 2.0
AUTOCLAIM_INTERVAL_SECONDS = 30.0
AUTOCLAIM_MIN_IDLE_MS = 60_000


def _slot_order(item: tuple[str, dict, dict | None]) -> tuple[int, str]:
    """Sort key: on-chain slot first, signature as a deterministic tiebreak."""
    _entry_id, fields, tx = item
    slot = int(tx.get("slot", 0)) if tx else int(fields.get("slot") or 0)
    return slot, fields.get("signature", "")


class IngestWriter:
    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        rpc: SolanaRpc,
        session_factory: async_sessionmaker[AsyncSession],
        consumer_name: str = "writer-1",
    ):
        self._settings = settings
        self._redis = redis
        self._rpc = rpc
        self._session_factory = session_factory
        self._consumer = consumer_name
        self._stopped = asyncio.Event()
        self._fetch_semaphore = asyncio.Semaphore(settings.ingest_fetch_concurrency)
        self._last_autoclaim = 0.0

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        stream, group = self._settings.ingest_stream_key, self._settings.ingest_group
        priority = self._settings.ingest_priority_stream_key
        await ensure_group(self._redis, stream, group)
        await ensure_group(self._redis, priority, group)
        log.info("ingest_writer_started", stream=stream, group=group, consumer=self._consumer)
        while not self._stopped.is_set():
            try:
                await self._autoclaim_if_due()
                # Priority lane first: followed wallets' signatures must be
                # fetched NOW (they gate live copy decisions), never queued
                # behind the sampled firehose. Non-blocking peek; fall back
                # to the main stream only when the lane is empty.
                entries = await self._redis.xreadgroup(
                    group,
                    self._consumer,
                    {priority: ">"},
                    count=self._settings.ingest_batch_size,
                    block=0,
                )
                active_stream, budget_exempt = priority, True
                if not entries or not entries[0][1]:
                    entries = await self._redis.xreadgroup(
                        group,
                        self._consumer,
                        {stream: ">"},
                        count=self._settings.ingest_batch_size,
                        block=5000,
                    )
                    active_stream, budget_exempt = stream, False
                if not entries:
                    continue
                messages = entries[0][1]
                if not messages:
                    continue
                fetched = await asyncio.gather(
                    *(
                        self._fetch(entry_id, fields, budget_exempt=budget_exempt)
                        for entry_id, fields in messages
                    )
                )
                # Persist sequentially, oldest slot first (see module
                # docstring); retries requeue concurrently afterwards so
                # their pacing delay never stalls the write path.
                sol_price = await self._sol_price()
                retries: list[tuple[str, dict]] = []
                for entry_id, fields, tx in sorted(fetched, key=_slot_order):
                    if tx is None or not await self._persist_and_ack(
                        entry_id, fields, tx, sol_price, stream=active_stream
                    ):
                        retries.append((entry_id, fields))
                if retries:
                    await asyncio.gather(
                        *(
                            self._requeue_or_drop(entry_id, fields, stream=active_stream)
                            for entry_id, fields in retries
                        )
                    )
                try:
                    metrics.QUEUE_DEPTH.set(await self._redis.xlen(stream))
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest_writer_loop_error")
                await asyncio.sleep(1)

    async def _autoclaim_if_due(self) -> None:
        loop_time = asyncio.get_running_loop().time()
        if loop_time - self._last_autoclaim < AUTOCLAIM_INTERVAL_SECONDS:
            return
        self._last_autoclaim = loop_time
        for stream in (
            self._settings.ingest_priority_stream_key,
            self._settings.ingest_stream_key,
        ):
            try:
                await self._redis.xautoclaim(
                    stream,
                    self._settings.ingest_group,
                    self._consumer,
                    min_idle_time=AUTOCLAIM_MIN_IDLE_MS,
                    count=self._settings.ingest_batch_size,
                )
            except Exception as exc:
                log.warning("xautoclaim_failed", stream=stream, error=str(exc))

    async def _fetch(
        self, entry_id: str, fields: dict, budget_exempt: bool = False
    ) -> tuple[str, dict, dict | None]:
        """Concurrent phase: fetch the transaction; never touches the DB."""
        signature = fields.get("signature", "")
        try:
            async with self._fetch_semaphore:
                tx = await self._rpc.get_transaction(
                    signature, budget_exempt=budget_exempt or None
                )
        except Exception as exc:
            log.warning("tx_fetch_error", signature=signature, error=str(exc))
            metrics.TX_FETCHED.labels(status="error").inc()
            return entry_id, fields, None
        metrics.TX_FETCHED.labels(status="ok" if tx is not None else "not_found").inc()
        return entry_id, fields, tx

    async def _persist_and_ack(
        self,
        entry_id: str,
        fields: dict,
        tx: dict,
        sol_price: Decimal | None,
        stream: str | None = None,
    ) -> bool:
        """Sequential phase: persist one transaction; True when acked."""
        stream = stream or self._settings.ingest_stream_key
        group = self._settings.ingest_group
        try:
            await self._persist(tx, sol_price)
        except Exception:
            metrics.DB_WRITE_ERRORS.inc()
            log.exception("tx_persist_error", signature=fields.get("signature"))
            return False
        await self._redis.xack(stream, group, entry_id)
        return True

    async def _requeue_or_drop(
        self, entry_id: str, fields: dict, stream: str | None = None
    ) -> None:
        """Re-add first, ack second: a crash in between duplicates (safe, the
        writes are idempotent) instead of silently dropping the signature."""
        stream = stream or self._settings.ingest_stream_key
        group = self._settings.ingest_group
        attempts = int(fields.get("attempts", "0"))
        if attempts + 1 >= self._settings.ingest_max_attempts:
            log.warning("signature_dropped", signature=fields.get("signature"), attempts=attempts + 1)
            metrics.TX_FETCHED.labels(status="dropped").inc()
            await self._redis.xack(stream, group, entry_id)
            return
        # Pace retries so a not-yet-indexed transaction is not burned through
        # all its attempts before the RPC node catches up.
        await asyncio.sleep(REQUEUE_DELAY_SECONDS)
        maxlen = (
            self._settings.ingest_priority_maxlen
            if stream == self._settings.ingest_priority_stream_key
            else self._settings.ingest_stream_maxlen
        )
        await self._redis.xadd(
            stream,
            {**fields, "attempts": str(attempts + 1)},
            maxlen=maxlen,
            approximate=True,
        )
        await self._redis.xack(stream, group, entry_id)

    async def _sol_price(self) -> Decimal | None:
        try:
            value = await self._redis.get(SOL_PRICE_KEY)
            return Decimal(value) if value else None
        except Exception:
            return None

    async def _persist(self, tx: dict, sol_price: Decimal | None) -> None:
        from app.ingestion import writer  # local import to keep worker deps explicit

        events = parse_transaction(tx)
        async with self._session_factory() as session:
            async with session.begin():
                new_events = await writer.persist_parsed_transaction(
                    session,
                    tx,
                    events,
                    sol_price_usd=sol_price,
                    store_raw=self._settings.ingest_store_raw,
                )
        for event in new_events:
            metrics.TRADES_WRITTEN.labels(dex=event.dex.value).inc()
            await publish_json(
                self._redis,
                TRADES_CHANNEL,
                {
                    "signature": event.signature,
                    "event_index": event.event_index,
                    # Both names: the copytrader consumes "wallet", the
                    # dashboard's live rows render REST's "wallet_address".
                    "wallet": event.wallet,
                    "wallet_address": event.wallet,
                    "token_mint": event.token_mint,
                    "side": event.side.value,
                    "dex": event.dex.value,
                    "aggregator": event.aggregator,
                    "token_amount": event.token_amount,
                    "quote_amount": event.quote_amount,
                    "quote_mint": event.quote_mint,
                    "price_quote_per_token": event.price_quote_per_token,
                    "price_usd": (
                        event.price_quote_per_token * sol_price
                        if event.price_quote_per_token is not None
                        and sol_price is not None
                        and event.quote_mint == WSOL_MINT
                        else None
                    ),
                    # Explicit UTC: a bare isoformat() on a naive datetime
                    # parses as LOCAL time in the browser's Date().
                    "block_time": aware(event.block_time).isoformat(),
                    "slot": event.slot,
                },
            )
