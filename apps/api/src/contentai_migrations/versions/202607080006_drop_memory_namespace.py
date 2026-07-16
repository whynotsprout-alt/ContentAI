"""Drop legacy memory namespace column.

Revision ID: 202607080006
Revises: 202607080005
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607080006"
down_revision: str | None = "202607080005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_memoryrecord_namespace", table_name="memoryrecord")
    op.drop_column("memoryrecord", "namespace")


def downgrade() -> None:
    op.add_column(
        "memoryrecord",
        sa.Column("namespace", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_memoryrecord_namespace", "memoryrecord", ["namespace"])
    op.alter_column("memoryrecord", "namespace", server_default=None)
