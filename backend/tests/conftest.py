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


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
