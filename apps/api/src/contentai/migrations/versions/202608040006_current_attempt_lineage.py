"""enforce current execution attempt lineage

Revision ID: 202608040006
Revises: 202608040005
Create Date: 2026-08-04 17:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202608040006"
down_revision: str | Sequence[str] | None = "202608040005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTEMPT_LINEAGE_INDEX = "ux_agentexecutionattempt_id_execution"
CURRENT_ATTEMPT_LINEAGE_FK = "fk_agentexecution_current_attempt_lineage"


def upgrade() -> None:
    # Freeze both sides while validating and installing the circular lineage
    # reference. Invalid pointers are never reassigned silently.
    op.execute(
        "LOCK TABLE agentexecution, agentexecutionattempt "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        DECLARE
            mismatch_count bigint;
        BEGIN
            SELECT count(*)
            INTO mismatch_count
            FROM agentexecution AS execution
            LEFT JOIN agentexecutionattempt AS attempt
              ON attempt.id = execution.current_attempt_id
             AND attempt.execution_id = execution.id
            WHERE execution.current_attempt_id IS NOT NULL
              AND attempt.id IS NULL;

            IF mismatch_count > 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = format(
                        'agentexecution current_attempt lineage preflight failed: '
                        'mismatch_count=%s. No rows were modified; correct or clear '
                        'invalid current_attempt_id values before retrying.',
                        mismatch_count
                    );
            END IF;
        END
        $$
        """
    )
    op.create_index(
        ATTEMPT_LINEAGE_INDEX,
        "agentexecutionattempt",
        ["id", "execution_id"],
        unique=True,
    )
    op.create_foreign_key(
        CURRENT_ATTEMPT_LINEAGE_FK,
        "agentexecution",
        "agentexecutionattempt",
        ["current_attempt_id", "id"],
        ["id", "execution_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        CURRENT_ATTEMPT_LINEAGE_FK,
        "agentexecution",
        type_="foreignkey",
    )
    op.drop_index(
        ATTEMPT_LINEAGE_INDEX,
        table_name="agentexecutionattempt",
    )
