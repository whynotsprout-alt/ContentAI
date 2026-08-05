"""enforce chat message invocation and execution lineage

Revision ID: 202608040004
Revises: 202608040003
Create Date: 2026-08-04 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202608040004"
down_revision: str | Sequence[str] | None = "202608040003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXECUTION_LINEAGE_INDEX = "ux_agentexecution_id_invocation_session"
EXECUTION_REQUIRES_INVOCATION_CHECK = (
    "ck_chatmessage_execution_requires_invocation"
)
INVOCATION_SESSION_FK = "fk_chatmessage_invocation_session"
EXECUTION_LINEAGE_FK = "fk_chatmessage_execution_lineage"


def upgrade() -> None:
    # Keep the validation and DDL in one PostgreSQL transaction, and prevent a
    # writer from inserting a bad row between the preflight and the constraints.
    # Existing rows are never reassigned: operators must repair or delete any
    # invalid ownership explicitly before retrying the migration.
    op.execute("LOCK TABLE chatmessage IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        DECLARE
            execution_without_invocation bigint;
            invocation_session_mismatch bigint;
            execution_lineage_mismatch bigint;
        BEGIN
            SELECT
                count(*) FILTER (
                    WHERE message.execution_id IS NOT NULL
                      AND message.invocation_id IS NULL
                ),
                count(*) FILTER (
                    WHERE message.invocation_id IS NOT NULL
                      AND (
                          invocation.id IS NULL
                          OR invocation.session_id IS DISTINCT FROM message.session_id
                      )
                ),
                count(*) FILTER (
                    WHERE message.execution_id IS NOT NULL
                      AND (
                          execution.id IS NULL
                          OR execution.invocation_id IS DISTINCT FROM message.invocation_id
                          OR execution.session_id IS DISTINCT FROM message.session_id
                      )
                )
            INTO
                execution_without_invocation,
                invocation_session_mismatch,
                execution_lineage_mismatch
            FROM chatmessage AS message
            LEFT JOIN agentinvocation AS invocation
              ON invocation.id = message.invocation_id
            LEFT JOIN agentexecution AS execution
              ON execution.id = message.execution_id;

            IF execution_without_invocation > 0
               OR invocation_session_mismatch > 0
               OR execution_lineage_mismatch > 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = format(
                        'chatmessage lineage preflight failed: '
                        'execution_without_invocation=%s, '
                        'invocation_session_mismatch=%s, '
                        'execution_lineage_mismatch=%s. '
                        'No rows were modified; correct or delete the invalid '
                        'chatmessage rows before retrying.',
                        execution_without_invocation,
                        invocation_session_mismatch,
                        execution_lineage_mismatch
                    );
            END IF;
        END
        $$
        """
    )

    op.create_index(
        EXECUTION_LINEAGE_INDEX,
        "agentexecution",
        ["id", "invocation_id", "session_id"],
        unique=True,
    )
    op.create_check_constraint(
        EXECUTION_REQUIRES_INVOCATION_CHECK,
        "chatmessage",
        "execution_id IS NULL OR invocation_id IS NOT NULL",
    )
    op.create_foreign_key(
        INVOCATION_SESSION_FK,
        "chatmessage",
        "agentinvocation",
        ["invocation_id", "session_id"],
        ["id", "session_id"],
        ondelete="CASCADE",
    )
    # MATCH SIMPLE skips a composite FK when any referencing column is NULL.
    # The CHECK above closes that escape hatch whenever execution_id is set;
    # invocation-only messages remain valid and are covered by the FK above.
    op.create_foreign_key(
        EXECUTION_LINEAGE_FK,
        "chatmessage",
        "agentexecution",
        ["execution_id", "invocation_id", "session_id"],
        ["id", "invocation_id", "session_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # The referencing constraints must be removed before their supporting
    # unique index.
    op.drop_constraint(
        EXECUTION_LINEAGE_FK,
        "chatmessage",
        type_="foreignkey",
    )
    op.drop_constraint(
        INVOCATION_SESSION_FK,
        "chatmessage",
        type_="foreignkey",
    )
    op.drop_constraint(
        EXECUTION_REQUIRES_INVOCATION_CHECK,
        "chatmessage",
        type_="check",
    )
    op.drop_index(EXECUTION_LINEAGE_INDEX, table_name="agentexecution")
