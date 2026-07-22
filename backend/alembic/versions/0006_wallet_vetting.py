"""Wallet vetting: fake-wallet / wash-trading-ring verdicts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _vetting_tables():
    from app.db.models import WalletVetting

    return [WalletVetting.__table__]


def upgrade() -> None:
    from app.db.base import Base

    Base.metadata.create_all(bind=op.get_bind(), tables=_vetting_tables(), checkfirst=False)


def downgrade() -> None:
    from app.db.base import Base

    Base.metadata.drop_all(bind=op.get_bind(), tables=_vetting_tables(), checkfirst=False)
