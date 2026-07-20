"""V0.5.0 validate and contract.

Revision ID: 202607170002
Revises: 202607170001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607170002"
down_revision: str | Sequence[str] | None = "202607170001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINTS = (
    ("chatsession", "fk_chatsession_agent_owner"),
    ("chatsession", "fk_chatsession_version_agent"),
    ("agentinvocation", "fk_agentinvocation_session_lineage"),
    ("agentexecution", "fk_agentexecution_invocation_session"),
    ("agentexecution", "fk_agentexecution_session_version"),
    ("memoryrecord", "fk_memoryrecord_agent_owner"),
    ("memoryrecord", "fk_memoryrecord_session_owner"),
    ("researchpackage", "fk_researchpackage_execution_lineage"),
    ("executionresumerequest", "fk_executionresumerequest_message"),
    ("executionresumerequest", "ck_executionresumerequest_decision"),
    ("chatmessage", "fk_chatmessage_execution"),
)


def _abort_for_contract_violations() -> None:
    connection = op.get_bind()
    failures: list[str] = []
    token_ids = [
        str(row[0])
        for row in connection.execute(sa.text("SELECT id FROM useractiontoken ORDER BY id"))
    ]
    if token_ids:
        failures.append("non-empty UserActionToken rows: " + ", ".join(token_ids))
    execution_ids = [
        str(row[0])
        for row in connection.execute(
            sa.text("SELECT id FROM agentexecution WHERE session_id IS NULL ORDER BY id")
        )
    ]
    if execution_ids:
        failures.append("executions without sessions: " + ", ".join(execution_ids))
    if failures:
        raise RuntimeError("V0.5.0 contract preflight failed; " + "; ".join(failures))


def upgrade() -> None:
    _abort_for_contract_violations()
    for table_name, constraint_name in CONSTRAINTS:
        op.execute(
            f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}"
        )
    op.alter_column(
        "appuser",
        "must_change_password",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    )
    op.alter_column(
        "agentexecution",
        "session_id",
        existing_type=sa.String(),
        nullable=False,
    )
    op.drop_table("useractiontoken")
    op.create_index(
        "ix_appuser_created_id", "appuser", ["created_at", "id"], unique=False
    )
    op.create_index(
        "ix_appuser_status_created_id",
        "appuser",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_chatsession_user_updated_id",
        "chatsession",
        ["user_id", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_agentexecution_session_updated_id",
        "agentexecution",
        ["session_id", "updated_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agentexecution_session_updated_id")
    op.execute("DROP INDEX IF EXISTS ix_chatsession_user_updated_id")
    op.execute("DROP INDEX IF EXISTS ix_appuser_status_created_id")
    op.execute("DROP INDEX IF EXISTS ix_appuser_created_id")
    op.create_table(
        "useractiontoken",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["appuser.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="ux_useractiontoken_token_hash"),
    )
    for column in ("expires_at", "purpose", "token_hash", "used_at", "user_id"):
        op.create_index(f"ix_useractiontoken_{column}", "useractiontoken", [column])
    op.create_index(
        "ix_useractiontoken_user_purpose",
        "useractiontoken",
        ["user_id", "purpose", "created_at"],
    )
    op.alter_column(
        "agentexecution",
        "session_id",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "appuser",
        "must_change_password",
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=sa.false(),
    )
