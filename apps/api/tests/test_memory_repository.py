from datetime import timedelta

from contentai.db.session import get_engine
from contentai.memory import MemoryRepository
from contentai.models.base import utcnow
from contentai.models.chat import ChatSession
from contentai.models.memory import MemoryRecord
from sqlmodel import Session, select


def _session(session: Session, session_id: str) -> ChatSession:
    chat = ChatSession(
        id=session_id,
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
    )
    session.add(chat)
    session.commit()
    return chat


def test_memory_repository_allows_large_content():
    large_content = "summary line " * 600
    with Session(get_engine()) as session:
        chat = _session(session, "large-content-test")
        entry = MemoryRepository(session).upsert(
            "summary",
            content=large_content,
            kind="summary",
            user_id="local-user",
            session_id=chat.id,
        )
    assert entry.content == large_content.strip()
    assert entry.memory_scope == "short_term"


def test_memory_repository_isolates_long_term_and_session_records():
    with Session(get_engine()) as session:
        chat = _session(session, "scope-test")
        repository = MemoryRepository(session)
        repository.upsert(
            "preference",
            content="durable preference",
            user_id="local-user",
            agent_id="default-agent",
        )
        repository.upsert(
            "preference",
            content="session summary",
            user_id="local-user",
            session_id=chat.id,
        )
        durable = repository.search(
            "preference", user_id="local-user", agent_id="default-agent"
        )
        transient = repository.list_scope(user_id="local-user", session_id=chat.id)
    assert [item.content for item in durable] == ["durable preference"]
    assert [item.content for item in transient] == ["session summary"]


def test_memory_repository_filters_expired_and_deleted_records():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "active",
            content="active memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        repository.upsert(
            "expired",
            content="expired memory",
            user_id="local-user",
            agent_id="default-agent",
            expires_at=utcnow() - timedelta(days=1),
        )
        deleted = repository.upsert(
            "deleted",
            content="deleted memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        row = session.exec(select(MemoryRecord).where(MemoryRecord.memory_key == deleted.key)).one()
        row.deleted_at = utcnow()
        session.add(row)
        session.commit()
        listed = repository.list_scope(user_id="local-user", agent_id="default-agent")
    assert [item.content for item in listed] == ["active memory"]


def test_memory_repository_tracks_access_statistics():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "remembered",
            content="accessed memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        entry = repository.get(
            "remembered",
            user_id="local-user",
            agent_id="default-agent",
        )
    assert entry is not None
    assert entry.access_count == 1
    assert entry.last_accessed_at is not None
