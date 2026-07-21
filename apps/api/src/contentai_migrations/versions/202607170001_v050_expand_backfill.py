"""V0.5.0 expand and backfill.

Revision ID: 202607170001
Revises: 202607150001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607170001"
down_revision: str | Sequence[str] | None = "202607150001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PREFLIGHT_QUERIES = (
    ("non-empty UserActionToken rows", "SELECT id FROM useractiontoken ORDER BY id"),
    (
        "session owner mismatches",
        """
        SELECT s.id FROM chatsession s
        LEFT JOIN agentprofile a ON a.id = s.agent_id
        WHERE a.user_id IS DISTINCT FROM s.user_id ORDER BY s.id
        """,
    ),
    (
        "session version mismatches",
        """
        SELECT s.id FROM chatsession s
        LEFT JOIN agentversion v ON v.id = s.agent_version_id
        WHERE v.agent_id IS DISTINCT FROM s.agent_id ORDER BY s.id
        """,
    ),
    (
        "invocation owner/session mismatches",
        """
        SELECT i.id FROM agentinvocation i
        LEFT JOIN chatsession s ON s.id = i.session_id
        WHERE i.agent_id IS DISTINCT FROM s.agent_id
           OR i.user_id IS DISTINCT FROM s.user_id
        ORDER BY i.id
        """,
    ),
    (
        "execution version/session mismatches",
        """
        SELECT e.id FROM agentexecution e
        LEFT JOIN agentinvocation i ON i.id = e.invocation_id
        LEFT JOIN chatsession s ON s.id = i.session_id
        WHERE e.agent_version_id IS DISTINCT FROM s.agent_version_id ORDER BY e.id
        """,
    ),
    (
        "memory owner mismatches",
        """
        SELECT m.id FROM memoryrecord m
        LEFT JOIN agentprofile a ON a.id = m.agent_id
        LEFT JOIN chatsession s ON s.id = m.session_id
        WHERE (m.agent_id IS NOT NULL AND a.user_id IS DISTINCT FROM m.user_id)
           OR (m.session_id IS NOT NULL AND s.user_id IS DISTINCT FROM m.user_id)
        ORDER BY m.id
        """,
    ),
    (
        "research execution/session/version mismatches",
        """
        SELECT r.id FROM researchpackage r
        LEFT JOIN agentexecution e ON e.id = r.execution_id
        LEFT JOIN agentinvocation i ON i.id = e.invocation_id
        WHERE r.session_id IS DISTINCT FROM i.session_id
           OR r.agent_version_id IS DISTINCT FROM e.agent_version_id
        ORDER BY r.id
        """,
    ),
    (
        "multiple executions per invocation",
        """
        SELECT e.id FROM agentexecution e
        WHERE e.invocation_id IN (
            SELECT invocation_id FROM agentexecution
            GROUP BY invocation_id HAVING count(*) > 1
        ) ORDER BY e.id
        """,
    ),
    (
        "ambiguous assistant messages",
        """
        SELECT m.id FROM chatmessage m
        WHERE m.role = 'assistant' AND m.invocation_id IS NOT NULL
          AND (SELECT count(*) FROM agentexecution e
               WHERE e.invocation_id = m.invocation_id) <> 1
        ORDER BY m.id
        """,
    ),
    (
        "duplicate assistant messages",
        """
        SELECT m.id FROM chatmessage m
        WHERE m.role = 'assistant' AND m.invocation_id IN (
            SELECT invocation_id FROM chatmessage
            WHERE role = 'assistant' AND invocation_id IS NOT NULL
            GROUP BY invocation_id HAVING count(*) > 1
        ) ORDER BY m.id
        """,
    ),
)


def _run_preflight() -> None:
    connection = op.get_bind()
    failures: list[str] = []
    for label, query in PREFLIGHT_QUERIES:
        ids = [str(row[0]) for row in connection.execute(sa.text(query)).fetchall()]
        if ids:
            failures.append(f"{label}: {', '.join(ids)}")
    if failures:
        raise RuntimeError("V0.5.0 migration preflight failed; " + "; ".join(failures))


def _convert_application_timestamps(*, timezone: bool) -> None:
    connection = op.get_bind()
    source_type = "timestamp without time zone" if timezone else "timestamp with time zone"
    rows = connection.execute(
        sa.text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type = :source_type
              AND table_name NOT IN (
                'alembic_version', 'checkpoint_blobs', 'checkpoint_migrations',
                'checkpoint_writes', 'checkpoints', 'store', 'store_migrations'
              )
            ORDER BY table_name, ordinal_position
            """
        ),
        {"source_type": source_type},
    ).fetchall()
    preparer = connection.dialect.identifier_preparer
    target_type = postgresql.TIMESTAMP(timezone=timezone)
    for table_name, column_name in rows:
        quoted_column = preparer.quote(column_name)
        using = (
            f"{quoted_column} AT TIME ZONE 'UTC'"
            if timezone
            else f"{quoted_column} AT TIME ZONE 'UTC'"
        )
        op.alter_column(
            table_name,
            column_name,
            type_=target_type,
            postgresql_using=using,
        )


