from datetime import timedelta

import pytest
from db.session import get_engine
from models.agent import AgentProfile, AgentVersion
from models.base import utcnow
from models.chat import ChatMessage, ChatSession, ServiceHeartbeat
from models.enums import MessageRole
from models.memory import MemoryRecord
from services.purge_business_data import MAX_BATCH_SIZE, purge_business_data
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


@pytest.mark.parametrize("batch_size", [0, MAX_BATCH_SIZE + 1])
def test_batch_size_rejects_out_of_range_values(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        purge_business_data(dry_run=True, batch_size=batch_size)
