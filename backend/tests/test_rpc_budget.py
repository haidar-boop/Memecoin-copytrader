"""Tests for the shared daily RPC credit budget."""

from __future__ import annotations

import pytest

from app.services.rpc import BUDGET_EXEMPT_METHODS, RpcBudget


class FakeRedis:
    """Minimal async Redis stand-in for INCR/EXPIRE."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self.fail = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


async def test_zero_limit_never_touches_redis(fake_redis: FakeRedis) -> None:
    budget = RpcBudget(fake_redis, 0)
    await budget.acquire()
    assert fake_redis.counters == {}


async def test_under_limit_passes_and_sets_ttl(fake_redis: FakeRedis) -> None:
    budget = RpcBudget(fake_redis, 5)
    for _ in range(5):
        await budget.acquire()
    (key,) = fake_redis.counters
    assert key.startswith(RpcBudget.KEY_PREFIX)
    assert fake_redis.counters[key] == 5
    assert fake_redis.expires[key] == RpcBudget.KEY_TTL_SECONDS


async def test_over_limit_blocks_until_budget_frees(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = RpcBudget(fake_redis, 2)
    await budget.acquire()
    await budget.acquire()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Simulate the UTC day rolling over: fresh counter.
        fake_redis.counters.clear()

    monkeypatch.setattr("app.services.rpc.asyncio.sleep", fake_sleep)
    await budget.acquire()
    assert sleeps == [RpcBudget.POLL_SECONDS]


async def test_over_limit_exempt_never_blocks(fake_redis: FakeRedis) -> None:
    budget = RpcBudget(fake_redis, 1)
    await budget.acquire()
    # Would block if not exempt; must return immediately (still counted).
    await budget.acquire(exempt=True)
    (key,) = fake_redis.counters
    assert fake_redis.counters[key] == 2


async def test_redis_failure_fails_open(fake_redis: FakeRedis) -> None:
    budget = RpcBudget(fake_redis, 1)
    fake_redis.fail = True
    await budget.acquire()
    await budget.acquire()


def test_execution_critical_methods_are_exempt() -> None:
    assert "sendTransaction" in BUDGET_EXEMPT_METHODS
    assert "simulateTransaction" in BUDGET_EXEMPT_METHODS
    assert "getTransaction" not in BUDGET_EXEMPT_METHODS


class FakeNotifyRedis(FakeRedis):
    """FakeRedis + the notification/list surface the exhaustion alert uses."""

    def __init__(self) -> None:
        super().__init__()
        self.kv: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.lists: dict[str, list[str]] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def lpush(self, key: str, *values: str) -> int:
        bucket = self.lists.setdefault(key, [])
        for value in values:
            bucket.insert(0, value)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        return True


async def test_exhaustion_notifies_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeNotifyRedis()
    budget = RpcBudget(redis, 1)

    async def fake_sleep(seconds: float) -> None:
        redis.counters.clear()  # day rolls over, loop exits

    monkeypatch.setattr("app.services.rpc.asyncio.sleep", fake_sleep)
    await budget.acquire()
    await budget.acquire()  # first over-limit call -> notify + block + resume
    assert len(redis.published) == 1
    redis.counters[next(iter(redis.kv), "x")] = 0  # keep counter state simple
    # A second exhaustion the same day must NOT notify again (NX marker).
    for _ in range(2):
        await budget.acquire()
    assert len(redis.published) == 1


async def test_solana_rpc_routes_exempt_calls_to_priority_budget(monkeypatch):
    """The regression this guards: budget_exempt=True must draw from its OWN
    capped budget, never bypass budgeting entirely — an exempt call after the
    MAIN budget is exhausted must still count (and can itself be capped),
    so total daily spend stays bounded by main_limit + priority_limit."""
    from app.services.rpc import SolanaRpc

    main_redis = FakeRedis()
    priority_redis = FakeRedis()
    main_budget = RpcBudget(main_redis, daily_limit=5)
    priority_budget = RpcBudget(
        priority_redis, daily_limit=3, key_prefix=RpcBudget.PRIORITY_KEY_PREFIX
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": "ok"}

        def raise_for_status(self):
            pass

    class FakeClient:
        async def post(self, url, json):
            return FakeResponse()

        async def aclose(self):
            pass

    rpc = SolanaRpc(
        "http://test", budget=main_budget, priority_budget=priority_budget
    )
    rpc._client = FakeClient()
    rpc._limiter._interval = 0.0

    # Exhaust the MAIN budget with non-exempt calls.
    for _ in range(5):
        await rpc.call("getTransaction", budget_exempt=False)
    assert main_redis.counters
    main_key = next(iter(main_redis.counters))
    assert main_redis.counters[main_key] == 5

    # Exempt calls must NOT touch the main counter at all.
    await rpc.call("sendTransaction", budget_exempt=True)
    assert main_redis.counters[main_key] == 5  # unchanged
    priority_key = next(iter(priority_redis.counters))
    assert priority_redis.counters[priority_key] == 1


async def test_solana_rpc_exempt_without_priority_budget_is_unbounded():
    """Documented legacy fallback: no priority_budget configured -> exempt
    calls skip budgeting entirely (opt-out, not the default wiring)."""
    from app.services.rpc import SolanaRpc

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": "ok"}

        def raise_for_status(self):
            pass

    class FakeClient:
        async def post(self, url, json):
            return FakeResponse()

        async def aclose(self):
            pass

    main_redis = FakeRedis()
    rpc = SolanaRpc(
        "http://test", budget=RpcBudget(main_redis, daily_limit=1), priority_budget=None
    )
    rpc._client = FakeClient()
    rpc._limiter._interval = 0.0
    for _ in range(10):
        await rpc.call("sendTransaction", budget_exempt=True)
    assert main_redis.counters == {}  # never touched