def upgrade() -> None:
    _run_preflight()
    _convert_application_timestamps(timezone=True)

    op.add_column(
        "appuser",
        sa.Column("must_change_password", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "appuser",
        sa.Column("temporary_password_expires_at", postgresql.TIMESTAMP(timezone=True)),
    )
    op.execute("UPDATE appuser SET must_change_password = false WHERE must_change_password IS NULL")
    op.create_index(
        "ix_appuser_temporary_password_expires_at",
        "appuser",
        ["temporary_password_expires_at"],
    )

    op.add_column("agentexecution", sa.Column("session_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE agentexecution e
        SET session_id = i.session_id
        FROM agentinvocation i
        WHERE i.id = e.invocation_id
        """
    )
    op.create_index("ix_agentexecution_session_id", "agentexecution", ["session_id"])

    op.add_column("agentinvocation", sa.Column("request_sha256", sa.String(length=64)))

    op.add_column(
        "executionoutbox",
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.add_column("chatmessage", sa.Column("execution_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE chatmessage m
        SET execution_id = e.id
        FROM agentexecution e
        WHERE m.role = 'assistant'
          AND m.invocation_id = e.invocation_id
          AND (SELECT count(*) FROM agentexecution candidate
               WHERE candidate.invocation_id = m.invocation_id) = 1
        """
    )
    op.create_index("ix_chatmessage_execution_id", "chatmessage", ["execution_id"])

    op.add_column("executionresumerequest", sa.Column("decision", sa.String(), nullable=True))
    op.add_column("executionresumerequest", sa.Column("message_id", sa.String(), nullable=True))
    op.create_index("ix_executionresumerequest_decision", "executionresumerequest", ["decision"])
    op.create_index("ix_executionresumerequest_message_id", "executionresumerequest", ["message_id"])

    # Parent unique anchors must exist before compound foreign keys are declared.
    op.create_index("ux_agentprofile_id_user", "agentprofile", ["id", "user_id"], unique=True)
    op.create_index("ux_agentversion_id_agent", "agentversion", ["id", "agent_id"], unique=True)
    op.create_index("ux_chatsession_id_user", "chatsession", ["id", "user_id"], unique=True)
    op.create_index(
        "ux_chatsession_id_agent_user",
        "chatsession",
        ["id", "agent_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ux_chatsession_id_agent_version",
        "chatsession",
        ["id", "agent_version_id"],
        unique=True,
    )
    op.create_index(
        "ux_agentinvocation_id_session",
        "agentinvocation",
        ["id", "session_id"],
        unique=True,
    )
    op.create_index(
        "ux_agentexecution_invocation",
        "agentexecution",
        ["invocation_id"],
        unique=True,
    )
    op.create_index(
        "ux_agentexecution_id_session_version",
        "agentexecution",
        ["id", "session_id", "agent_version_id"],
        unique=True,
    )
    op.create_index(
        "ux_chatmessage_assistant_execution",
        "chatmessage",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("role = 'assistant' AND execution_id IS NOT NULL"),
    )
    op.create_index(
        "ux_executionresumerequest_message",
        "executionresumerequest",
        ["message_id"],
        unique=True,
    )

    constraints = (
        "ALTER TABLE chatsession ADD CONSTRAINT fk_chatsession_agent_owner "
        "FOREIGN KEY (agent_id, user_id) REFERENCES agentprofile (id, user_id) "
        "ON DELETE RESTRICT NOT VALID",
        "ALTER TABLE chatsession ADD CONSTRAINT fk_chatsession_version_agent "
        "FOREIGN KEY (agent_version_id, agent_id) REFERENCES agentversion (id, agent_id) "
        "ON DELETE RESTRICT NOT VALID",
        "ALTER TABLE agentinvocation ADD CONSTRAINT fk_agentinvocation_session_lineage "
        "FOREIGN KEY (session_id, agent_id, user_id) "
        "REFERENCES chatsession (id, agent_id, user_id) ON DELETE CASCADE NOT VALID",
        "ALTER TABLE agentexecution ADD CONSTRAINT fk_agentexecution_invocation_session "
        "FOREIGN KEY (invocation_id, session_id) REFERENCES agentinvocation (id, session_id) "
        "ON DELETE CASCADE NOT VALID",
        "ALTER TABLE agentexecution ADD CONSTRAINT fk_agentexecution_session_version "
        "FOREIGN KEY (session_id, agent_version_id) REFERENCES chatsession (id, agent_version_id) "
        "ON DELETE CASCADE NOT VALID",
        "ALTER TABLE memoryrecord ADD CONSTRAINT fk_memoryrecord_agent_owner "
        "FOREIGN KEY (agent_id, user_id) REFERENCES agentprofile (id, user_id) "
        "ON DELETE CASCADE NOT VALID",
        "ALTER TABLE memoryrecord ADD CONSTRAINT fk_memoryrecord_session_owner "
        "FOREIGN KEY (session_id, user_id) REFERENCES chatsession (id, user_id) "
        "ON DELETE CASCADE NOT VALID",
        "ALTER TABLE researchpackage ADD CONSTRAINT fk_researchpackage_execution_lineage "
        "FOREIGN KEY (execution_id, session_id, agent_version_id) "
        "REFERENCES agentexecution (id, session_id, agent_version_id) "
        "ON DELETE CASCADE NOT VALID",
        "ALTER TABLE executionresumerequest ADD CONSTRAINT "
        "fk_executionresumerequest_message FOREIGN KEY (message_id) "
        "REFERENCES chatmessage (id) ON DELETE CASCADE NOT VALID",
        "ALTER TABLE executionresumerequest ADD CONSTRAINT "
        "ck_executionresumerequest_decision CHECK "
        "(decision IS NULL OR decision IN ('approve', 'reject')) NOT VALID",
    )
    for statement in constraints:
        op.execute(statement)

    op.create_foreign_key(
        "fk_chatmessage_execution",
        "chatmessage",
        "agentexecution",
        ["execution_id"],
        ["id"],
        ondelete="CASCADE",
        postgresql_not_valid=True,
    )

    op.create_table(
        "serviceheartbeat",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("service_name", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("queue_name", sa.String(), server_default="", nullable=False),
        sa.Column("status", sa.String(), server_default="healthy", nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("heartbeat_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_name",
            "instance_id",
            "queue_name",
            name="ux_serviceheartbeat_service_instance_queue",
        ),
    )
    op.create_index(
        "ix_serviceheartbeat_service_queue_heartbeat",
        "serviceheartbeat",
        ["service_name", "queue_name", "heartbeat_at"],
    )
    for column in ("service_name", "instance_id", "queue_name", "status", "heartbeat_at"):
        op.create_index(f"ix_serviceheartbeat_{column}", "serviceheartbeat", [column])

    op.create_table(
        "checkpointdeletionoutbox",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("checkpoint_ns", sa.String(), server_default="", nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String()),
        sa.Column("locked_until", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("last_error", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["appuser.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "checkpoint_ns", name="ux_checkpointdeletion_thread_ns"),
    )
    op.create_index(
        "ix_checkpointdeletion_status_available",
        "checkpointdeletionoutbox",
        ["status", "available_at"],
    )
    for column in (
        "user_id",
        "session_id",
        "thread_id",
        "checkpoint_ns",
        "status",
        "available_at",
        "locked_by",
        "locked_until",
    ):
        op.create_index(f"ix_checkpointdeletionoutbox_{column}", "checkpointdeletionoutbox", [column])

    op.create_table(
        "sideeffectreceipt",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), server_default="", nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="completed", nullable=False),
        sa.Column("result_digest", sa.String(), server_default="", nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["agentexecution.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "tool_call_id",
            name="ux_sideeffectreceipt_execution_tool_call",
        ),
    )
    op.create_index(
        "ix_sideeffectreceipt_status_created",
        "sideeffectreceipt",
        ["status", "created_at"],
    )
    for column in (
        "execution_id",
        "tool_call_id",
        "idempotency_key",
        "operation",
        "status",
        "result_digest",
    ):
        op.create_index(f"ix_sideeffectreceipt_{column}", "sideeffectreceipt", [column])


def downgrade() -> None:
    op.drop_table("sideeffectreceipt")
    op.drop_table("checkpointdeletionoutbox")
    op.drop_table("serviceheartbeat")

    for table, constraint in (
        ("executionresumerequest", "ck_executionresumerequest_decision"),
        ("executionresumerequest", "fk_executionresumerequest_message"),
        ("researchpackage", "fk_researchpackage_execution_lineage"),
        ("memoryrecord", "fk_memoryrecord_session_owner"),
        ("memoryrecord", "fk_memoryrecord_agent_owner"),
        ("agentexecution", "fk_agentexecution_session_version"),
        ("agentexecution", "fk_agentexecution_invocation_session"),
        ("agentinvocation", "fk_agentinvocation_session_lineage"),
        ("chatsession", "fk_chatsession_version_agent"),
        ("chatsession", "fk_chatsession_agent_owner"),
        ("chatmessage", "fk_chatmessage_execution"),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey" if constraint.startswith("fk_") else "check")

    for table, index in (
        ("executionresumerequest", "ux_executionresumerequest_message"),
        ("chatmessage", "ux_chatmessage_assistant_execution"),
        ("agentexecution", "ux_agentexecution_id_session_version"),
        ("agentexecution", "ux_agentexecution_invocation"),
        ("agentinvocation", "ux_agentinvocation_id_session"),
        ("chatsession", "ux_chatsession_id_agent_version"),
        ("chatsession", "ux_chatsession_id_agent_user"),
        ("chatsession", "ux_chatsession_id_user"),
        ("agentversion", "ux_agentversion_id_agent"),
        ("agentprofile", "ux_agentprofile_id_user"),
    ):
        op.drop_index(index, table_name=table)

    op.drop_index("ix_executionresumerequest_message_id", table_name="executionresumerequest")
    op.drop_index("ix_executionresumerequest_decision", table_name="executionresumerequest")
    op.drop_column("executionresumerequest", "message_id")
    op.drop_column("executionresumerequest", "decision")
    op.drop_index("ix_chatmessage_execution_id", table_name="chatmessage")
    op.drop_column("chatmessage", "execution_id")
    op.drop_index("ix_agentexecution_session_id", table_name="agentexecution")
    op.drop_column("agentexecution", "session_id")
    op.execute("ALTER TABLE executionoutbox DROP COLUMN IF EXISTS payload")
    op.drop_column("agentinvocation", "request_sha256")
    op.drop_index("ix_appuser_temporary_password_expires_at", table_name="appuser")
    op.drop_column("appuser", "temporary_password_expires_at")
    op.drop_column("appuser", "must_change_password")
    _convert_application_timestamps(timezone=False)
