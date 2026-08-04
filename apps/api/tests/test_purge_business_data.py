from datetime import timedelta

import pytest
from contentai.db.session import get_engine
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ServiceHeartbeat,
    ToolExecution,
)
from contentai.models.enums import MessageRole, RunStatus
from contentai.models.memory import MemoryRecord
from contentai.services.purge_business_data import MAX_BATCH_SIZE, purge_business_data
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlalchemy import text
from sqlmodel import Session, select


def test_retention_preserves_old_session_with_new_message_and_memory() -> None:
    cutoff = utcnow() - timedelta(days=1)
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-retention-new-child",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
            created_at=cutoff - timedelta(days=1),
        )
        session.add(chat)
        session.flush()
        session.add(
            ChatMessage(
                id="message-retention-new-child",
                session_id=chat.id,
                role=MessageRole.user,
                content="new child",
                created_at=cutoff + timedelta(hours=1),
            )
        )
        session.add(
            MemoryRecord(
                id="memory-retention-new-child",
                user_id="local-user",
                session_id=chat.id,
                memory_key="new-child",
                created_at=cutoff + timedelta(hours=1),
            )
        )
        session.commit()

    purge_business_data(batch_size=10, retention_cutoff=cutoff)

    with Session(get_engine()) as session:
        assert session.get(ChatSession, "session-retention-new-child") is not None
        assert session.get(ChatMessage, "message-retention-new-child") is not None
        assert session.get(MemoryRecord, "memory-retention-new-child") is not None


def test_retention_preserves_old_agent_and_version_with_new_session() -> None:
    cutoff = utcnow() - timedelta(days=1)
    with Session(get_engine()) as session:
        profile = AgentProfile(
            id="agent-retention-new-child",
            user_id="local-user",
            name="Retention parent",
            created_at=cutoff - timedelta(days=2),
        )
        session.add(profile)
        session.flush()
        version = AgentVersion(
            id="version-retention-new-child",
            agent_id=profile.id,
            content_prompt="test",
            created_at=cutoff - timedelta(days=2),
        )
        session.add(version)
        session.flush()
        session.add(
            ChatSession(
                id="session-retention-agent-child",
                agent_id=profile.id,
                agent_version_id=version.id,
                user_id="local-user",
                created_at=cutoff + timedelta(hours=1),
            )
        )
        session.commit()

    purge_business_data(batch_size=10, retention_cutoff=cutoff)

    with Session(get_engine()) as session:
        assert session.get(AgentProfile, "agent-retention-new-child") is not None
        assert session.get(AgentVersion, "version-retention-new-child") is not None
        assert session.get(ChatSession, "session-retention-agent-child") is not None


def test_dry_run_is_bounded_and_does_not_delete() -> None:
    cutoff = utcnow() - timedelta(days=1)
    with Session(get_engine()) as session:
        for index in range(3):
            session.add(
                ServiceHeartbeat(
                    id=f"heartbeat-dry-{index}",
                    service_name="test",
                    instance_id=str(index),
                    created_at=cutoff - timedelta(days=1),
                )
            )
        session.commit()

    summary = purge_business_data(dry_run=True, batch_size=2, retention_cutoff=cutoff)

    assert summary["tables"]["serviceheartbeat"] == {
        "candidate_count": 2,
        "has_more": True,
        "deleted": 0,
    }
    with Session(get_engine()) as session:
        assert len(session.exec(select(ServiceHeartbeat)).all()) == 3
    assert "agentevent" not in summary["tables"]


def test_execute_deletes_at_most_one_batch_and_repeated_runs_converge() -> None:
    cutoff = utcnow() - timedelta(days=1)
    with Session(get_engine()) as session:
        for index in range(3):
            session.add(
                ServiceHeartbeat(
                    id=f"heartbeat-run-{index}",
                    service_name="test",
                    instance_id=str(index),
                    created_at=cutoff - timedelta(days=1),
                )
            )
        session.commit()

    first = purge_business_data(batch_size=2, retention_cutoff=cutoff)
    second = purge_business_data(batch_size=2, retention_cutoff=cutoff)
    third = purge_business_data(batch_size=2, retention_cutoff=cutoff)

    assert first["tables"]["serviceheartbeat"]["deleted"] == 2
    assert second["tables"]["serviceheartbeat"]["deleted"] == 1
    assert third["tables"]["serviceheartbeat"]["deleted"] == 0


