"""Drop memory content btree index.

Revision ID: 202607080001
Revises: 202607030001
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "202607080001"
down_revision: str | None = "202607030001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_memoryrecord_content", table_name="memoryrecord")


def downgrade() -> None:
    op.create_index("ix_memoryrecord_content", "memoryrecord", ["content"])
