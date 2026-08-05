from __future__ import annotations

import pytest
from alembic import command
from contentai.core.alembic import build_alembic_config
from contentai.db.session import get_engine
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
)
from contentai.models.enums import MessageRole
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

SESSION_A = "lineage-session-a"
SESSION_B = "lineage-session-b"
INVOCATION_A = "lineage-invocation-a"
INVOCATION_B = "lineage-invocation-b"
EXECUTION_A = "lineage-execution-a"
EXECUTION_B = "lineage-execution-b"


def _seed_two_lineages() -> None:
    with Session(get_engine()) as session:
        session.add_all(
            [
                ChatSession(
                    id=SESSION_A,
                    langgraph_thread_id="lineage-thread-a",
                    agent_id="default-agent",
                    agent_version_id="default-agent-v1",
                    user_id="local-user",
                ),
                ChatSession(
                    id=SESSION_B,
                    langgraph_thread_id="lineage-thread-b",
                    agent_id="default-agent",
                    agent_version_id="default-agent-v1",
                    user_id="local-user",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AgentInvocation(
                    id=INVOCATION_A,
                    session_id=SESSION_A,
                    agent_id="default-agent",
                    user_id="local-user",
                ),
                AgentInvocation(
                    id=INVOCATION_B,
                    session_id=SESSION_B,
                    agent_id="default-agent",
                    user_id="local-user",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AgentExecution(
                    id=EXECUTION_A,
                    invocation_id=INVOCATION_A,
                    session_id=SESSION_A,
                    agent_version_id="default-agent-v1",
                    model_config_id=DEFAULT_MODEL_CONFIG_ID,
                ),
                AgentExecution(
                    id=EXECUTION_B,
                    invocation_id=INVOCATION_B,
                    session_id=SESSION_B,
                    agent_version_id="default-agent-v1",
                    model_config_id=DEFAULT_MODEL_CONFIG_ID,
                ),
            ]
        )
        session.commit()


def _insert_message(
    *,
    message_id: str,
    session_id: str,
    invocation_id: str | None,
    execution_id: str | None,
) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO chatmessage (
                    id, session_id, invocation_id, execution_id,
                    role, message_type, content, created_at
                )
                VALUES (
                    :message_id, :session_id, :invocation_id, :execution_id,
                    'assistant', 'text', 'lineage constraint probe', now()
                )
                """
            ),
            {
                "message_id": message_id,
                "session_id": session_id,
                "invocation_id": invocation_id,
                "execution_id": execution_id,
            },
        )


def test_chatmessage_lineage_schema_matches_orm_metadata() -> None:
    inspector = inspect(get_engine())
    unique_indexes = {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("agentexecution")
        if index.get("unique")
    }
    assert (
        "ux_agentexecution_id_invocation_session",
        ("id", "invocation_id", "session_id"),
    ) in unique_indexes

    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("chatmessage")
    }
    assert "ck_chatmessage_execution_requires_invocation" in checks

    foreign_keys = {
        constraint["name"]: (
            tuple(constraint["constrained_columns"]),
            constraint["referred_table"],
            tuple(constraint["referred_columns"]),
        )
        for constraint in inspector.get_foreign_keys("chatmessage")
    }
    assert foreign_keys["fk_chatmessage_invocation_session"] == (
        ("invocation_id", "session_id"),
        "agentinvocation",
        ("id", "session_id"),
    )
    assert foreign_keys["fk_chatmessage_execution_lineage"] == (
        ("execution_id", "invocation_id", "session_id"),
        "agentexecution",
        ("id", "invocation_id", "session_id"),
    )
    assert all(
        len(identifier) <= 63
        for identifier in (
            "ux_agentexecution_id_invocation_session",
            "ck_chatmessage_execution_requires_invocation",
            "fk_chatmessage_invocation_session",
            "fk_chatmessage_execution_lineage",
        )
    )


def test_normal_chatmessage_creation_supports_each_valid_lineage_shape() -> None:
    _seed_two_lineages()

    with Session(get_engine()) as session:
        session.add_all(
            [
                ChatMessage(
                    id="lineage-message-session-only",
                    session_id=SESSION_A,
                    role=MessageRole.user,
                    content="session-only historical message",
                ),
                ChatMessage(
                    id="lineage-message-invocation",
                    session_id=SESSION_A,
                    invocation_id=INVOCATION_A,
                    role=MessageRole.user,
                    content="new turn input",
                ),
                ChatMessage(
                    id="lineage-message-execution",
                    session_id=SESSION_A,
                    invocation_id=INVOCATION_A,
                    execution_id=EXECUTION_A,
                    role=MessageRole.assistant,
                    content="completed turn output",
                ),
            ]
        )
        session.commit()

    with Session(get_engine()) as session:
        messages = session.exec(
            select(ChatMessage).order_by(ChatMessage.id)
        ).all()
    assert [message.id for message in messages] == [
        "lineage-message-execution",
        "lineage-message-invocation",
        "lineage-message-session-only",
    ]


@pytest.mark.parametrize(
    ("message_id", "session_id", "invocation_id", "execution_id", "constraint"),
    [
        (
            "lineage-message-missing-invocation",
            SESSION_A,
            None,
            EXECUTION_A,
            "ck_chatmessage_execution_requires_invocation",
        ),
        (
            "lineage-message-wrong-invocation-session",
            SESSION_B,
            INVOCATION_A,
            None,
            "fk_chatmessage_invocation_session",
        ),
        (
            "lineage-message-wrong-execution",
            SESSION_A,
            INVOCATION_A,
            EXECUTION_B,
            "fk_chatmessage_execution_lineage",
        ),
    ],
)
def test_database_rejects_invalid_chatmessage_lineage(
    message_id: str,
    session_id: str,
    invocation_id: str | None,
    execution_id: str | None,
    constraint: str,
) -> None:
    _seed_two_lineages()

    with pytest.raises(IntegrityError, match=constraint):
        _insert_message(
            message_id=message_id,
            session_id=session_id,
            invocation_id=invocation_id,
            execution_id=execution_id,
        )


def test_chatmessage_lineage_migration_fails_fast_without_reassigning_rows() -> None:
    config = build_alembic_config()
    engine = get_engine()
    dirty_message_id = "lineage-message-preflight-mismatch"

    try:
        command.downgrade(config, "202608040003")
        _seed_two_lineages()
        _insert_message(
            message_id=dirty_message_id,
            session_id=SESSION_B,
            invocation_id=INVOCATION_A,
            execution_id=None,
        )

        with pytest.raises(IntegrityError, match="chatmessage lineage preflight failed"):
            command.upgrade(config, "202608040004")

        with engine.connect() as connection:
            persisted = connection.execute(
                text(
                    """
                    SELECT session_id, invocation_id, execution_id
                    FROM chatmessage
                    WHERE id = :message_id
                    """
                ),
                {"message_id": dirty_message_id},
            ).one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert persisted == (SESSION_B, INVOCATION_A, None)
        assert revision == "202608040003"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM chatmessage WHERE id = :message_id"),
                {"message_id": dirty_message_id},
            )
        command.upgrade(config, "head")
