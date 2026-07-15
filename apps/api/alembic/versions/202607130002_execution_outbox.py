"""Add distributed execution leases and transactional outbox.

Revision ID: 202607130002
Revises: 202607130001
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607130002"
down_revision: str | None = "202607130001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column(
            "resume_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("agentexecution", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("agentexecution", sa.Column("worker_id", sa.String()))
    op.add_column(
        "agentexecution",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_agentexecution_lease_expires_at", "agentexecution", ["lease_expires_at"])
    op.create_index("ix_agentexecution_worker_id", "agentexecution", ["worker_id"])
    op.alter_column("agentexecution", "attempt_count", server_default=None)

    op.create_table(
        "executionoutbox",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(),
            sa.ForeignKey("agentexecution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False, server_default="execute"),
        sa.Column("request_id", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "execution_id",
            "kind",
            name="ux_executionoutbox_execution_kind",
        ),
    )
    op.create_index(
        "ix_executionoutbox_status_available",
        "executionoutbox",
        ["status", "available_at"],
    )
    op.create_index("ix_executionoutbox_locked_until", "executionoutbox", ["locked_until"])
    op.create_index("ix_executionoutbox_available_at", "executionoutbox", ["available_at"])
    op.create_index("ix_executionoutbox_locked_by", "executionoutbox", ["locked_by"])
    op.create_index("ix_executionoutbox_published_at", "executionoutbox", ["published_at"])
    op.create_index("ix_executionoutbox_execution_id", "executionoutbox", ["execution_id"])
    op.create_index("ix_executionoutbox_kind", "executionoutbox", ["kind"])
    op.create_index("ix_executionoutbox_request_id", "executionoutbox", ["request_id"])
    op.create_index("ix_executionoutbox_status", "executionoutbox", ["status"])


def downgrade() -> None:
    op.drop_table("executionoutbox")
    op.drop_index("ix_agentexecution_worker_id", table_name="agentexecution")
    op.drop_index("ix_agentexecution_lease_expires_at", table_name="agentexecution")
    op.drop_column("agentexecution", "attempt_count")
    op.drop_column("agentexecution", "worker_id")
    op.drop_column("agentexecution", "lease_expires_at")
    op.drop_column("agentexecution", "resume_payload")
