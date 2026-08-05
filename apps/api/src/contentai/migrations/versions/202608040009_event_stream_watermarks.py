"""add durable execution event stream watermarks

Revision ID: 202608040009
Revises: 202608040008
Create Date: 2026-08-04 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608040009"
down_revision: str | Sequence[str] | None = "202608040008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentexecution",
        sa.Column(
            "stream_committed_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "agentexecution",
        sa.Column("terminal_stream_sequence", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "agentexecution",
        sa.Column("terminal_stream_attempt_id", sa.String(), nullable=True),
    )
    op.add_column(
        "agentexecution",
        sa.Column("terminal_stream_status", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_agentexecution_stream_committed_sequence_nonnegative",
        "agentexecution",
        "stream_committed_sequence BETWEEN 0 AND 9007199254740991",
    )
    op.create_check_constraint(
        "ck_agentexecution_terminal_stream_sequence_valid",
        "agentexecution",
        "(terminal_stream_sequence IS NULL AND "
        "terminal_stream_attempt_id IS NULL AND terminal_stream_status IS NULL) OR "
        "(terminal_stream_sequence IS NOT NULL AND terminal_stream_sequence > 0 AND "
        "terminal_stream_sequence = stream_committed_sequence AND "
        "terminal_stream_attempt_id IS NOT NULL AND "
        "btrim(terminal_stream_attempt_id) <> '' AND "
        "terminal_stream_status IN "
        "('waiting_input', 'completed', 'failed', 'cancelled'))",
    )

    # Redis streams are intentionally ephemeral and cannot be inspected safely
    # from an Alembic transaction. Existing rows therefore have no trustworthy
    # committed watermark; preserve durable messages while making historical
    # SSE replay explicitly fail closed.
    op.execute(
        sa.text(
            """
            UPDATE agentexecution
            SET streaming_degraded = TRUE,
                streaming_degraded_at = clock_timestamp(),
                streaming_degraded_reason = 'STREAM_WATERMARK_MIGRATION_REQUIRED',
                updated_at = clock_timestamp()
            WHERE streaming_degraded = FALSE
              AND (
                first_event_at IS NOT NULL
                OR first_token_at IS NOT NULL
                OR status IN ('waiting_input', 'completed', 'failed', 'cancelled')
              )
            """
        )
    )


def downgrade() -> None:
    # Sticky degradation is deliberately retained: clearing it could overwrite
    # a real publication failure that won concurrently with this migration.
    op.drop_constraint(
        "ck_agentexecution_terminal_stream_sequence_valid",
        "agentexecution",
        type_="check",
    )
    op.drop_constraint(
        "ck_agentexecution_stream_committed_sequence_nonnegative",
        "agentexecution",
        type_="check",
    )
    op.drop_column("agentexecution", "terminal_stream_sequence")
    op.drop_column("agentexecution", "terminal_stream_status")
    op.drop_column("agentexecution", "terminal_stream_attempt_id")
    op.drop_column("agentexecution", "stream_committed_sequence")
