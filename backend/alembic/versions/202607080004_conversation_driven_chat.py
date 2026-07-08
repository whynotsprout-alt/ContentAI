"""Conversation-driven chat runtime schema.

Revision ID: 202607080004
Revises: 202607080003
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607080004"
down_revision: str | None = "202607080003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chatsession", sa.Column("account_id", sa.String(), nullable=True))
    op.add_column("chatsession", sa.Column("langgraph_thread_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE chatsession
        SET account_id = COALESCE(
                (
                    SELECT account_id
                    FROM agentrun
                    WHERE agentrun.session_id = chatsession.id
                    ORDER BY agentrun.updated_at DESC
                    LIMIT 1
                ),
                (
                    SELECT id
                    FROM account
                    ORDER BY created_at ASC
                    LIMIT 1
                )
            ),
            langgraph_thread_id = id
        """
    )
    op.execute("DELETE FROM chatsession WHERE account_id IS NULL")
    op.alter_column("chatsession", "account_id", nullable=False)
    op.alter_column("chatsession", "langgraph_thread_id", nullable=False)
    op.alter_column("chatsession", "tenant_id", server_default=None)
    op.alter_column("chatsession", "owner_user_id", server_default=None)
    op.create_index("ix_chatsession_account_id", "chatsession", ["account_id"])
    op.create_index("ix_chatsession_langgraph_thread_id", "chatsession", ["langgraph_thread_id"])
    op.create_index(
        "ix_chatsession_tenant_owner_updated",
        "chatsession",
        ["tenant_id", "owner_user_id", "updated_at"],
    )
    op.create_index(
        "ix_chatsession_account_updated",
        "chatsession",
        ["account_id", "updated_at"],
    )
    op.create_foreign_key(
        "fk_chatsession_account_id_account",
        "chatsession",
        "account",
        ["account_id"],
        ["id"],
    )

    op.create_table(
        "agentinvocation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("chatsession.id"), nullable=False),
        sa.Column("account_id", sa.String(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("user_message_id", sa.String(), sa.ForeignKey("chatmessage.id"), nullable=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentinvocation_session_id", "agentinvocation", ["session_id"])
    op.create_index("ix_agentinvocation_account_id", "agentinvocation", ["account_id"])
    op.create_index("ix_agentinvocation_user_message_id", "agentinvocation", ["user_message_id"])
    op.create_index("ix_agentinvocation_tenant_id", "agentinvocation", ["tenant_id"])
    op.create_index(
        "ix_agentinvocation_created_by_user_id",
        "agentinvocation",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_agentinvocation_session_created",
        "agentinvocation",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_agentinvocation_tenant_user_created",
        "agentinvocation",
        ["tenant_id", "created_by_user_id", "created_at"],
    )

    op.create_table(
        "agentexecution",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.String(),
            sa.ForeignKey("agentinvocation.id"),
            nullable=False,
        ),
        sa.Column("checkpoint_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentexecution_invocation_id", "agentexecution", ["invocation_id"])
    op.create_index("ix_agentexecution_checkpoint_id", "agentexecution", ["checkpoint_id"])
    op.create_index("ix_agentexecution_status", "agentexecution", ["status"])
    op.create_index(
        "ix_agentexecution_cancel_requested_at",
        "agentexecution",
        ["cancel_requested_at"],
    )
    op.create_index(
        "ix_agentexecution_invocation_created",
        "agentexecution",
        ["invocation_id", "created_at"],
    )
    op.create_index(
        "ix_agentexecution_status_updated",
        "agentexecution",
        ["status", "updated_at"],
    )

    op.drop_index("ix_chatmessage_run_id", table_name="chatmessage")
    op.drop_index("ix_chatmessage_parent_message_id", table_name="chatmessage")
    op.drop_constraint("chatmessage_run_id_fkey", "chatmessage", type_="foreignkey")
    op.drop_constraint("chatmessage_parent_message_id_fkey", "chatmessage", type_="foreignkey")
    op.add_column("chatmessage", sa.Column("invocation_id", sa.String(), nullable=True))
    op.add_column("chatmessage", sa.Column("model_name", sa.String(), nullable=True))
    op.add_column(
        "chatmessage",
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chatmessage",
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("chatmessage", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.alter_column(
        "chatmessage",
        "message_metadata",
        type_=postgresql.JSONB(),
        postgresql_using="message_metadata::jsonb",
    )
    op.create_foreign_key(
        "fk_chatmessage_invocation_id_agentinvocation",
        "chatmessage",
        "agentinvocation",
        ["invocation_id"],
        ["id"],
    )
    op.create_index("ix_chatmessage_invocation_id", "chatmessage", ["invocation_id"])
    op.create_index("ix_chatmessage_model_name", "chatmessage", ["model_name"])
    op.create_index("ix_chatmessage_session_created", "chatmessage", ["session_id", "created_at"])
    op.drop_column("chatmessage", "run_id")
    op.drop_column("chatmessage", "parent_message_id")
    op.alter_column("chatmessage", "input_tokens", server_default=None)
    op.alter_column("chatmessage", "output_tokens", server_default=None)

    op.create_table(
        "toolexecution",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(),
            sa.ForeignKey("agentexecution.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_toolexecution_execution_id", "toolexecution", ["execution_id"])
    op.create_index("ix_toolexecution_tool_name", "toolexecution", ["tool_name"])
    op.create_index("ix_toolexecution_tool_call_id", "toolexecution", ["tool_call_id"])
    op.create_index("ix_toolexecution_status", "toolexecution", ["status"])
    op.create_index(
        "ix_toolexecution_execution_created",
        "toolexecution",
        ["execution_id", "created_at"],
    )
    op.create_index(
        "ix_toolexecution_tool_created",
        "toolexecution",
        ["tool_name", "created_at"],
    )

    op.drop_index("ix_agentrunevent_run_id", table_name="agentrunevent")
    op.drop_table("agentrunevent")
    op.drop_index("ux_agentrun_active_session_account", table_name="agentrun")
    op.drop_index("ix_agentrun_tenant_user", table_name="agentrun")
    op.drop_index("ix_agentrun_created_by_user_id", table_name="agentrun")
    op.drop_index("ix_agentrun_tenant_id", table_name="agentrun")
    op.drop_index("ix_agentrun_cancel_requested_at", table_name="agentrun")
    op.drop_index("ix_agentrun_account_id", table_name="agentrun")
    op.drop_index("ix_agentrun_session_id", table_name="agentrun")
    op.drop_index("ix_agentrun_status", table_name="agentrun")
    op.drop_table("agentrun")


def downgrade() -> None:
    op.create_table(
        "agentrun",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("chatsession.id"), nullable=False),
        sa.Column("account_id", sa.String(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=False),
        sa.Column("user_message_id", sa.String(), nullable=True),
        sa.Column("user_message", sa.String(), nullable=False),
        sa.Column("resume_value", sa.String(), nullable=False, server_default=""),
        sa.Column("interrupt_payload", sa.String(), nullable=False, server_default="{}"),
        sa.Column("tool_permissions", sa.String(), nullable=False, server_default='["*"]'),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentrun_status", "agentrun", ["status"])
    op.create_index("ix_agentrun_session_id", "agentrun", ["session_id"])
    op.create_index("ix_agentrun_account_id", "agentrun", ["account_id"])
    op.create_index("ix_agentrun_cancel_requested_at", "agentrun", ["cancel_requested_at"])
    op.create_index("ix_agentrun_tenant_id", "agentrun", ["tenant_id"])
    op.create_index("ix_agentrun_created_by_user_id", "agentrun", ["created_by_user_id"])
    op.create_index("ix_agentrun_tenant_user", "agentrun", ["tenant_id", "created_by_user_id"])
    op.create_index(
        "ux_agentrun_active_session_account",
        "agentrun",
        ["session_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "agentrunevent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("agentrun.id"), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentrunevent_run_id", "agentrunevent", ["run_id"])

    op.drop_index("ix_toolexecution_tool_created", table_name="toolexecution")
    op.drop_index("ix_toolexecution_execution_created", table_name="toolexecution")
    op.drop_index("ix_toolexecution_status", table_name="toolexecution")
    op.drop_index("ix_toolexecution_tool_call_id", table_name="toolexecution")
    op.drop_index("ix_toolexecution_tool_name", table_name="toolexecution")
    op.drop_index("ix_toolexecution_execution_id", table_name="toolexecution")
    op.drop_table("toolexecution")

    op.add_column("chatmessage", sa.Column("parent_message_id", sa.String(), nullable=True))
    op.add_column("chatmessage", sa.Column("run_id", sa.String(), nullable=True))
    op.drop_index("ix_chatmessage_session_created", table_name="chatmessage")
    op.drop_index("ix_chatmessage_model_name", table_name="chatmessage")
    op.drop_index("ix_chatmessage_invocation_id", table_name="chatmessage")
    op.drop_constraint(
        "fk_chatmessage_invocation_id_agentinvocation",
        "chatmessage",
        type_="foreignkey",
    )
    op.drop_column("chatmessage", "latency_ms")
    op.drop_column("chatmessage", "output_tokens")
    op.drop_column("chatmessage", "input_tokens")
    op.drop_column("chatmessage", "model_name")
    op.drop_column("chatmessage", "invocation_id")
    op.alter_column(
        "chatmessage",
        "message_metadata",
        type_=sa.String(),
        postgresql_using="message_metadata::text",
    )
    op.create_foreign_key(
        "chatmessage_run_id_fkey",
        "chatmessage",
        "agentrun",
        ["run_id"],
        ["id"],
    )
    op.create_foreign_key(
        "chatmessage_parent_message_id_fkey",
        "chatmessage",
        "chatmessage",
        ["parent_message_id"],
        ["id"],
    )
    op.create_index("ix_chatmessage_run_id", "chatmessage", ["run_id"])
    op.create_index("ix_chatmessage_parent_message_id", "chatmessage", ["parent_message_id"])

    op.drop_index("ix_agentexecution_status_updated", table_name="agentexecution")
    op.drop_index("ix_agentexecution_invocation_created", table_name="agentexecution")
    op.drop_index("ix_agentexecution_cancel_requested_at", table_name="agentexecution")
    op.drop_index("ix_agentexecution_status", table_name="agentexecution")
    op.drop_index("ix_agentexecution_checkpoint_id", table_name="agentexecution")
    op.drop_index("ix_agentexecution_invocation_id", table_name="agentexecution")
    op.drop_table("agentexecution")
    op.drop_index("ix_agentinvocation_tenant_user_created", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_session_created", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_created_by_user_id", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_tenant_id", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_user_message_id", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_account_id", table_name="agentinvocation")
    op.drop_index("ix_agentinvocation_session_id", table_name="agentinvocation")
    op.drop_table("agentinvocation")

    op.drop_constraint("fk_chatsession_account_id_account", "chatsession", type_="foreignkey")
    op.drop_index("ix_chatsession_account_updated", table_name="chatsession")
    op.drop_index("ix_chatsession_tenant_owner_updated", table_name="chatsession")
    op.drop_index("ix_chatsession_langgraph_thread_id", table_name="chatsession")
    op.drop_index("ix_chatsession_account_id", table_name="chatsession")
    op.drop_column("chatsession", "langgraph_thread_id")
    op.drop_column("chatsession", "account_id")
    op.alter_column("chatsession", "tenant_id", server_default="local")
    op.alter_column("chatsession", "owner_user_id", server_default="local-user")
