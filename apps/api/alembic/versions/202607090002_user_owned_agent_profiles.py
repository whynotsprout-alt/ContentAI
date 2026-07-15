"""Replace accounts with user-owned agent profiles.

Revision ID: 202607090002
Revises: 202607090001
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607090002"
down_revision: str | None = "202607090001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agentprofile",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("agent_type", sa.String(), nullable=False, server_default="custom"),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.Column("updated_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "name",
            name="ux_agentprofile_tenant_user_name",
        ),
    )
    op.create_index("ix_agentprofile_tenant_id", "agentprofile", ["tenant_id"])
    op.create_index("ix_agentprofile_owner_user_id", "agentprofile", ["owner_user_id"])
    op.create_index("ix_agentprofile_name", "agentprofile", ["name"])
    op.create_index("ix_agentprofile_agent_type", "agentprofile", ["agent_type"])
    op.create_index("ix_agentprofile_status", "agentprofile", ["status"])
    op.create_index("ix_agentprofile_created_by_user_id", "agentprofile", ["created_by_user_id"])
    op.create_index("ix_agentprofile_updated_by_user_id", "agentprofile", ["updated_by_user_id"])
    op.create_index("ix_agentprofile_tenant_user", "agentprofile", ["tenant_id", "owner_user_id"])
    op.create_index(
        "ix_agentprofile_tenant_user_status",
        "agentprofile",
        ["tenant_id", "owner_user_id", "status"],
    )
    op.create_index(
        "ix_agentprofile_tenant_user_type",
        "agentprofile",
        ["tenant_id", "owner_user_id", "agent_type"],
    )

    op.create_table(
        "agentversion",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agentprofile.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_prompt", sa.String(), nullable=False),
        sa.Column("graph_name", sa.String(), nullable=False, server_default="default"),
        sa.Column("tools_config", postgresql.JSONB(), nullable=False),
        sa.Column("memory_config", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="ux_agentversion_agent_version"),
    )
    op.create_index("ix_agentversion_agent_id", "agentversion", ["agent_id"])
    op.create_index("ix_agentversion_version", "agentversion", ["version"])
    op.create_index("ix_agentversion_graph_name", "agentversion", ["graph_name"])
    op.create_index("ix_agentversion_created_by_user_id", "agentversion", ["created_by_user_id"])
    op.create_index("ix_agentversion_agent_created", "agentversion", ["agent_id", "created_at"])

    op.execute(
        """
        INSERT INTO agentprofile (
            id, tenant_id, owner_user_id, name, description, agent_type, status,
            created_by_user_id, updated_by_user_id, created_at, updated_at
        )
        SELECT
            id,
            tenant_id,
            NULL,
            name,
            positioning,
            'content',
            'active',
            NULL,
            NULL,
            created_at,
            updated_at
        FROM account
        """
    )
    op.execute(
        """
        INSERT INTO agentversion (
            id, agent_id, version, content_prompt, graph_name, tools_config,
            memory_config, created_by_user_id, created_at
        )
        SELECT
            'agv_' || left(md5(id), 24),
            id,
            1,
            concat_ws(
                E'\n\n',
                'Topic scoring prompt:',
                topic_scoring_prompt,
                'Content creation prompt:',
                content_creation_prompt
            ),
            'default',
            jsonb_build_object('hotspot_sources', hotspot_sources),
            '{}'::jsonb,
            'local-user',
            created_at
        FROM account
        """
    )

    op.drop_constraint("fk_chatsession_account_id_account", "chatsession", type_="foreignkey")
    op.drop_index("ix_chatsession_account_updated", table_name="chatsession")
    op.drop_index("ix_chatsession_account_id", table_name="chatsession")
    op.alter_column("chatsession", "account_id", new_column_name="agent_id")
    op.create_foreign_key(
        "fk_chatsession_agent_id_agentprofile",
        "chatsession",
        "agentprofile",
        ["agent_id"],
        ["id"],
    )
    op.create_index("ix_chatsession_agent_id", "chatsession", ["agent_id"])
    op.create_index("ix_chatsession_agent_updated", "chatsession", ["agent_id", "updated_at"])

    op.drop_constraint("agentinvocation_account_id_fkey", "agentinvocation", type_="foreignkey")
    op.drop_index("ix_agentinvocation_account_id", table_name="agentinvocation")
    op.alter_column("agentinvocation", "account_id", new_column_name="agent_id")
    op.create_foreign_key(
        "fk_agentinvocation_agent_id_agentprofile",
        "agentinvocation",
        "agentprofile",
        ["agent_id"],
        ["id"],
    )
    op.create_index("ix_agentinvocation_agent_id", "agentinvocation", ["agent_id"])

    op.add_column("agentexecution", sa.Column("agent_version_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE agentexecution
        SET agent_version_id = agentversion.id
        FROM agentinvocation
        JOIN agentversion ON agentversion.agent_id = agentinvocation.agent_id
        WHERE agentexecution.invocation_id = agentinvocation.id
          AND agentversion.version = 1
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM agentexecution WHERE agent_version_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot map every execution to an agent version; no data was deleted. '
                    'Repair orphaned invocation/account rows before retrying the migration.';
            END IF;
        END $$
        """
    )
    op.alter_column("agentexecution", "agent_version_id", nullable=False)
    op.create_foreign_key(
        "fk_agentexecution_agent_version_id_agentversion",
        "agentexecution",
        "agentversion",
        ["agent_version_id"],
        ["id"],
    )
    op.create_index("ix_agentexecution_agent_version_id", "agentexecution", ["agent_version_id"])

    op.drop_constraint("ux_account_tenant_name", "account", type_="unique")
    op.drop_index("ix_account_tenant_id", table_name="account")
    op.drop_index("ix_account_name", table_name="account")
    op.drop_table("account")

    op.alter_column("agentprofile", "description", server_default=None)
    op.alter_column("agentprofile", "agent_type", server_default=None)
    op.alter_column("agentprofile", "status", server_default=None)
    op.alter_column("agentversion", "graph_name", server_default=None)


def downgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("positioning", sa.String(), nullable=False),
        sa.Column("topic_scoring_prompt", sa.String(), nullable=False),
        sa.Column("content_creation_prompt", sa.String(), nullable=False),
        sa.Column("hotspot_sources", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="ux_account_tenant_name"),
    )
    op.execute(
        """
        INSERT INTO account (
            id, tenant_id, name, positioning, topic_scoring_prompt,
            content_creation_prompt, hotspot_sources, created_at, updated_at
        )
        SELECT DISTINCT ON (agentprofile.id)
            agentprofile.id,
            agentprofile.tenant_id,
            agentprofile.name,
            agentprofile.description,
            agentversion.content_prompt,
            agentversion.content_prompt,
            COALESCE(agentversion.tools_config->'hotspot_sources', '[]'::jsonb),
            agentprofile.created_at,
            agentprofile.updated_at
        FROM agentprofile
        JOIN agentversion ON agentversion.agent_id = agentprofile.id
        ORDER BY agentprofile.id, agentversion.version DESC
        """
    )
    op.create_index("ix_account_name", "account", ["name"])
    op.create_index("ix_account_tenant_id", "account", ["tenant_id"])

    op.drop_index("ix_agentexecution_agent_version_id", table_name="agentexecution")
    op.drop_constraint(
        "fk_agentexecution_agent_version_id_agentversion",
        "agentexecution",
        type_="foreignkey",
    )
    op.drop_column("agentexecution", "agent_version_id")

    op.drop_index("ix_agentinvocation_agent_id", table_name="agentinvocation")
    op.drop_constraint(
        "fk_agentinvocation_agent_id_agentprofile",
        "agentinvocation",
        type_="foreignkey",
    )
    op.alter_column("agentinvocation", "agent_id", new_column_name="account_id")
    op.create_foreign_key(
        "agentinvocation_account_id_fkey",
        "agentinvocation",
        "account",
        ["account_id"],
        ["id"],
    )
    op.create_index("ix_agentinvocation_account_id", "agentinvocation", ["account_id"])

    op.drop_index("ix_chatsession_agent_updated", table_name="chatsession")
    op.drop_index("ix_chatsession_agent_id", table_name="chatsession")
    op.drop_constraint("fk_chatsession_agent_id_agentprofile", "chatsession", type_="foreignkey")
    op.alter_column("chatsession", "agent_id", new_column_name="account_id")
    op.create_foreign_key(
        "fk_chatsession_account_id_account",
        "chatsession",
        "account",
        ["account_id"],
        ["id"],
    )
    op.create_index("ix_chatsession_account_id", "chatsession", ["account_id"])
    op.create_index("ix_chatsession_account_updated", "chatsession", ["account_id", "updated_at"])

    op.drop_index("ix_agentversion_agent_created", table_name="agentversion")
    op.drop_index("ix_agentversion_created_by_user_id", table_name="agentversion")
    op.drop_index("ix_agentversion_graph_name", table_name="agentversion")
    op.drop_index("ix_agentversion_version", table_name="agentversion")
    op.drop_index("ix_agentversion_agent_id", table_name="agentversion")
    op.drop_table("agentversion")

    op.drop_index("ix_agentprofile_tenant_user_type", table_name="agentprofile")
    op.drop_index("ix_agentprofile_tenant_user_status", table_name="agentprofile")
    op.drop_index("ix_agentprofile_tenant_user", table_name="agentprofile")
    op.drop_index("ix_agentprofile_updated_by_user_id", table_name="agentprofile")
    op.drop_index("ix_agentprofile_created_by_user_id", table_name="agentprofile")
    op.drop_index("ix_agentprofile_status", table_name="agentprofile")
    op.drop_index("ix_agentprofile_agent_type", table_name="agentprofile")
    op.drop_index("ix_agentprofile_name", table_name="agentprofile")
    op.drop_index("ix_agentprofile_owner_user_id", table_name="agentprofile")
    op.drop_index("ix_agentprofile_tenant_id", table_name="agentprofile")
    op.drop_table("agentprofile")
