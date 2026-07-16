"""Keep legacy tenant-wide agent profiles accessible after ownership split."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607100002"
down_revision: str | None = "202607100001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("agentprofile", "owner_user_id", nullable=True)
    # Profiles created by the legacy account table were tenant-scoped, not
    # user-scoped. New profiles created by the service keep an explicit owner.
    op.execute(
        sa.text("UPDATE agentprofile SET owner_user_id = NULL WHERE owner_user_id = 'local-user'")
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE agentprofile SET owner_user_id = 'local-user' WHERE owner_user_id IS NULL")
    )
    op.alter_column("agentprofile", "owner_user_id", nullable=False)
