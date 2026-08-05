from __future__ import annotations

import pytest
from alembic import command
from contentai.core.alembic import build_alembic_config
from contentai.db.session import get_engine
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatSession,
)
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session


def _seed_execution(session: Session, suffix: str) -> AgentExecution:
    chat = ChatSession(
        id=f"attempt-lineage-session-{suffix}",
        langgraph_thread_id=f"attempt-lineage-thread-{suffix}",
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
    )
    session.add(chat)
    session.flush()
    invocation = AgentInvocation(
        id=f"attempt-lineage-invocation-{suffix}",
        session_id=chat.id,
        agent_id=chat.agent_id,
        user_id=chat.user_id,
    )
    session.add(invocation)
    session.flush()
    execution = AgentExecution(
        id=f"attempt-lineage-execution-{suffix}",
        invocation_id=invocation.id,
        session_id=chat.id,
        agent_version_id=chat.agent_version_id,
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
    )
    session.add(execution)
    session.flush()
    return execution


def test_current_attempt_lineage_schema_matches_orm_metadata() -> None:
    inspector = inspect(get_engine())
    indexes = {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("agentexecutionattempt")
        if index.get("unique")
    }
    assert (
        "ux_agentexecutionattempt_id_execution",
        ("id", "execution_id"),
    ) in indexes

    foreign_keys = {
        constraint["name"]: (
            tuple(constraint["constrained_columns"]),
            constraint["referred_table"],
            tuple(constraint["referred_columns"]),
        )
        for constraint in inspector.get_foreign_keys("agentexecution")
    }
    assert foreign_keys["fk_agentexecution_current_attempt_lineage"] == (
        ("current_attempt_id", "id"),
        "agentexecutionattempt",
        ("id", "execution_id"),
    )


def test_database_rejects_cross_execution_current_attempt() -> None:
    first_id = "attempt-lineage-execution-first"
    attempt_id = "attempt-lineage-second-attempt"
    with Session(get_engine()) as session:
        _seed_execution(session, "first")
        second = _seed_execution(session, "second")
        attempt = AgentExecutionAttempt(
            id=attempt_id,
            execution_id=second.id,
            ordinal=1,
        )
        session.add(attempt)
        session.commit()

    with pytest.raises(
        IntegrityError,
        match="fk_agentexecution_current_attempt_lineage",
    ):
        with get_engine().begin() as connection:
            connection.execute(
                text(
                    "UPDATE agentexecution SET current_attempt_id = :attempt_id "
                    "WHERE id = :execution_id"
                ),
                {"attempt_id": attempt_id, "execution_id": first_id},
            )


def test_deleting_execution_cascades_its_referenced_current_attempt() -> None:
    execution_id = "attempt-lineage-execution-delete"
    attempt_id = "attempt-lineage-delete-attempt"
    with Session(get_engine()) as session:
        execution = _seed_execution(session, "delete")
        attempt = AgentExecutionAttempt(
            id=attempt_id,
            execution_id=execution.id,
            ordinal=1,
        )
        session.add(attempt)
        session.flush()
        execution.current_attempt_id = attempt.id
        session.add(execution)
        session.commit()
        execution_id = execution.id

    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        session.delete(execution)
        session.commit()

    with Session(get_engine()) as session:
        assert session.get(AgentExecution, execution_id) is None
        assert session.get(AgentExecutionAttempt, attempt_id) is None


def test_current_attempt_migration_fails_fast_without_reassigning_rows() -> None:
    config = build_alembic_config()
    engine = get_engine()
    first_id = "attempt-lineage-execution-migration-first"
    attempt_id = "attempt-lineage-migration-second-attempt"

    try:
        command.downgrade(config, "202608040005")
        with Session(engine) as session:
            first = _seed_execution(session, "migration-first")
            second = _seed_execution(session, "migration-second")
            session.add(
                AgentExecutionAttempt(
                    id=attempt_id,
                    execution_id=second.id,
                    ordinal=1,
                )
            )
            session.flush()
            first.current_attempt_id = attempt_id
            session.add(first)
            session.commit()
            first_id = first.id

        with pytest.raises(IntegrityError, match="current_attempt lineage preflight failed"):
            command.upgrade(config, "202608040006")

        with engine.connect() as connection:
            persisted_attempt_id = connection.execute(
                text(
                    "SELECT current_attempt_id FROM agentexecution "
                    "WHERE id = :execution_id"
                ),
                {"execution_id": first_id},
            ).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert persisted_attempt_id == attempt_id
        assert revision == "202608040005"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agentexecution SET current_attempt_id = NULL "
                    "WHERE id = :execution_id"
                ),
                {"execution_id": first_id},
            )
        command.upgrade(config, "head")
