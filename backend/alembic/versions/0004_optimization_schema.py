"""Phase 4 optimization schema: prediction outcomes, regimes, reports.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _phase4_tables():
    from app.db.models import (
        MarketRegime,
        ModelPerformance,
        PredictionOutcome,
        RegimeStrategyStat,
        Report,
    )

    return [
        PredictionOutcome.__table__,
        ModelPerformance.__table__,
        MarketRegime.__table__,
        RegimeStrategyStat.__table__,
        Report.__table__,
    ]


def upgrade() -> None:
    from app.db.base import Base

    Base.metadata.create_all(bind=op.get_bind(), tables=_phase4_tables(), checkfirst=False)


def downgrade() -> None:
    from app.db.base import Base

    Base.metadata.drop_all(bind=op.get_bind(), tables=_phase4_tables(), checkfirst=False)
