"""Tests for the followed-wallet priority lane (publisher + listener routing)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.follow_lane import publish_followed
from app.config import Settings
from app.db.models import Wallet, WalletStats
from app.ingestion.ws_listener import LogsListener
from app.services.redis import FOLLOWED_WALLETS_KEY, get_followed_wallets

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


class LaneStubRedis:
    """get/set + xadd/hset capture, per-stream."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.streams: dict[str, list[dict]] = {}
        self.hashes: dict[str, dict] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.data.pop(key, None) is not None else 0
        return removed

    async def xadd(self, stream: str, fields: dict, maxlen=None, approximate=True):
        self.streams.setdefault(stream, []).append(fields)
        return f"{len(self.streams[stream])}-0"

    async def hset(self, key: str, mapping: dict) -> int:
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)


def _wallet(session_objects, address: str, tracked: bool, conf: str | None):
    wallet = Wallet(
        address=address,
        first_seen_at=NOW - timedelta(days=5),
        last_seen_at=NOW,
        is_tracked=tracked,
    )
    session_objects.append(wallet)
    return wallet


async def test_publish_followed_tracks_and_caps(db_session) -> None:
    wallets = []
    starred = _wallet(wallets, "Starred111111111111111111111111111111111111", True, None)
    strong = _wallet(wallets, "Strong1111111111111111111111111111111111111", False, "80")
    weak = _wallet(wallets, "Weak11111111111111111111111111111111111111X", False, "20")
    db_session.add_all(wallets)
    await db_session.flush()
    db_session.add_all(
        [
            WalletStats(
                wallet_id=strong.id, computed_at=NOW,
                confidence_score=Decimal("80"),
            ),
            WalletStats(
                wallet_id=weak.id, computed_at=NOW,
                confidence_score=Decimal("20"),
            ),
        ]
    )
    await db_session.commit()

    redis = LaneStubRedis()
    settings = Settings(copy_min_wallet_confidence=65.0, follow_lane_max=50)
    published = await publish_followed(db_session, redis, settings)
    assert starred.address in published
    assert strong.address in published
    assert weak.address not in published
    assert sorted(published) == json.loads(redis.data[FOLLOWED_WALLETS_KEY])

    # Cap: with room for only one, the tracked wallet wins.
    capped = await publish_followed(
        db_session, redis, Settings(copy_min_wallet_confidence=65.0, follow_lane_max=1)
    )
    assert capped == [starred.address]

    # Disabled lane publishes an empty set.
    off = await publish_followed(
        db_session, redis, Settings(follow_lane_enabled=False)
    )
    assert off == [] and await get_followed_wallets(redis) == []


async def test_listener_routes_wallet_hits_to_priority_stream() -> None:
    redis = LaneStubRedis()
    settings = Settings()
    listener = LogsListener(settings, redis)

    # Program-sub notification -> main stream, dedup enforced.
    await listener._enqueue(
        signature="sig-main", slot=1, failed=False,
        program_id=next(iter(listener._programs)),
    )
    assert [f["signature"] for f in redis.streams[settings.ingest_stream_key]] == [
        "sig-main"
    ]

    # Wallet-sub notification -> priority stream, even when dedup was
    # already claimed by the program copy.
    await listener._enqueue(
        signature="sig-main", slot=2, failed=False, program_id="", priority=True
    )
    await listener._enqueue(
        signature="sig-lead", slot=3, failed=False, program_id="", priority=True
    )
    priority = [
        f["signature"] for f in redis.streams[settings.ingest_priority_stream_key]
    ]
    assert priority == ["sig-main", "sig-lead"]
    # And the priority copy claimed dedup, so a later program copy is dropped.
    before = len(redis.streams[settings.ingest_stream_key])
    await listener._enqueue(
        signature="sig-lead", slot=3, failed=False,
        program_id=next(iter(listener._programs)),
    )
    assert len(redis.streams[settings.ingest_stream_key]) == before


async def test_writer_drains_priority_first_and_budget_exempt() -> None:
    from app.ingestion.fetcher import IngestWriter

    settings = Settings(ingest_max_attempts=1)  # drop (no 2s retry sleep)
    reads: list[tuple[str, int | None]] = []
    acked: list[tuple[str, str]] = []

    class WriterStubRedis(LaneStubRedis):
        def __init__(self) -> None:
            super().__init__()
            self.served = False

        async def xgroup_create(self, *a, **k):
            return True

        async def xautoclaim(self, *a, **k):
            return None

        async def xreadgroup(self, group, consumer, streams, count, block):
            stream = next(iter(streams))
            reads.append((stream, block))
            if stream == settings.ingest_priority_stream_key:
                if not self.served:
                    self.served = True
                    return [
                        [stream, [("1-0", {"signature": "lead-sig", "attempts": "0"})]]
                    ]
                return []
            writer.stop()  # main-stream read reached -> end the loop
            return []

        async def xack(self, stream, group, entry_id):
            acked.append((stream, entry_id))
            return 1

        async def xlen(self, stream):
            return 0

    class ExemptRpc:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool | None]] = []

        async def get_transaction(self, signature, budget_exempt=None):
            self.calls.append((signature, budget_exempt))
            return None  # not found -> requeue path (drops at max_attempts=1)

    redis = WriterStubRedis()
    rpc = ExemptRpc()
    writer = IngestWriter(settings, redis, rpc, session_factory=None)
    await writer.run()

    # Priority stream polled (non-blocking) BEFORE the main stream, every
    # cycle; the main stream is only consulted when the lane is empty.
    # block MUST be None (non-blocking): block=0 means "wait forever" in
    # redis-py, which would hang on an idle priority lane and starve the
    # main stream. The main-stream read blocks up to 5000ms.
    assert reads[0] == (settings.ingest_priority_stream_key, None)
    assert (settings.ingest_stream_key, 5000) in reads
    assert reads.index((settings.ingest_priority_stream_key, None)) < reads.index(
        (settings.ingest_stream_key, 5000)
    )
    # The leader signature was fetched budget-exempt and acked on the
    # priority stream after dropping.
    assert rpc.calls == [("lead-sig", True)]
    assert acked == [(settings.ingest_priority_stream_key, "1-0")]
