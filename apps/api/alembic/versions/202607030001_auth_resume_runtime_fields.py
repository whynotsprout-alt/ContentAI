"""Add auth and resume fields.

Revision ID: 202607030001
Revises: 202607020001
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607030001"
down_revision: str | None = "202607020001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chatsession",
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="local"),
    )
    op.add_column(
        "chatsession",
        sa.Column("owner_user_id", sa.String(), nullable=False, server_default="local-user"),
    )
    op.create_index("ix_chatsession_tenant_id", "chatsession", ["tenant_id"])
    op.create_index("ix_chatsession_owner_user_id", "chatsession", ["owner_user_id"])
    op.create_index(
        "ix_chatsession_tenant_owner",
        "chatsession",
        ["tenant_id", "owner_user_id"],
    )

    op.add_column(
        "agentrun",
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="local"),
    )
    op.add_column(
        "agentrun",
        sa.Column("created_by_user_id", sa.String(), nullable=False, server_default="local-user"),
    )
    op.add_column(
        "agentrun",
        sa.Column("resume_value", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "agentrun",
        sa.Column("interrupt_payload", sa.String(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "agentrun",
        sa.Column("tool_permissions", sa.String(), nullable=False, server_default='["*"]'),
    )
    op.create_index("ix_agentrun_tenant_id", "agentrun", ["tenant_id"])
    op.create_index("ix_agentrun_created_by_user_id", "agentrun", ["created_by_user_id"])
    op.create_index(
        "ix_agentrun_tenant_user",
        "agentrun",
        ["tenant_id", "created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agentrun_tenant_user", table_name="agentrun")
    op.drop_index("ix_agentrun_created_by_user_id", table_name="agentrun")
    op.drop_index("ix_agentrun_tenant_id", table_name="agentrun")
    op.drop_column("agentrun", "tool_permissions")
    op.drop_column("agentrun", "interrupt_payload")
    op.drop_column("agentrun", "resume_value")
    op.drop_column("agentrun", "created_by_user_id")
    op.drop_column("agentrun", "tenant_id")

    op.drop_index("ix_chatsession_tenant_owner", table_name="chatsession")
    op.drop_index("ix_chatsession_owner_user_id", table_name="chatsession")
    op.drop_index("ix_chatsession_tenant_id", table_name="chatsession")
    op.drop_column("chatsession", "owner_user_id")
    op.drop_column("chatsession", "tenant_id")
