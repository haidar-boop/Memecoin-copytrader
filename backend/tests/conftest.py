"""Shared test fixtures.

- ``load_tx``: loads a recorded/crafted getTransaction JSON fixture from
  tests/fixtures/<name>.json.
- ``db_session``: async SQLAlchemy session on in-memory SQLite with the full
  schema created. Unit tests exercise logic, not PostgreSQL/Timescale
  features; integration against real infra runs via docker compose.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401  (register all tables on Base.metadata)
from app.db.base import Base

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_tx_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def load_tx() -> Callable[[str], dict]:
    return load_tx_fixture


class StubRedis:
    """Minimal in-memory async Redis stand-in for decision/execution tests."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.data.pop(key, None) is not None else 0
        return removed

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    async def incrby(self, key: str, amount: int) -> int:
        value = int(self.data.get(key, "0")) + int(amount)
        self.data[key] = str(value)
        return value

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def lpush(self, key: str, *values: str) -> int:
        bucket = self.lists.setdefault(key, [])
        for value in values:
            bucket.insert(0, value)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        bucket = self.lists.get(key, [])
        self.lists[key] = bucket[start : stop + 1]
        return True


@pytest.fixture
def stub_redis() -> StubRedis:
    return StubRedis()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
