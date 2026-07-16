"""Persist research packages and pin sessions to an agent version.

Revision ID: 202607150001
Revises: 202607140001
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607150001"
down_revision: str | None = "202607140001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chatsession",
        sa.Column("agent_version_id", sa.String(), nullable=True),
    )
    op.execute(
        """
        UPDATE chatsession AS chat
        SET agent_version_id = COALESCE(
            (
                SELECT execution.agent_version_id
                FROM agentexecution AS execution
                JOIN agentinvocation AS invocation
                  ON invocation.id = execution.invocation_id
                WHERE invocation.session_id = chat.id
                ORDER BY execution.created_at DESC, execution.id DESC
                LIMIT 1
            ),
            (
                SELECT version.id
                FROM agentversion AS version
                WHERE version.agent_id = chat.agent_id
                ORDER BY version.version DESC, version.created_at DESC, version.id DESC
                LIMIT 1
            )
        )
        """
    )
    op.alter_column("chatsession", "agent_version_id", nullable=False)
    op.create_foreign_key(
        "fk_chatsession_agent_version_id_agentversion",
        "chatsession",
        "agentversion",
        ["agent_version_id"],
        ["id"],
    )
    op.create_index(
        "ix_chatsession_agent_version_id",
        "chatsession",
        ["agent_version_id"],
    )

    op.create_table(
        "researchpackage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("chatsession.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(),
            sa.ForeignKey("agentexecution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_version_id",
            sa.String(),
            sa.ForeignKey("agentversion.id"),
            nullable=False,
        ),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("topic_hash", sa.String(), nullable=False),
        sa.Column(
            "package_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "provider_diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("rendered_content", sa.Text(), nullable=False),
        sa.Column("valid_source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("isolated_source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "removed_unknown_reference_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "execution_id",
            "topic_hash",
            name="ux_researchpackage_execution_topic",
        ),
    )
    for column in ("session_id", "execution_id", "agent_version_id", "topic_hash"):
        op.create_index(
            f"ix_researchpackage_{column}",
            "researchpackage",
            [column],
        )
    op.create_index(
        "ix_researchpackage_session_created",
        "researchpackage",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_researchpackage_session_created", table_name="researchpackage")
    for column in ("topic_hash", "agent_version_id", "execution_id", "session_id"):
        op.drop_index(f"ix_researchpackage_{column}", table_name="researchpackage")
    op.drop_table("researchpackage")
    op.drop_index("ix_chatsession_agent_version_id", table_name="chatsession")
    op.drop_constraint(
        "fk_chatsession_agent_version_id_agentversion",
        "chatsession",
        type_="foreignkey",
    )
    op.drop_column("chatsession", "agent_version_id")
