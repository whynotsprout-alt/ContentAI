"""Inline agent runtime schema cleanup.

Revision ID: 202607080002
Revises: 202607080001
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607080002"
down_revision: str | None = "202607080001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agentrun
        SET status = 'cancelled',
            finished_at = COALESCE(finished_at, updated_at)
        WHERE status = 'queued'
        """
    )
    op.drop_index("ux_agentrun_active_session_account", table_name="agentrun")
    op.create_index(
        "ux_agentrun_active_session_account",
        "agentrun",
        ["session_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_index("ix_agentrun_lease_owner", table_name="agentrun")
    op.drop_index("ix_agentrun_lease_expires_at", table_name="agentrun")
    op.drop_index("ix_agentrun_last_heartbeat_at", table_name="agentrun")
    op.drop_column("agentrun", "lease_owner")
    op.drop_column("agentrun", "lease_expires_at")
    op.drop_column("agentrun", "last_heartbeat_at")


def downgrade() -> None:
    op.add_column("agentrun", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("agentrun", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("agentrun", sa.Column("lease_owner", sa.String()))
    op.create_index("ix_agentrun_last_heartbeat_at", "agentrun", ["last_heartbeat_at"])
    op.create_index("ix_agentrun_lease_expires_at", "agentrun", ["lease_expires_at"])
    op.create_index("ix_agentrun_lease_owner", "agentrun", ["lease_owner"])
    op.drop_index("ux_agentrun_active_session_account", table_name="agentrun")
    op.create_index(
        "ux_agentrun_active_session_account",
        "agentrun",
        ["session_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
