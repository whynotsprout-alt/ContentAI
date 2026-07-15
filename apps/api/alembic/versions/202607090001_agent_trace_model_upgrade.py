"""Upgrade agent trace data model.

Revision ID: 202607090001
Revises: 202607080006
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607090001"
down_revision: str | None = "202607080006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chatsession",
        sa.Column("title_source", sa.String(), nullable=False, server_default="default"),
    )
    op.add_column(
        "chatsession",
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
    )
    op.add_column("chatsession", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_chatsession_title_source", "chatsession", ["title_source"])
    op.create_index("ix_chatsession_status", "chatsession", ["status"])
    op.create_index("ix_chatsession_pinned_at", "chatsession", ["pinned_at"])
    op.alter_column("chatsession", "title_source", server_default=None)
    op.alter_column("chatsession", "status", server_default=None)

    op.add_column("agentexecution", sa.Column("trace_id", sa.String(), nullable=True))
    op.execute("UPDATE agentexecution SET trace_id = id WHERE trace_id IS NULL")
    op.alter_column("agentexecution", "trace_id", nullable=False)
    op.execute("UPDATE agentexecution SET status = 'waiting_input' WHERE status = 'interrupted'")
    op.drop_index("ix_agentexecution_checkpoint_id", table_name="agentexecution")
    op.alter_column("agentexecution", "checkpoint_id", new_column_name="latest_checkpoint_id")
    op.create_index("ix_agentexecution_trace_id", "agentexecution", ["trace_id"])
    op.create_index(
        "ix_agentexecution_latest_checkpoint_id",
        "agentexecution",
        ["latest_checkpoint_id"],
    )
    op.create_index(
        "ix_agentexecution_invocation_status",
        "agentexecution",
        ["invocation_id", "status"],
    )

    op.add_column("chatmessage", sa.Column("parent_message_id", sa.String(), nullable=True))
    op.add_column(
        "chatmessage",
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_chatmessage_parent_message_id_chatmessage",
        "chatmessage",
        "chatmessage",
        ["parent_message_id"],
        ["id"],
    )
    op.create_index("ix_chatmessage_parent_message_id", "chatmessage", ["parent_message_id"])
    op.drop_index("ix_chatmessage_session_created", table_name="chatmessage")
    op.create_index(
        "ix_chatmessage_session_created_id",
        "chatmessage",
        ["session_id", "created_at", "id"],
    )
    op.alter_column("chatmessage", "payload", server_default=None)

    op.add_column(
        "toolexecution",
        sa.Column("tool_version", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "toolexecution",
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("toolexecution", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.create_index("ix_toolexecution_sequence", "toolexecution", ["sequence"])
    op.create_index("ix_toolexecution_duration_ms", "toolexecution", ["duration_ms"])
    op.create_index(
        "ix_toolexecution_execution_sequence",
        "toolexecution",
        ["execution_id", "sequence"],
    )
    op.alter_column("toolexecution", "tool_version", server_default=None)
    op.alter_column("toolexecution", "sequence", server_default=None)

    op.create_table(
        "agentevent",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("execution_id", sa.String(), sa.ForeignKey("agentexecution.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agentevent_execution_id", "agentevent", ["execution_id"])
    op.create_index("ix_agentevent_event_type", "agentevent", ["event_type"])
    op.create_index("ix_agentevent_sequence", "agentevent", ["sequence"])
    op.create_index(
        "ix_agentevent_execution_created",
        "agentevent",
        ["execution_id", "created_at"],
    )
    op.create_index(
        "ix_agentevent_execution_sequence",
        "agentevent",
        ["execution_id", "sequence"],
    )
    op.create_index("ix_agentevent_type_created", "agentevent", ["event_type", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agentevent_type_created", table_name="agentevent")
    op.drop_index("ix_agentevent_execution_sequence", table_name="agentevent")
    op.drop_index("ix_agentevent_execution_created", table_name="agentevent")
    op.drop_index("ix_agentevent_sequence", table_name="agentevent")
    op.drop_index("ix_agentevent_event_type", table_name="agentevent")
    op.drop_index("ix_agentevent_execution_id", table_name="agentevent")
    op.drop_table("agentevent")

    op.drop_index("ix_toolexecution_execution_sequence", table_name="toolexecution")
    op.drop_index("ix_toolexecution_duration_ms", table_name="toolexecution")
    op.drop_index("ix_toolexecution_sequence", table_name="toolexecution")
    op.drop_column("toolexecution", "duration_ms")
    op.drop_column("toolexecution", "sequence")
    op.drop_column("toolexecution", "tool_version")

    op.drop_index("ix_chatmessage_session_created_id", table_name="chatmessage")
    op.create_index("ix_chatmessage_session_created", "chatmessage", ["session_id", "created_at"])
    op.drop_index("ix_chatmessage_parent_message_id", table_name="chatmessage")
    op.drop_constraint(
        "fk_chatmessage_parent_message_id_chatmessage",
        "chatmessage",
        type_="foreignkey",
    )
    op.drop_column("chatmessage", "payload")
    op.drop_column("chatmessage", "parent_message_id")

    op.drop_index("ix_agentexecution_invocation_status", table_name="agentexecution")
    op.drop_index("ix_agentexecution_latest_checkpoint_id", table_name="agentexecution")
    op.drop_index("ix_agentexecution_trace_id", table_name="agentexecution")
    op.alter_column(
        "agentexecution",
        "latest_checkpoint_id",
        new_column_name="checkpoint_id",
    )
    op.create_index("ix_agentexecution_checkpoint_id", "agentexecution", ["checkpoint_id"])
    op.execute("UPDATE agentexecution SET status = 'interrupted' WHERE status = 'waiting_input'")
    op.drop_column("agentexecution", "trace_id")

    op.drop_index("ix_chatsession_pinned_at", table_name="chatsession")
    op.drop_index("ix_chatsession_status", table_name="chatsession")
    op.drop_index("ix_chatsession_title_source", table_name="chatsession")
    op.drop_column("chatsession", "pinned_at")
    op.drop_column("chatsession", "status")
    op.drop_column("chatsession", "title_source")
