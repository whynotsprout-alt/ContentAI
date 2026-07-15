from datetime import timedelta

from db.session import get_engine
from memory import MemoryRepository
from models.base import utcnow
from models.enums import MemoryOwnerType, MemoryScope
from models.memory import MemoryRecord
from sqlmodel import Session, select


def test_memory_repository_allows_large_content():
    large_content = "summary line " * 600

    with Session(get_engine()) as session:
        entry = MemoryRepository(session).upsert(
            "summary",
            content=large_content,
            kind="summary",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.session,
            session_id="large-content-test",
            memory_scope=MemoryScope.short_term,
        )

    assert entry.content == large_content.strip()


def test_memory_repository_isolates_long_term_records_by_tenant_user_account():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        tenant_a = repository.upsert(
            "favorite_language",
            content="prefers Python",
            kind="preference",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )
        tenant_b = repository.upsert(
            "favorite_language",
            content="prefers Java",
            kind="preference",
            tenant_id="tenant_b",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )

        recalled = repository.search(
            "prefers",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )

    assert tenant_a.content == "prefers Python"
    assert tenant_b.content == "prefers Java"
    assert [item.content for item in recalled] == ["prefers Python"]


def test_memory_repository_filters_expired_and_deleted_records():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "active",
            content="active memory",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )
        repository.upsert(
            "expired",
            content="expired memory",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
            expires_at=utcnow() - timedelta(days=1),
        )
        deleted = repository.upsert(
            "deleted",
            content="deleted memory",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )
        row = session.exec(select(MemoryRecord).where(MemoryRecord.memory_key == deleted.key)).one()
        row.deleted_at = utcnow()
        session.add(row)
        session.commit()

        listed = repository.list_scope(
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )

    assert [item.content for item in listed] == ["active memory"]


def test_memory_repository_tracks_access_statistics():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "remembered",
            content="accessed memory",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )

        entry = repository.get(
            "remembered",
            tenant_id="tenant_a",
            user_id="user_1",
            owner_type=MemoryOwnerType.agent,
            agent_id="agent_1",
            memory_scope=MemoryScope.long_term,
        )

    assert entry is not None
    assert entry.access_count == 1
    assert entry.last_accessed_at is not None
