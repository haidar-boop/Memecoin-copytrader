"""FastAPI dependencies backed by app.state resources."""

from __future__ import annotations

from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis
