"""Small shared DB helpers used across ingestion, enrichment, and analytics."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


def aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (SQLite round trips drop tzinfo) to UTC-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def sql_cutoff(session: AsyncSession, dt: datetime) -> datetime:
    """A time bound suitable for SQL comparison on this session's dialect.

    SQLite stores DateTime columns as naive ISO strings, so an aware bound
    would compare against a differently-formatted literal; PostgreSQL wants
    the aware value. Normalizing here lets window predicates live in SQL
    (bounded scans) instead of Python post-filtering.
    """
    dt = aware(dt)
    if session.get_bind().dialect.name == "sqlite":
        return dt.replace(tzinfo=None)
    return dt


def to_decimal(value: Any) -> Decimal | None:
    """Driver/Redis output -> finite Decimal, else None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


_LAMPORT_QUANTUM = Decimal("0.000000001")


def quantize_sol(value: Decimal) -> Decimal:
    """Round a SOL amount to lamport precision (9 dp).

    Chain amounts are exact in lamports; anything beyond 9 decimals is float
    round-trip noise (SQLite stores NUMERIC as float in the test suite).
    """
    quantized = value.quantize(_LAMPORT_QUANTUM).normalize()
    # normalize() can flip large integers into scientific notation (1E+2);
    # rescale those back to plain integers.
    return quantized.quantize(Decimal(1)) if quantized.as_tuple().exponent > 0 else quantized


def to_float(value: Any) -> float | None:
    """Driver output -> finite float, else None."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


async def bulk_append(
    session: AsyncSession,
    model: type,
    rows: list[dict],
    *,
    ignore_conflicts: bool = False,
) -> None:
    """Append-only executemany INSERT.

    Uses Core INSERT instead of ORM ``add_all``: the ORM's RETURNING-based
    sentinel matching cannot handle SQLite round-tripping naive datetimes in
    composite time-keyed primary keys. ``ignore_conflicts`` makes replays of
    the same cycle idempotent instead of aborting the batch.
    """
    if not rows:
        return
    if not ignore_conflicts:
        await session.execute(insert(model), rows)
        return
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(pg_insert(model).on_conflict_do_nothing(), rows)
    else:
        await session.execute(insert(model).prefix_with("OR IGNORE"), rows)
