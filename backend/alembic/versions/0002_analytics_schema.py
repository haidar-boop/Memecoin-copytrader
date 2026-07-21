"""Phase 2 analytics schema: wallet stats, strategies, ML registry, patterns.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-21

Adds the wallet-analysis tables. ``wallet_stats_snapshots`` becomes a
TimescaleDB hypertable via the same DO-guarded SQL as 0001, so plain
PostgreSQL and offline ``--sql`` mode both keep working.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_HYPERTABLE_TEMPLATE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('{table}', '{column}',
            chunk_time_interval => INTERVAL '7 days',
            if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
END $$;
"""


def _phase2_tables():
    from app.db.models import (
        DiscoveredPattern,
        MlModel,
        Prediction,
        StrategyCluster,
        StrategyStat,
        WalletStats,
        WalletStatsSnapshot,
    )

    return [
        WalletStats.__table__,
        WalletStatsSnapshot.__table__,
        StrategyCluster.__table__,
        StrategyStat.__table__,
        MlModel.__table__,
        Prediction.__table__,
        DiscoveredPattern.__table__,
    ]


def upgrade() -> None:
    from app.db.base import Base
    from app.db.models import PHASE2_HYPERTABLES

    bind = op.get_bind()
    # checkfirst=False: emits pure DDL, which also keeps offline --sql working.
    Base.metadata.create_all(bind=bind, tables=_phase2_tables(), checkfirst=False)

    if bind.dialect.name != "postgresql":
        return
    for table, time_column in PHASE2_HYPERTABLES:
        op.execute(_HYPERTABLE_TEMPLATE.format(table=table, column=time_column))
    op.execute(
        "CREATE INDEX IF NOT EXISTS brin_wallet_stats_snapshots_ts "
        "ON wallet_stats_snapshots USING BRIN (ts)"
    )


def downgrade() -> None:
    from app.db.base import Base

    Base.metadata.drop_all(bind=op.get_bind(), tables=_phase2_tables(), checkfirst=False)
