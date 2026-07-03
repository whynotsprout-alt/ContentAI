"""Initial PostgreSQL schema.

Revision ID: 202607020001
Revises:
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607020001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("positioning", sa.String(), nullable=False),
        sa.Column("topic_scoring_prompt", sa.String(), nullable=False),
        sa.Column("content_creation_prompt", sa.String(), nullable=False),
        sa.Column("hotspot_sources", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_account_name", "account", ["name"])

    op.create_table(
        "chatsession",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "agentrun",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("chatsession.id"), nullable=False),
        sa.Column("account_id", sa.String(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("user_message_id", sa.String(), nullable=True),
        sa.Column("user_message", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentrun_account_id", "agentrun", ["account_id"])
    op.create_index("ix_agentrun_cancel_requested_at", "agentrun", ["cancel_requested_at"])
    op.create_index("ix_agentrun_last_heartbeat_at", "agentrun", ["last_heartbeat_at"])
    op.create_index("ix_agentrun_lease_expires_at", "agentrun", ["lease_expires_at"])
    op.create_index("ix_agentrun_lease_owner", "agentrun", ["lease_owner"])
    op.create_index("ix_agentrun_session_id", "agentrun", ["session_id"])
    op.create_index("ix_agentrun_status", "agentrun", ["status"])
    op.create_index(
        "ux_agentrun_active_session_account",
        "agentrun",
        ["session_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "chatmessage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("chatsession.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("message_metadata", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("agentrun.id"), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("parent_message_id", sa.String(), sa.ForeignKey("chatmessage.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chatmessage_parent_message_id", "chatmessage", ["parent_message_id"])
    op.create_index("ix_chatmessage_run_id", "chatmessage", ["run_id"])
    op.create_index("ix_chatmessage_session_id", "chatmessage", ["session_id"])
    op.create_index("ix_chatmessage_tool_call_id", "chatmessage", ["tool_call_id"])
    op.create_index("ix_chatmessage_tool_name", "chatmessage", ["tool_name"])

    op.create_table(
        "agentrunevent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("agentrun.id"), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentrunevent_run_id", "agentrunevent", ["run_id"])

    op.create_table(
        "memoryrecord",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("memory_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("namespace", "memory_key", name="ux_memoryrecord_namespace_key"),
    )
    op.create_index("ix_memoryrecord_content", "memoryrecord", ["content"])
    op.create_index("ix_memoryrecord_kind", "memoryrecord", ["kind"])
    op.create_index("ix_memoryrecord_memory_key", "memoryrecord", ["memory_key"])
    op.create_index("ix_memoryrecord_namespace", "memoryrecord", ["namespace"])


def downgrade() -> None:
    op.drop_index("ix_memoryrecord_namespace", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_memory_key", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_kind", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_content", table_name="memoryrecord")
    op.drop_table("memoryrecord")
    op.drop_index("ix_agentrunevent_run_id", table_name="agentrunevent")
    op.drop_table("agentrunevent")
    op.drop_index("ix_chatmessage_tool_name", table_name="chatmessage")
    op.drop_index("ix_chatmessage_tool_call_id", table_name="chatmessage")
    op.drop_index("ix_chatmessage_session_id", table_name="chatmessage")
    op.drop_index("ix_chatmessage_run_id", table_name="chatmessage")
    op.drop_index("ix_chatmessage_parent_message_id", table_name="chatmessage")
    op.drop_table("chatmessage")
    op.drop_index("ux_agentrun_active_session_account", table_name="agentrun")
    op.drop_index("ix_agentrun_status", table_name="agentrun")
    op.drop_index("ix_agentrun_session_id", table_name="agentrun")
    op.drop_index("ix_agentrun_lease_owner", table_name="agentrun")
    op.drop_index("ix_agentrun_lease_expires_at", table_name="agentrun")
    op.drop_index("ix_agentrun_last_heartbeat_at", table_name="agentrun")
    op.drop_index("ix_agentrun_cancel_requested_at", table_name="agentrun")
    op.drop_index("ix_agentrun_account_id", table_name="agentrun")
    op.drop_table("agentrun")
    op.drop_table("chatsession")
    op.drop_index("ix_account_name", table_name="account")
    op.drop_table("account")
