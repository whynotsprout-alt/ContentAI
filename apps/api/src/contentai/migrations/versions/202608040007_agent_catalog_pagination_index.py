"""add the agent catalog pagination index

Revision ID: 202608040007
Revises: 202608040006
Create Date: 2026-08-04 18:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202608040007"
down_revision: str | Sequence[str] | None = "202608040006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_CATALOG_INDEX = "ix_agentprofile_user_created_id"


def upgrade() -> None:
    op.create_index(
        AGENT_CATALOG_INDEX,
        "agentprofile",
        ["user_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(AGENT_CATALOG_INDEX, table_name="agentprofile")
