"""Token rug-risk schema: assessments, learned weights, token authorities.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TOKEN_COLUMNS = ("mint_authority", "freeze_authority", "security_checked_at")


def _risk_tables():
    from app.db.models import RiskWeightSnapshot, TokenRiskAssessment

    return [TokenRiskAssessment.__table__, RiskWeightSnapshot.__table__]


def _existing_token_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("tokens")}


def upgrade() -> None:
    from app.db.base import Base

    Base.metadata.create_all(bind=op.get_bind(), tables=_risk_tables(), checkfirst=False)
    # Migration 0001 creates phase-1 tables from CURRENT model metadata, so a
    # fresh install already has these columns; only pre-existing databases
    # need the ALTERs.
    existing = _existing_token_columns()
    if "mint_authority" not in existing:
        op.add_column("tokens", sa.Column("mint_authority", sa.String(64), nullable=True))
    if "freeze_authority" not in existing:
        op.add_column("tokens", sa.Column("freeze_authority", sa.String(64), nullable=True))
    if "security_checked_at" not in existing:
        op.add_column(
            "tokens",
            sa.Column("security_checked_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    from app.db.base import Base

    existing = _existing_token_columns()
    for column in TOKEN_COLUMNS:
        if column in existing:
            op.drop_column("tokens", column)
    Base.metadata.drop_all(bind=op.get_bind(), tables=_risk_tables(), checkfirst=False)
