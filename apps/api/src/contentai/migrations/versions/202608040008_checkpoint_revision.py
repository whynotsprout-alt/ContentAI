"""add the execution checkpoint revision fence

Revision ID: 202608040008
Revises: 202608040007
Create Date: 2026-08-04 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608040008"
down_revision: str | Sequence[str] | None = "202608040007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column(
            "checkpoint_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agentexecution", "checkpoint_revision")
