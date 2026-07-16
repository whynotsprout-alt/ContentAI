"""Add execution observability and administrator audit records.

Revision ID: 202607130001
Revises: 202607100004
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607130001"
down_revision: str | None = "202607100004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("claimed_at", "heartbeat_at", "first_event_at", "first_token_at"):
        op.add_column("agentexecution", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        op.create_index(f"ix_agentexecution_{name}", "agentexecution", [name])

    op.create_unique_constraint(
        "ux_agentevent_execution_sequence", "agentevent", ["execution_id", "sequence"]
    )
    op.add_column(
        "modelusage", sa.Column("provider", sa.String(), nullable=False, server_default="unknown")
    )
    op.add_column("modelusage", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.add_column(
        "modelusage", sa.Column("status", sa.String(), nullable=False, server_default="completed")
    )
    op.create_index("ix_modelusage_provider", "modelusage", ["provider"])
    op.create_index("ix_modelusage_latency_ms", "modelusage", ["latency_ms"])
    op.create_index("ix_modelusage_status", "modelusage", ["status"])
    op.alter_column("modelusage", "provider", server_default=None)
    op.alter_column("modelusage", "status", server_default=None)

    op.create_table(
        "adminauditlog",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("actor_user_id", sa.String(), sa.ForeignKey("appuser.id"), nullable=False),
        sa.Column("target_user_id", sa.String(), sa.ForeignKey("appuser.id"), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False, server_default=""),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_adminauditlog_actor_created",
        "adminauditlog",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_adminauditlog_target_created",
        "adminauditlog",
        ["target_user_id", "created_at"],
    )
    op.create_index("ix_adminauditlog_action", "adminauditlog", ["action"])
    op.create_index("ix_adminauditlog_actor_user_id", "adminauditlog", ["actor_user_id"])
    op.create_index("ix_adminauditlog_target_user_id", "adminauditlog", ["target_user_id"])
    op.create_index("ix_adminauditlog_request_id", "adminauditlog", ["request_id"])
    op.create_index("ix_adminauditlog_created_at", "adminauditlog", ["created_at"])


def downgrade() -> None:
    op.drop_table("adminauditlog")
    for name in ("status", "latency_ms", "provider"):
        index = f"ix_modelusage_{name}"
        op.drop_index(index, table_name="modelusage")
        op.drop_column("modelusage", name)
    op.drop_constraint("ux_agentevent_execution_sequence", "agentevent", type_="unique")
    for name in ("first_token_at", "first_event_at", "heartbeat_at", "claimed_at"):
        op.drop_index(f"ix_agentexecution_{name}", table_name="agentexecution")
        op.drop_column("agentexecution", name)
