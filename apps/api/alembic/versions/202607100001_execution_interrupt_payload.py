"""Persist execution interrupt payloads for reconnectable resumes.

Revision ID: 202607100001
Revises: 202607090003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607100001"
down_revision: str | None = "202607090003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column(
            "interrupt_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("agentexecution", "interrupt_payload", server_default=None)


def downgrade() -> None:
    op.drop_column("agentexecution", "interrupt_payload")