def test_checkpoint_purge_is_bounded_dry_run_safe_and_converges() -> None:
    with Session(get_engine()) as session:
        for index in range(3):
            values = {
                "thread_id": f"thread-purge-checkpoint-{index}",
                "checkpoint_id": f"checkpoint-purge-{index}",
                "version": str(index),
            }
            session.execute(
                text(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (:thread_id, 'purge', :checkpoint_id, '{}'::jsonb, '{}'::jsonb)"
                ),
                values,
            )
            session.execute(
                text(
                    "INSERT INTO checkpoint_blobs "
                    "(thread_id, checkpoint_ns, channel, version, type, blob) "
                    "VALUES (:thread_id, 'purge', 'messages', :version, 'empty', NULL)"
                ),
                values,
            )
            session.execute(
                text(
                    "INSERT INTO checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, "
                    "idx, channel, type, blob) "
                    "VALUES (:thread_id, 'purge', :checkpoint_id, 'task', '', 0, "
                    "'messages', 'msgpack', :blob)"
                ),
                {**values, "blob": b"checkpoint-purge"},
            )
        session.commit()

    dry_run = purge_business_data(dry_run=True, batch_size=2)
    expected_dry_run = {"candidate_count": 2, "has_more": True, "deleted": 0}
    assert dry_run["checkpoints"] == {
        "checkpoint_writes": expected_dry_run,
        "checkpoint_blobs": expected_dry_run,
        "checkpoints": expected_dry_run,
    }

    with Session(get_engine()) as session:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            assert (
                session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 3
            )

    first = purge_business_data(batch_size=2)
    second = purge_business_data(batch_size=2)
    third = purge_business_data(batch_size=2)

    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        assert first["checkpoints"][table]["deleted"] == 2
        assert first["checkpoints"][table]["has_more"] is True
        assert second["checkpoints"][table]["deleted"] == 1
        assert third["checkpoints"][table] == {
            "candidate_count": 0,
            "has_more": False,
            "deleted": 0,
        }


def test_bounded_purge_removes_old_execution_cohorts_in_fk_order() -> None:
    cutoff = utcnow() - timedelta(days=1)
    old = cutoff - timedelta(days=1)
    newer = cutoff + timedelta(hours=1)
    with Session(get_engine()) as session:
        for suffix, created_at in (("old-a", old), ("old-b", old), ("new", newer)):
            chat = ChatSession(
                id=f"session-purge-cohort-{suffix}",
                agent_id="default-agent",
                agent_version_id="default-agent-v1",
                user_id="local-user",
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(chat)
            session.flush()
            invocation = AgentInvocation(
                id=f"invocation-purge-cohort-{suffix}",
                session_id=chat.id,
                agent_id=chat.agent_id,
                user_id=chat.user_id,
                created_at=created_at,
            )
            session.add(invocation)
            session.flush()
            execution = AgentExecution(
                id=f"execution-purge-cohort-{suffix}",
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id=chat.agent_version_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status=RunStatus.completed,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(execution)
            session.flush()
            session.add_all(
                [
                    ChatMessage(
                        id=f"message-purge-cohort-{suffix}",
                        session_id=chat.id,
                        invocation_id=invocation.id,
                        execution_id=execution.id,
                        role=MessageRole.assistant,
                        content="old cohort",
                        created_at=created_at,
                    ),
                    AgentExecutionAttempt(
                        id=f"attempt-purge-cohort-{suffix}",
                        execution_id=execution.id,
                        ordinal=1,
                        started_at=created_at,
                    ),
                    ExecutionOutbox(
                        id=f"outbox-purge-cohort-{suffix}",
                        execution_id=execution.id,
                        model_config_id=DEFAULT_MODEL_CONFIG_ID,
                        created_at=created_at,
                        updated_at=created_at,
                    ),
                    ToolExecution(
                        id=f"tool-purge-cohort-{suffix}",
                        execution_id=execution.id,
                        tool_name="test",
                        sequence=1,
                        created_at=created_at,
                        updated_at=created_at,
                    ),
                ]
            )
        session.commit()

    first = purge_business_data(batch_size=1, retention_cutoff=cutoff)
    second = purge_business_data(batch_size=1, retention_cutoff=cutoff)
    third = purge_business_data(batch_size=1, retention_cutoff=cutoff)

    assert first["tables"]["agentexecution"]["deleted"] == 1
    assert second["tables"]["agentexecution"]["deleted"] == 1
    assert third["tables"]["agentexecution"]["deleted"] == 0
    with Session(get_engine()) as session:
        for suffix in ("old-a", "old-b"):
            assert session.get(ChatSession, f"session-purge-cohort-{suffix}") is None
            assert session.get(AgentInvocation, f"invocation-purge-cohort-{suffix}") is None
            assert session.get(AgentExecution, f"execution-purge-cohort-{suffix}") is None
            assert session.get(ChatMessage, f"message-purge-cohort-{suffix}") is None
            assert session.get(AgentExecutionAttempt, f"attempt-purge-cohort-{suffix}") is None
            assert session.get(ExecutionOutbox, f"outbox-purge-cohort-{suffix}") is None
            assert session.get(ToolExecution, f"tool-purge-cohort-{suffix}") is None
        assert session.get(ChatSession, "session-purge-cohort-new") is not None
        assert session.get(AgentInvocation, "invocation-purge-cohort-new") is not None
        assert session.get(AgentExecution, "execution-purge-cohort-new") is not None


@pytest.mark.parametrize("batch_size", [0, MAX_BATCH_SIZE + 1])
def test_batch_size_rejects_out_of_range_values(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        purge_business_data(dry_run=True, batch_size=batch_size)
