"""Initial Phase 1 schema: core tables, hypertables, time-series indexes.

Revision ID: 0001
Revises:
Create Date: 2026-07-20

Creates every table from app.db.models, then promotes the append-only event
and snapshot tables to TimescaleDB hypertables (1-day chunks). When the
timescaledb extension is unavailable (plain PostgreSQL), the schema still
works — BRIN indexes keep time scans cheap — so dev environments don't need
Timescale.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

BRIN_TABLES = [
    ("transactions", "block_time"),
    ("trades", "block_time"),
    ("failed_transactions", "block_time"),
    ("token_snapshots", "ts"),
]

# DO-guarded so the same DDL works online, in `alembic upgrade --sql` offline
# mode, and on plain PostgreSQL without the timescaledb extension installed.
_ENABLE_TIMESCALE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
    END IF;
END $$;
"""

_HYPERTABLE_TEMPLATE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('{table}', '{column}',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
END $$;
"""


def upgrade() -> None:
    from app.db.base import Base
    from app.db.models import HYPERTABLES  # noqa: F401  (imports register tables)

    # In offline (--sql) mode this is Alembic's MockConnection: it renders DDL
    # instead of executing it, and still knows its dialect. Nothing here may
    # inspect query *results*, which is why the Timescale probing lives in
    # DO-guarded SQL rather than Python.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    if bind.dialect.name != "postgresql":
        return

    op.execute(_ENABLE_TIMESCALE)
    for table, time_column in HYPERTABLES:
        op.execute(_HYPERTABLE_TEMPLATE.format(table=table, column=time_column))

    for table, time_column in BRIN_TABLES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS brin_{table}_{time_column} "
            f"ON {table} USING BRIN ({time_column})"
        )


def downgrade() -> None:
    from app.db.base import Base
    from app.db import models  # noqa: F401

    Base.metadata.drop_all(bind=op.get_bind())
