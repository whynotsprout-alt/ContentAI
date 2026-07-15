"""Add memory owner and source type constraints.

Revision ID: 202607090003
Revises: 202607090002
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607090003"
down_revision: str | None = "202607090002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memoryrecord",
        sa.Column("owner_type", sa.String(), nullable=False, server_default="agent"),
    )
    op.execute(
        """
        UPDATE memoryrecord
        SET owner_type = CASE
            WHEN memory_scope = 'short_term' OR session_id IS NOT NULL THEN 'session'
            WHEN account_id IS NOT NULL THEN 'agent'
            ELSE 'user'
        END
        """
    )
    op.execute(
        """
        UPDATE memoryrecord
        SET source_type = CASE
            WHEN source_type IN ('user_message', 'turn_summary', 'summary', 'tool', 'system')
                THEN source_type
            ELSE 'manual'
        END
        """
    )

    op.drop_index("ux_memoryrecord_active_short_term", table_name="memoryrecord")
    op.drop_index("ux_memoryrecord_active_long_term", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_lookup", table_name="memoryrecord")

    op.create_index("ix_memoryrecord_owner_type", "memoryrecord", ["owner_type"])
    op.create_index("ix_memoryrecord_source_type", "memoryrecord", ["source_type"])
    op.create_index(
        "ix_memoryrecord_lookup",
        "memoryrecord",
        ["tenant_id", "user_id", "owner_type", "account_id", "memory_scope", "kind", "updated_at"],
    )
    op.create_index(
        "ux_memoryrecord_active_user_long_term",
        "memoryrecord",
        ["tenant_id", "user_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text(
            "memory_scope = 'long_term' AND owner_type = 'user' AND deleted_at IS NULL"
        ),
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
    op.create_index(
        "ux_memoryrecord_active_session",
        "memoryrecord",
        ["tenant_id", "user_id", "session_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text("owner_type = 'session' AND deleted_at IS NULL"),
    )
    op.create_check_constraint(
        "ck_memoryrecord_owner_type",
        "memoryrecord",
        "owner_type IN ('user', 'agent', 'session')",
    )
    op.create_check_constraint(
        "ck_memoryrecord_source_type",
        "memoryrecord",
        "source_type IN ('manual', 'user_message', 'turn_summary', 'summary', 'tool', 'system')",
    )
    op.alter_column("memoryrecord", "owner_type", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_memoryrecord_source_type", "memoryrecord", type_="check")
    op.drop_constraint("ck_memoryrecord_owner_type", "memoryrecord", type_="check")
    op.drop_index("ux_memoryrecord_active_session", table_name="memoryrecord")
    op.drop_index("ux_memoryrecord_active_agent_long_term", table_name="memoryrecord")
    op.drop_index("ux_memoryrecord_active_user_long_term", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_lookup", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_source_type", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_owner_type", table_name="memoryrecord")

    op.create_index(
        "ix_memoryrecord_lookup",
        "memoryrecord",
        ["tenant_id", "user_id", "account_id", "memory_scope", "kind", "updated_at"],
    )
    op.create_index(
        "ux_memoryrecord_active_long_term",
        "memoryrecord",
        ["tenant_id", "user_id", "account_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text("memory_scope = 'long_term' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ux_memoryrecord_active_short_term",
        "memoryrecord",
        ["tenant_id", "user_id", "session_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text("memory_scope = 'short_term' AND deleted_at IS NULL"),
    )
    op.drop_column("memoryrecord", "owner_type")
