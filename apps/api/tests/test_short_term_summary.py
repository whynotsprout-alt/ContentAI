from __future__ import annotations

from contentai.db.session import get_engine
from contentai.memory.repository import MemoryRepository
from contentai.memory.short_term import ShortTermMemory
from contentai.models.chat import ChatMessage, ChatSession
from contentai.models.enums import MessageRole
from sqlmodel import Session


def test_short_term_summary_accumulates_from_cursor_without_losing_early_constraints() -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-cumulative-summary",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
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
            user_id="local-user",
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
            user_id="local-user",
        )
        entry = repository.get(
            "summary",
            user_id="local-user",
            session_id=chat.id,
        )

    assert cumulative.count("never invent source links") == 1
    assert "Recent decision" in cumulative
    assert entry is not None
    assert entry.payload["cursor_message_id"] == second_id


def test_short_term_memory_load_messages_limits_history_in_sql_and_keeps_order() -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-short-term-window",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        for index in range(45):
            session.add(
                ChatMessage(
                    id=f"message-window-{index:03d}",
                    session_id=chat.id,
                    role=MessageRole.user,
                    content=f"message {index}",
                )
            )
        session.commit()

        messages = ShortTermMemory(MemoryRepository(session)).load_messages(
            session,
            session_id=chat.id,
        )

    assert len(messages) == 40
    assert messages[0].content == "message 5"
    assert messages[-1].content == "message 44"
