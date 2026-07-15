"""Add durable resume, bounded tool audit and post-processing state.

Revision ID: 202607140001
Revises: 202607130005
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607140001"
down_revision: str | None = "202607130005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column("postprocess_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agentexecution_postprocess_completed_at",
        "agentexecution",
        ["postprocess_completed_at"],
    )

    op.add_column(
        "toolexecution",
        sa.Column("arguments_hash", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "toolexecution",
        sa.Column("result_digest", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_toolexecution_arguments_hash", "toolexecution", ["arguments_hash"])
    op.create_index("ix_toolexecution_result_digest", "toolexecution", ["result_digest"])
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY execution_id, tool_call_id ORDER BY created_at, id
            ) AS ordinal
            FROM toolexecution
            WHERE tool_call_id IS NOT NULL
        )
        UPDATE toolexecution AS target
        SET tool_call_id = target.tool_call_id || '-legacy-' || target.id
        FROM ranked
        WHERE target.id = ranked.id AND ranked.ordinal > 1
        """
    )
    op.create_unique_constraint(
        "ux_toolexecution_execution_tool_call",
        "toolexecution",
        ["execution_id", "tool_call_id"],
    )

    op.create_table(
        "executionresumerequest",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(),
            sa.ForeignKey("agentexecution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interrupt_id", sa.String(), nullable=False),
        sa.Column("tool_calls_hash", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "execution_id",
            "interrupt_id",
            name="ux_executionresumerequest_execution_interrupt",
        ),
    )
    for column in (
        "execution_id",
        "interrupt_id",
        "tool_calls_hash",
        "status",
        "claimed_by",
        "claimed_at",
        "consumed_at",
    ):
        op.create_index(
            f"ix_executionresumerequest_{column}",
            "executionresumerequest",
            [column],
        )
    op.create_index(
        "ix_executionresumerequest_status_created",
        "executionresumerequest",
        ["status", "created_at"],
    )

    # Preserve resume input from any pre-release database that used the JSON field.
    op.execute(
        """
        INSERT INTO executionresumerequest (
            id, execution_id, interrupt_id, tool_calls_hash, value, status,
            created_at, updated_at
        )
        SELECT
            'res_' || left(md5(id || updated_at::text), 24),
            id,
            COALESCE(
                interrupt_payload #>> '{interrupts,0,id}',
                'legacy-' || id
            ),
            md5(COALESCE((interrupt_payload #> '{interrupts,0,value,tool_calls}')::text, '[]')),
            jsonb_build_object('payload', resume_payload->'value'),
            'pending',
            updated_at,
            updated_at
        FROM agentexecution
        WHERE resume_payload <> '{}'::jsonb
          AND resume_payload ? 'value'
        ON CONFLICT (execution_id, interrupt_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("executionresumerequest")
    op.drop_constraint(
        "ux_toolexecution_execution_tool_call",
        "toolexecution",
        type_="unique",
    )
    op.drop_index("ix_toolexecution_result_digest", table_name="toolexecution")
    op.drop_index("ix_toolexecution_arguments_hash", table_name="toolexecution")
    op.drop_column("toolexecution", "result_digest")
    op.drop_column("toolexecution", "arguments_hash")
    op.drop_index(
        "ix_agentexecution_postprocess_completed_at",
        table_name="agentexecution",
    )
    op.drop_column("agentexecution", "postprocess_completed_at")
