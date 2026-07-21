"""Phase 3 copy-trading schema: decisions, executions, copy positions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _phase3_tables():
    from app.db.models import CopyPosition, CopyTrade, TradeDecision

    return [TradeDecision.__table__, CopyTrade.__table__, CopyPosition.__table__]


def upgrade() -> None:
    from app.db.base import Base

    Base.metadata.create_all(bind=op.get_bind(), tables=_phase3_tables(), checkfirst=False)


def downgrade() -> None:
    from app.db.base import Base

    Base.metadata.drop_all(bind=op.get_bind(), tables=_phase3_tables(), checkfirst=False)
