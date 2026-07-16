"""Add sticky streaming degradation and logical execution attempts.

Revision ID: 202607130005
Revises: 202607130003
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607130005"
down_revision: str | None = "202607130003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column("next_attempt_kind", sa.String(16), nullable=False, server_default="initial"),
    )
    op.add_column("agentexecution", sa.Column("current_attempt_id", sa.String(120), nullable=True))
    op.add_column(
        "agentexecution",
        sa.Column("streaming_degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "agentexecution",
        sa.Column("streaming_degraded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agentexecution",
        sa.Column("streaming_degraded_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_agentexecution_next_attempt_kind", "agentexecution", ["next_attempt_kind"])
    op.create_index(
        "ix_agentexecution_current_attempt_id", "agentexecution", ["current_attempt_id"]
    )
    op.create_index(
        "ix_agentexecution_streaming_degraded", "agentexecution", ["streaming_degraded"]
    )
    op.create_index(
        "ix_agentexecution_streaming_degraded_at",
        "agentexecution",
        ["streaming_degraded_at"],
    )
    op.create_table(
        "agentexecutionattempt",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column(
            "execution_id", sa.String(120), sa.ForeignKey("agentexecution.id"), nullable=False
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "execution_id", "ordinal", name="ux_agentexecutionattempt_execution_ordinal"
        ),
    )
    for column in ("execution_id", "ordinal", "kind", "worker_id", "status"):
        op.create_index(f"ix_agentexecutionattempt_{column}", "agentexecutionattempt", [column])
    op.create_index(
        "ix_agentexecutionattempt_execution_started",
        "agentexecutionattempt",
        ["execution_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("agentexecutionattempt")
    for index in (
        "ix_agentexecution_streaming_degraded",
        "ix_agentexecution_current_attempt_id",
        "ix_agentexecution_next_attempt_kind",
    ):
        op.drop_index(index, table_name="agentexecution")
    for column in (
        "streaming_degraded_reason",
        "streaming_degraded_at",
        "streaming_degraded",
        "current_attempt_id",
        "next_attempt_kind",
    ):
        op.drop_column("agentexecution", column)
