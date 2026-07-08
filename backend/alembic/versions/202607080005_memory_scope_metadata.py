"""Add scoped structured memory metadata.

Revision ID: 202607080005
Revises: 202607080004
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607080005"
down_revision: str | None = "202607080004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ux_memoryrecord_namespace_key", "memoryrecord", type_="unique")

    op.add_column(
        "memoryrecord",
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="local"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("user_id", sa.String(), nullable=False, server_default="local-user"),
    )
    op.add_column("memoryrecord", sa.Column("account_id", sa.String(), nullable=True))
    op.add_column("memoryrecord", sa.Column("session_id", sa.String(), nullable=True))
    op.add_column(
        "memoryrecord",
        sa.Column("memory_scope", sa.String(), nullable=False, server_default="long_term"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("source_type", sa.String(), nullable=False, server_default=""),
    )
    op.add_column("memoryrecord", sa.Column("source_session_id", sa.String(), nullable=True))
    op.add_column("memoryrecord", sa.Column("source_message_id", sa.String(), nullable=True))
    op.add_column("memoryrecord", sa.Column("source_execution_id", sa.String(), nullable=True))
    op.add_column(
        "memoryrecord",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memoryrecord",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION contentai_try_parse_jsonb(value text)
        RETURNS jsonb AS $$
        BEGIN
            RETURN COALESCE(value::jsonb, '{}'::jsonb);
        EXCEPTION WHEN others THEN
            RETURN '{}'::jsonb;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE
        """
    )
    op.alter_column(
        "memoryrecord",
        "payload",
        type_=postgresql.JSONB(),
        existing_nullable=False,
        postgresql_using="contentai_try_parse_jsonb(payload)",
    )
    op.execute("DROP FUNCTION contentai_try_parse_jsonb(text)")

    op.execute(
        """
        UPDATE memoryrecord
        SET source_type = COALESCE(NULLIF(payload->>'source', ''), source_type),
            confidence = CASE
                WHEN payload->>'confidence' ~ '^[0-9]+(\\.[0-9]+)?$'
                THEN (payload->>'confidence')::float
                ELSE confidence
            END,
            importance_score = CASE
                WHEN payload->>'importance_score' ~ '^[0-9]+(\\.[0-9]+)?$'
                THEN (payload->>'importance_score')::float
                ELSE importance_score
            END
        WHERE jsonb_typeof(payload) = 'object'
        """
    )

    op.create_index("ix_memoryrecord_tenant_id", "memoryrecord", ["tenant_id"])
    op.create_index("ix_memoryrecord_user_id", "memoryrecord", ["user_id"])
    op.create_index("ix_memoryrecord_account_id", "memoryrecord", ["account_id"])
    op.create_index("ix_memoryrecord_session_id", "memoryrecord", ["session_id"])
    op.create_index("ix_memoryrecord_memory_scope", "memoryrecord", ["memory_scope"])
    op.create_index("ix_memoryrecord_source_session_id", "memoryrecord", ["source_session_id"])
    op.create_index("ix_memoryrecord_source_message_id", "memoryrecord", ["source_message_id"])
    op.create_index("ix_memoryrecord_source_execution_id", "memoryrecord", ["source_execution_id"])
    op.create_index("ix_memoryrecord_last_accessed_at", "memoryrecord", ["last_accessed_at"])
    op.create_index("ix_memoryrecord_expires_at", "memoryrecord", ["expires_at"])
    op.create_index("ix_memoryrecord_deleted_at", "memoryrecord", ["deleted_at"])
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

    for column_name in (
        "tenant_id",
        "user_id",
        "memory_scope",
        "confidence",
        "importance_score",
        "source_type",
        "version",
        "access_count",
    ):
        op.alter_column("memoryrecord", column_name, server_default=None)


def downgrade() -> None:
    op.drop_index("ux_memoryrecord_active_short_term", table_name="memoryrecord")
    op.drop_index("ux_memoryrecord_active_long_term", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_lookup", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_deleted_at", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_expires_at", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_last_accessed_at", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_source_execution_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_source_message_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_source_session_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_memory_scope", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_session_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_account_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_user_id", table_name="memoryrecord")
    op.drop_index("ix_memoryrecord_tenant_id", table_name="memoryrecord")

    op.alter_column(
        "memoryrecord",
        "payload",
        type_=sa.String(),
        existing_nullable=False,
        postgresql_using="payload::text",
    )
    op.create_unique_constraint(
        "ux_memoryrecord_namespace_key",
        "memoryrecord",
        ["namespace", "memory_key"],
    )
    for column_name in (
        "deleted_at",
        "expires_at",
        "last_accessed_at",
        "access_count",
        "version",
        "source_execution_id",
        "source_message_id",
        "source_session_id",
        "source_type",
        "importance_score",
        "confidence",
        "memory_scope",
        "session_id",
        "account_id",
        "user_id",
        "tenant_id",
    ):
        op.drop_column("memoryrecord", column_name)
