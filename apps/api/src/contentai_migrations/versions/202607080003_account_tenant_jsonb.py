"""Add tenant isolation to accounts and use JSONB sources.

Revision ID: 202607080003
Revises: 202607080002
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607080003"
down_revision: str | None = "202607080002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column("tenant_id", sa.String(), nullable=False, server_default="local"),
    )
    op.create_index("ix_account_tenant_id", "account", ["tenant_id"])
    op.create_unique_constraint(
        "ux_account_tenant_name",
        "account",
        ["tenant_id", "name"],
    )
    op.alter_column(
        "account",
        "hotspot_sources",
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(),
        existing_nullable=False,
        postgresql_using="hotspot_sources::jsonb",
    )


def downgrade() -> None:
    op.alter_column(
        "account",
        "hotspot_sources",
        existing_type=postgresql.JSONB(),
        type_=sa.JSON(),
        existing_nullable=False,
        postgresql_using="hotspot_sources::json",
    )
    op.drop_constraint("ux_account_tenant_name", "account", type_="unique")
    op.drop_index("ix_account_tenant_id", table_name="account")
    op.drop_column("account", "tenant_id")
