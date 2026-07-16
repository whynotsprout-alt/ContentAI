"""Rename memory ownership from account to agent.

Revision ID: 202607130003
Revises: 202607130002
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607130003"
down_revision: str | None = "202607130002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ux_memoryrecord_active_agent_long_term", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_lookup", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_account_id", table_name="memoryrecord")

    op.alter_column("memoryrecord", "account_id", new_column_name="agent_id")

    op.create_index("ix_memoryrecord_agent_id", "memoryrecord", ["agent_id"])
    op.create_index(
        "ix_memoryrecord_lookup",
        "memoryrecord",
        ["tenant_id", "user_id", "owner_type", "agent_id", "memory_scope", "kind", "updated_at"],
    )
    op.create_index(
        "ux_memoryrecord_active_agent_long_term",
        "memoryrecord",
        ["tenant_id", "user_id", "agent_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text(
            "memory_scope = 'long_term' AND owner_type = 'agent' AND deleted_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ux_memoryrecord_active_agent_long_term", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_lookup", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_agent_id", table_name="memoryrecord")

    op.alter_column("memoryrecord", "agent_id", new_column_name="account_id")

    op.create_index("ix_memoryrecord_account_id", "memoryrecord", ["account_id"])
    op.create_index(
        "ix_memoryrecord_lookup",
        "memoryrecord",
        [
            "tenant_id",
            "user_id",
            "owner_type",
            "account_id",
            "memory_scope",
            "kind",
            "updated_at",
        ],
    )
    op.create_index(
        "ux_memoryrecord_active_agent_long_term",
        "memoryrecord",
        ["tenant_id", "user_id", "account_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text(
            "memory_scope = 'long_term' AND owner_type = 'agent' AND deleted_at IS NULL"
        ),
    )
