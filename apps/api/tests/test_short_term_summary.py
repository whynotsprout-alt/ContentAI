from __future__ import annotations

from db.session import get_engine
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from models.chat import ChatMessage, ChatSession
from models.enums import MemoryOwnerType, MemoryScope, MessageRole
from sqlmodel import Session


def test_short_term_summary_accumulates_from_cursor_without_losing_early_constraints() -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-cumulative-summary",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.flush()
        first = ChatMessage(
            id="message-summary-1",
            session_id=chat.id,
            role=MessageRole.user,
            content="Early constraint: never invent source links.",
        )
        session.add(first)
        session.commit()

        repository = MemoryRepository(session)
        memory = ShortTermMemory(repository)
        initial = memory.refresh_summary(
            session,
            session_id=chat.id,
            tenant_id="local",
            user_id="local-user",
            agent_id="default-agent",
        )
        assert "never invent source links" in initial

        second = ChatMessage(
            id="message-summary-2",
            session_id=chat.id,
            role=MessageRole.assistant,
            content="Recent decision: use the second topic.",
        )
        second_id = second.id
        session.add(second)
        session.commit()
        cumulative = memory.refresh_summary(
            session,
            session_id=chat.id,
            tenant_id="local",
            user_id="local-user",
            agent_id="default-agent",
        )
        entry = repository.get(
            "summary",
            tenant_id="local",
            user_id="local-user",
            owner_type=MemoryOwnerType.session,
            agent_id="default-agent",
            session_id=chat.id,
            memory_scope=MemoryScope.short_term,
        )

    assert cumulative.count("never invent source links") == 1
    assert "Recent decision" in cumulative
    assert entry is not None
    assert entry.payload["cursor_message_id"] == second_id
