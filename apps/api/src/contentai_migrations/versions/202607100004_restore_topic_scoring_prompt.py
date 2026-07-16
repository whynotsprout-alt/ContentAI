"""Restore the topic scoring prompt on versioned content accounts.

Revision ID: 202607100004
Revises: 202607100003
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607100004"
down_revision: str | None = "202607100003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agentversion",
        sa.Column("topic_scoring_prompt", sa.String(), nullable=False, server_default=""),
    )
    # Versions created by the Agent Profile migration stored the two legacy prompts
    # together. Restore their original separation without changing newer free-form prompts.
    op.execute(
        """
        UPDATE agentversion
        SET
            topic_scoring_prompt = btrim(
                replace(
                    split_part(content_prompt, 'Content creation prompt:', 1),
                    'Topic scoring prompt:',
                    ''
                ),
                E' \t\n\r'
            ),
            content_prompt = btrim(
                split_part(content_prompt, 'Content creation prompt:', 2),
                E' \t\n\r'
            )
        WHERE content_prompt LIKE 'Topic scoring prompt:%Content creation prompt:%'
        """
    )
    op.alter_column("agentversion", "topic_scoring_prompt", server_default=None)


def downgrade() -> None:
    op.drop_column("agentversion", "topic_scoring_prompt")
