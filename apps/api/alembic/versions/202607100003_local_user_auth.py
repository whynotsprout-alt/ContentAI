"""Add local users, sessions and model usage without deleting legacy content.

Revision ID: 202607100003
Revises: 202607100002
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607100003"
down_revision: str | None = "202607100002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "appuser",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("email_normalized", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_verification"),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email_normalized", name="ux_appuser_email_normalized"),
        sa.UniqueConstraint("tenant_id", name="ux_appuser_tenant_id"),
    )
    for column in (
        "tenant_id",
        "email_normalized",
        "role",
        "status",
        "email_verified_at",
        "locked_until",
        "last_login_at",
        "created_at",
    ):
        op.create_index(f"ix_appuser_{column}", "appuser", [column])
    op.create_index("ix_appuser_role_status", "appuser", ["role", "status"])

    op.create_table(
        "authsession",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("appuser.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("csrf_hash", sa.String(), nullable=False),
        sa.Column("user_agent", sa.String(), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="ux_authsession_token_hash"),
    )
    op.create_index("ix_authsession_user_id", "authsession", ["user_id"])
    op.create_index("ix_authsession_token_hash", "authsession", ["token_hash"])
    op.create_index("ix_authsession_expires_at", "authsession", ["expires_at"])
    op.create_index("ix_authsession_revoked_at", "authsession", ["revoked_at"])
    op.create_index(
        "ix_authsession_user_active",
        "authsession",
        ["user_id", "revoked_at", "expires_at"],
    )

    op.create_table(
        "useractiontoken",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("appuser.id"), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="ux_useractiontoken_token_hash"),
    )
    for column in ("user_id", "purpose", "token_hash", "expires_at", "used_at"):
        op.create_index(f"ix_useractiontoken_{column}", "useractiontoken", [column])
    op.create_index(
        "ix_useractiontoken_user_purpose",
        "useractiontoken",
        ["user_id", "purpose", "created_at"],
    )

    op.create_table(
        "modelusage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("appuser.id"), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("execution_id", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("call_id", name="ux_modelusage_call_id"),
    )
    for column in (
        "call_id",
        "tenant_id",
        "user_id",
        "session_id",
        "execution_id",
        "category",
        "model_name",
        "usage_available",
        "created_at",
    ):
        op.create_index(f"ix_modelusage_{column}", "modelusage", [column])
    op.create_index("ix_modelusage_user_created", "modelusage", ["user_id", "created_at"])
    op.create_index(
        "ix_modelusage_execution_category",
        "modelusage",
        ["execution_id", "category"],
    )


def downgrade() -> None:
    op.drop_table("modelusage")
    op.drop_table("useractiontoken")
    op.drop_table("authsession")
    op.drop_table("appuser")
